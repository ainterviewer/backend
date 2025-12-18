from pathlib import Path

APP_DIR = Path("app")

STORAGE_DIR = APP_DIR.parent / "storage"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

QR_CODES_DIR = STORAGE_DIR / "qr_codes"
QR_CODES_DIR.mkdir(parents=True, exist_ok=True)

VIDEO_DIR = STORAGE_DIR / "videos"
VIDEO_DIR.mkdir(parents=True, exist_ok=True)

IMAGE_DIR = STORAGE_DIR / "videos"
IMAGE_DIR.mkdir(parents=True, exist_ok=True)
