from __future__ import annotations

import hashlib
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from app.config import settings

ALLOWED_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".zip", ".py", ".txt", ".md", ".ino",
    ".c", ".h", ".cpp", ".hpp", ".json", ".csv", ".docx", ".xlsx", ".pptx",
    ".mp3", ".wav", ".ogg", ".m4a", ".mp4", ".webm",
}


class UploadRejected(ValueError):
    pass


@dataclass(frozen=True)
class StoredUpload:
    stored_key: str
    original_name: str
    mime_type: str
    sha256: str
    size_bytes: int
    path: Path


def safe_filename(value: str) -> str:
    base = Path(value.replace("\\", "/")).name
    clean = re.sub(r"[^\w .()\-\u0600-\u06FF]", "_", base).strip(" .")
    return (clean or "attachment")[:180]


def _sniff(prefix: bytes, suffix: str) -> str:
    if prefix.startswith(b"%PDF-"):
        return "application/pdf"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return "image/webp"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WAVE":
        return "audio/wav"
    if prefix.startswith(b"OggS"):
        return "audio/ogg"
    if prefix.startswith(b"ID3") or (len(prefix) > 1 and prefix[0] == 0xFF and prefix[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    if prefix.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if len(prefix) > 8 and prefix[4:8] == b"ftyp":
        return "video/mp4" if suffix == ".mp4" else "audio/mp4"
    if prefix.startswith(b"PK\x03\x04"):
        return "application/zip"
    if suffix == ".json" and prefix.lstrip().startswith((b"{", b"[", b'"', b"true", b"false", b"null", b"-", *tuple(bytes((b,)) for b in range(48, 58)))):
        return "application/json"
    guessed, _ = mimetypes.guess_type("file" + suffix)
    if guessed and guessed.startswith("text/"):
        return guessed
    if suffix in {".docx", ".xlsx", ".pptx"} and prefix.startswith(b"PK"):
        return "application/zip"
    return "application/octet-stream"


def register_download(path: Path, provider_name: str, declared_mime: str | None = None) -> StoredUpload:
    name = safe_filename(provider_name)
    suffix = Path(name).suffix.casefold()
    size = path.stat().st_size
    if suffix not in ALLOWED_EXTENSIONS or not size or size > settings.max_upload_mb * 1024 * 1024:
        path.unlink(missing_ok=True)
        raise UploadRejected("نوع یا اندازه فایل مجاز نیست")
    with path.open("rb") as source:
        prefix = source.read(1024)
        source.seek(0)
        digest = hashlib.sha256()
        while chunk := source.read(128 * 1024):
            digest.update(chunk)
    detected = _sniff(prefix, suffix)
    if detected == "application/octet-stream" and suffix not in {".c", ".h", ".cpp", ".hpp", ".ino"}:
        path.unlink(missing_ok=True)
        raise UploadRejected("نوع فایل با محتوای آن سازگار نیست")
    if declared_mime and declared_mime.startswith(("image/", "application/pdf", "audio/", "video/")) and detected != declared_mime:
        path.unlink(missing_ok=True)
        raise UploadRejected("نوع فایل با اطلاعات پیام‌رسان سازگار نیست")
    if suffix in {".mp3", ".wav", ".ogg", ".m4a", ".mp4", ".webm"} and not detected.startswith(("audio/", "video/")):
        path.unlink(missing_ok=True)
        raise UploadRejected("محتوای صوتی یا تصویری معتبر نیست")
    key = uuid4().hex
    target = settings.uploads_dir.resolve() / key
    if path.resolve() != target:
        path.replace(target)
    return StoredUpload(key, name, detected, digest.hexdigest(), size, target)


# Keep document transfers within both providers' supported sizes.
MAX_DOCUMENT_CAPTION = 1024


def save_document(source: BinaryIO, name: str, byte_limit: int) -> StoredUpload:
    root = settings.uploads_dir.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = root / uuid4().hex
    try:
        size = 0
        with open(temp, "xb", opener=lambda path, flags: os.open(path, flags, 0o600)) as target:
            while chunk := source.read(128 * 1024):
                size += len(chunk)
                if size > byte_limit:
                    raise UploadRejected("حجم فایل بیشتر از سقف مجاز است.")
                target.write(chunk)
        return register_download(temp, name)
    finally:
        temp.unlink(missing_ok=True)
