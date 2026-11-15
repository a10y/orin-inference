"""HTTP API + static frontend for private object-detection inference."""
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, models, worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("infer.app")

# Host names this service may be reached by (Tailscale IP + MagicDNS names).
# Anything else is rejected, which blocks DNS-rebinding attacks.
ALLOWED_HOSTS = {h.strip().lower() for h in os.environ.get("INFER_ALLOWED_HOSTS", "").split(",") if h.strip()}
CLIENT_HEADER = "x-infer-client"

app = FastAPI(title="Orin Inference", docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
db.init(config.DB_PATH)
db.recover_after_restart()
_worker = worker.Worker()


@app.middleware("http")
async def guard(request: Request, call_next):
    host = (request.headers.get("host") or "").lower()
    hostname = urlsplit(f"//{host}").hostname or ""
    if ALLOWED_HOSTS and hostname not in ALLOWED_HOSTS:
        return JSONResponse({"detail": "Host not allowed"}, status_code=421)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        # Cross-site request forgery protection: browsers can't send a custom header
        # cross-origin without a CORS preflight, which this server never approves.
        if request.headers.get(CLIENT_HEADER) != "1":
            return JSONResponse({"detail": f"Missing {CLIENT_HEADER} header"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and (urlsplit(origin).netloc or "").lower() != host:
            return JSONResponse({"detail": "Cross-origin request rejected"}, status_code=403)
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    return resp


@app.on_event("startup")
def _start():
    _worker.start()
    threading.Thread(target=_janitor, daemon=True, name="janitor").start()
    log.info("worker started; data dir %s", config.DATA_DIR)


def _janitor():
    """Delete finished jobs older than the retention window, plus orphaned partial uploads."""
    while True:
        try:
            cutoff = time.time() - config.RETENTION_DAYS * 86400
            for job_id in db.finished_before(cutoff):
                worker.purge_job_files(job_id)
                db.delete(job_id)
                log.info("expired job %s", job_id)
            for p in config.JOBS_DIR.iterdir():
                if p.is_dir() and db.get(p.name) is None and time.time() - p.stat().st_mtime > 3600:
                    worker.purge_job_files(p.name)
        except Exception:  # noqa: BLE001
            log.exception("janitor error")
        time.sleep(3600)


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    stem, ext = os.path.splitext(name)
    stem = _SAFE.sub("_", stem).strip("._")[:80] or "upload"
    return stem + _SAFE.sub("", ext.lower())[:10]


def _public(job):
    job = dict(job)
    job.pop("input_name", None)
    job["outputs"] = [{**o, "url": f"/api/jobs/{job['id']}/files/{o['name']}"} for o in job["outputs"]]
    return job


def _job_or_404(job_id):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(404, "No such job")
    job = db.get(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    return job


@app.get("/api/models")
def list_models():
    return {"models": models.describe(), "default": models.DEFAULT_MODEL, "loaded": models.loaded_ids()}


@app.get("/api/status")
def status():
    return {"counts": db.counts(), "current_job": _worker.current, "loaded_models": models.loaded_ids(),
            "max_upload_bytes": config.MAX_UPLOAD_BYTES, "retention_days": config.RETENTION_DAYS}


@app.get("/api/jobs")
def list_jobs(limit: int = Query(200, ge=1, le=1000)):
    return {"jobs": [_public(j) for j in db.list_jobs(limit)]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return _public(_job_or_404(job_id))


@app.post("/api/jobs", status_code=201)
async def create_job(
    request: Request,
    filename: str = Query(..., min_length=1, max_length=255),
    model: str = Query(models.DEFAULT_MODEL),
    output: str = Query("media", pattern="^(media|json)$"),
    conf: float = Query(0.35, ge=0.01, le=0.99),
):
    """Upload the raw file as the request body; options go in the query string."""
    if model not in models.REGISTRY:
        raise HTTPException(400, f"Unknown model {model!r}")
    name = _safe_name(filename)
    ext = Path(name).suffix
    if ext in config.IMAGE_EXTS:
        media = "image"
    elif ext in config.VIDEO_EXTS:
        media = "video"
    else:
        raise HTTPException(415, f"Unsupported file type {ext or '(none)'}")
    declared = request.headers.get("content-length")
    if declared and int(declared) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File too large")

    job_id = uuid.uuid4().hex
    d = worker.job_dir(job_id)
    d.mkdir(parents=True)
    input_name = f"input{ext}"
    dest = d / input_name
    size = 0
    try:
        with open(dest, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > config.MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "File too large")
                f.write(chunk)
        if size == 0:
            raise HTTPException(400, "Empty upload")
    except BaseException:
        worker.purge_job_files(job_id)
        raise
    # Name outputs after the user's file, not "input".
    named = d / name
    if named.name != input_name:
        dest.rename(named)
        input_name = named.name

    job = db.create(id=job_id, filename=Path(filename.replace("\\", "/")).name[:255], input_name=input_name,
                    media=media, size=size, model=model, output=output, conf=conf,
                    message="Waiting in queue")
    _worker.wake.set()
    log.info("job %s queued: %s (%s, %d bytes) model=%s output=%s", job_id, name, media, size, model, output)
    return _public(job)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    _job_or_404(job_id)
    if db.transition(job_id, ("queued",), status="canceled", finished=time.time(), message="Canceled"):
        return {"status": "canceled"}
    if db.transition(job_id, ("running",), status="canceling", message="Canceling…"):
        return {"status": "canceling"}
    raise HTTPException(409, "Job is not queued or running")


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    job = _job_or_404(job_id)
    if job["status"] in ("running", "canceling"):
        raise HTTPException(409, "Cancel the job before deleting it")
    db.delete(job_id)
    worker.purge_job_files(job_id)
    return {"deleted": job_id}


@app.get("/api/jobs/{job_id}/files/{name}")
def download(job_id: str, name: str, inline: bool = False):
    job = _job_or_404(job_id)
    allowed = {o["name"] for o in job["outputs"]}
    if name not in allowed:
        raise HTTPException(404, "No such file")
    path = worker.job_dir(job_id) / name
    if not path.is_file():
        raise HTTPException(410, "File expired")
    return FileResponse(path, filename=name, content_disposition_type="inline" if inline else "attachment")


app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")
