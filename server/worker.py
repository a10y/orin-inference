"""Background worker: runs queued jobs one at a time on the GPU."""
import logging
import queue
import shutil
import subprocess
import threading
import time
import traceback

import cv2
import imageio_ffmpeg

from . import config, db, models
from .cocojson import CocoWriter
from .render import bgr_to_hex, class_colors, draw

log = logging.getLogger("infer.worker")
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
PROGRESS_INTERVAL = 0.5  # seconds between progress writes to the DB


class Canceled(Exception):
    pass


class Progress:
    def __init__(self, job_id, total):
        self.job_id = job_id
        self.total = total
        self.t0 = time.time()
        self.last = 0.0

    def __call__(self, done, force=False, message=None):
        now = time.time()
        if not force and now - self.last < PROGRESS_INTERVAL:
            return
        self.last = now
        if db.status(self.job_id) == "canceling":
            raise Canceled()
        fps = done / max(now - self.t0, 1e-6)
        fields = {"processed": done, "total": max(self.total, done), "fps": round(fps, 2)}
        if message:
            fields["message"] = message
        db.update(self.job_id, **fields)


def job_dir(job_id):
    return config.JOBS_DIR / job_id


def _summary(names, colors, counts, frames_with_dets, frames, extra=None):
    per_class = [
        {"class_id": c, "name": names[c], "color": bgr_to_hex(colors[c]), "detections": n,
         "frames": frames_with_dets.get(c, 0)}
        for c, n in sorted(counts.items(), key=lambda kv: -kv[1])
    ]
    return {"frames": frames, "total_detections": sum(counts.values()), "classes": per_class, **(extra or {})}


def _tally(dets, counts, frames_with):
    seen = set()
    for row in dets:
        c = int(row[5])
        counts[c] = counts.get(c, 0) + 1
        seen.add(c)
    for c in seen:
        frames_with[c] = frames_with.get(c, 0) + 1


def run_image(job, det, conf):
    d = job_dir(job["id"])
    src = d / job["input_name"]
    frame = cv2.imread(str(src), cv2.IMREAD_COLOR)  # applies EXIF orientation
    if frame is None:
        raise ValueError("Could not decode image")
    h, w = frame.shape[:2]
    prog = Progress(job["id"], 1)
    prog(0, force=True, message="Running inference")
    dets = det.predict([frame], conf)[0]
    names, colors = det.names, class_colors(det.names)
    counts, frames_with = {}, {}
    _tally(dets, counts, frames_with)
    outputs = []
    stem = src.stem
    if job["output"] == "media":
        ext = src.suffix.lower()
        if ext not in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}:
            ext = ".png"
        out = d / f"{stem}_detections{ext}"
        if not cv2.imwrite(str(out), draw(frame, dets, names, colors)):
            raise RuntimeError("Failed to encode output image")
        outputs.append({"name": out.name, "kind": "image", "size": out.stat().st_size})
    else:
        out = d / f"{stem}_detections.json"
        w_ = CocoWriter(out, _info(job, det, w, h), names)
        w_.add({"file_name": job["filename"], "width": w, "height": h}, dets)
        w_.close()
        outputs.append({"name": out.name, "kind": "json", "size": out.stat().st_size})
    prog(1, force=True, message="Done")
    return outputs, _summary(names, colors, counts, frames_with, 1, {"width": w, "height": h})


