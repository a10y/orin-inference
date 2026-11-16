"""Runtime configuration (overridable with environment variables)."""
import os
import subprocess
import time
from pathlib import Path

BASE_DIR = Path(os.environ.get("INFER_HOME", Path(__file__).resolve().parent.parent))
DATA_DIR = Path(os.environ.get("INFER_DATA", BASE_DIR / "data"))
JOBS_DIR = DATA_DIR / "jobs"
WEIGHTS_DIR = DATA_DIR / "weights"
DB_PATH = DATA_DIR / "jobs.db"
STATIC_DIR = BASE_DIR / "server" / "static"
# While this file exists the worker finishes its current job but starts no new ones.
# deploy/deploy.sh uses it to drain the queue before restarting.
PAUSE_FILE = DATA_DIR / "PAUSE"
PAUSE_MAX_AGE = 3 * 3600  # ignore a pause left behind by a deploy that died


def paused():
    try:
        return PAUSE_FILE.stat().st_mtime > time.time() - PAUSE_MAX_AGE
    except FileNotFoundError:
        return False

PORT = int(os.environ.get("INFER_PORT", "8765"))
MAX_UPLOAD_BYTES = int(float(os.environ.get("INFER_MAX_UPLOAD_GB", "8")) * 1024**3)
RETENTION_DAYS = float(os.environ.get("INFER_RETENTION_DAYS", "14"))
BATCH_SIZE = int(os.environ.get("INFER_BATCH", "8"))
MAX_LOADED_MODELS = int(os.environ.get("INFER_MAX_LOADED_MODELS", "2"))

# Keep downloaded torchvision weights inside the data dir.
os.environ.setdefault("TORCH_HOME", str(DATA_DIR / "torch"))


def _git_sha():
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=BASE_DIR, capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


# Commit the running code was started from; the deploy health check compares against it.
VERSION = _git_sha()

for d in (JOBS_DIR, WEIGHTS_DIR):
    d.mkdir(parents=True, exist_ok=True)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".mpg", ".mpeg", ".ts", ".wmv", ".flv"}
# Containers we can write H.264 into directly; anything else is re-muxed to .mp4.
H264_CONTAINERS = {".mp4", ".mov", ".m4v", ".mkv"}
