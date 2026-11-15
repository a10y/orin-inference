"""Runtime configuration (overridable with environment variables)."""
import os
from pathlib import Path

BASE_DIR = Path(os.environ.get("INFER_HOME", Path(__file__).resolve().parent.parent))
DATA_DIR = Path(os.environ.get("INFER_DATA", BASE_DIR / "data"))
JOBS_DIR = DATA_DIR / "jobs"
WEIGHTS_DIR = DATA_DIR / "weights"
DB_PATH = DATA_DIR / "jobs.db"
STATIC_DIR = BASE_DIR / "server" / "static"

PORT = int(os.environ.get("INFER_PORT", "8765"))
MAX_UPLOAD_BYTES = int(float(os.environ.get("INFER_MAX_UPLOAD_GB", "8")) * 1024**3)
RETENTION_DAYS = float(os.environ.get("INFER_RETENTION_DAYS", "14"))
BATCH_SIZE = int(os.environ.get("INFER_BATCH", "8"))
MAX_LOADED_MODELS = int(os.environ.get("INFER_MAX_LOADED_MODELS", "2"))

# Keep downloaded torchvision weights inside the data dir.
os.environ.setdefault("TORCH_HOME", str(DATA_DIR / "torch"))

for d in (JOBS_DIR, WEIGHTS_DIR):
    d.mkdir(parents=True, exist_ok=True)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".mpg", ".mpeg", ".ts", ".wmv", ".flv"}
# Containers we can write H.264 into directly; anything else is re-muxed to .mp4.
H264_CONTAINERS = {".mp4", ".mov", ".m4v", ".mkv"}