def _info(job, det, w, h, fps=None, frames=None):
    spec = models.REGISTRY[job["model"]]
    info = {
        "description": "Object detections (COCO results format, boxes are [x, y, width, height] in pixels)",
        "source_file": job["filename"],
        "media_type": job["media"],
        "model": spec.id,
        "model_name": spec.name,
        "model_license": spec.license,
        "confidence_threshold": job["conf"],
        "width": w,
        "height": h,
        "date_created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if fps is not None:
        info.update(fps=fps, frame_count=frames)
    return info


def _frame_reader(cap, batch, q, stop):
    """Decode frames on a separate thread so the GPU isn't waiting on the CPU."""
    try:
        while not stop.is_set():
            frames = []
            for _ in range(batch):
                ok, f = cap.read()
                if not ok:
                    break
                frames.append(f)
            if frames:
                q.put(frames)
            if len(frames) < batch:
                break
    finally:
        q.put(None)


def run_video(job, det, conf):
    d = job_dir(job["id"])
    src = d / job["input_name"]
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise ValueError("Could not open video")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    if not (0 < fps < 1000):
        fps = 30.0
    total = max(int(cap.get(cv2.CAP_PROP_FRAME_COUNT)), 0)
    names, colors = det.names, class_colors(det.names)
    prog = Progress(job["id"], total)

    ffmpeg = writer = out = None
    stem = src.stem
    w = h = None
    counts, frames_with = {}, {}
    n = 0
    q: "queue.Queue" = queue.Queue(maxsize=4)
    stop = threading.Event()
    reader = threading.Thread(target=_frame_reader, args=(cap, config.BATCH_SIZE, q, stop), daemon=True)
    reader.start()
    try:
        prog(0, force=True, message="Running inference")
        while True:
            frames = q.get()
            if frames is None:
                break
            if w is None:
                h, w = frames[0].shape[:2]
                if job["output"] == "media":
                    ext = src.suffix.lower() if src.suffix.lower() in config.H264_CONTAINERS else ".mp4"
                    out = d / f"{stem}_detections{ext}"
                    ffmpeg = _start_encoder(src, out, w, h, fps)
                else:
                    out = d / f"{stem}_detections.json"
                    writer = CocoWriter(out, None, names)
            results = det.predict(frames, conf)
            for i, (frame, dets) in enumerate(zip(frames, results)):
                _tally(dets, counts, frames_with)
                if ffmpeg:
                    if frame.shape[:2] != (h, w):
                        frame = cv2.resize(frame, (w, h))
                    ffmpeg.stdin.write(draw(frame, dets, names, colors).tobytes())
                else:
                    idx = n + i
                    writer.add({"file_name": f"{job['filename']}#frame={idx}", "width": w, "height": h,
                                "frame_index": idx, "timestamp_sec": round(idx / fps, 4)}, dets)
            n += len(frames)
            prog(n)
        if n == 0:
            raise ValueError("Video contained no decodable frames")
        prog.total = n
        prog(n, force=True, message="Finalizing output")
        if ffmpeg:
            ffmpeg.stdin.close()
            err = ffmpeg.stderr.read().decode(errors="replace")
            if ffmpeg.wait() != 0:
                raise RuntimeError(f"ffmpeg failed: {err[-2000:]}")
            ffmpeg = None
            kind = "video"
        else:
            writer.info = _info(job, det, w, h, fps=fps, frames=n)
            writer.close()
            writer = None
            kind = "json"
    finally:
        stop.set()
        while reader.is_alive():  # drain so the reader can exit
            try:
                q.get(timeout=0.1)
            except queue.Empty:
                pass
        cap.release()
        if ffmpeg:
            ffmpeg.kill()
            ffmpeg.wait()
        if writer:
            writer.discard_parts()

    outputs = [{"name": out.name, "kind": kind, "size": out.stat().st_size}]
    extra = {"width": w, "height": h, "fps": round(fps, 3), "duration_sec": round(n / fps, 2),
             "processing_fps": round(n / max(time.time() - prog.t0, 1e-6), 2)}
    return outputs, _summary(names, colors, counts, frames_with, n, extra)


def _start_encoder(src, out, w, h, fps):
    """H.264 encode annotated frames, copying the source's audio track if any."""
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", f"{fps:.6f}", "-i", "-",
        "-i", str(src),
        "-map", "0:v:0", "-map", "1:a:0?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "-c:a", "aac", "-b:a", "160k", "-shortest",
    ]
    if out.suffix in {".mp4", ".mov", ".m4v"}:
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(out))
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)


class Worker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="inference-worker")
        self.wake = threading.Event()
        self.current = None

    def run(self):
        while True:
            if config.paused():
                self.wake.wait(timeout=2)
                self.wake.clear()
                continue
            job = db.next_queued()
            if job is None:
                self.wake.wait(timeout=5)
                self.wake.clear()
                continue
            if not db.transition(job["id"], ("queued",), status="running", started=time.time(),
                                 message="Loading model"):
                continue
            self.current = job["id"]
            try:
                self._run(job)
            finally:
                self.current = None

    def _run(self, job):
        job_id = job["id"]
        try:
            det = models.get(job["model"])
            run = run_image if job["media"] == "image" else run_video
            outputs, summary = run(job, det, job["conf"])
            db.update(job_id, status="done", finished=time.time(), outputs=outputs, summary=summary,
                      message="Done")
            log.info("job %s done: %s", job_id, summary.get("total_detections"))
        except Canceled:
            self._cleanup_outputs(job)
            db.update(job_id, status="canceled", finished=time.time(), message="Canceled")
            log.info("job %s canceled", job_id)
        except Exception as e:  # noqa: BLE001 - report any failure on the job
            log.error("job %s failed\n%s", job_id, traceback.format_exc())
            self._cleanup_outputs(job)
            db.update(job_id, status="failed", finished=time.time(), error=f"{type(e).__name__}: {e}",
                      message="Failed")

    @staticmethod
    def _cleanup_outputs(job):
        d = job_dir(job["id"])
        for p in d.glob("*"):
            if p.name != job["input_name"]:
                p.unlink(missing_ok=True)


def purge_job_files(job_id):
    shutil.rmtree(job_dir(job_id), ignore_errors=True)
