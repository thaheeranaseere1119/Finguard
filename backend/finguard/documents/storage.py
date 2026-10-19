"""Private document storage and upload validation.

Files are stored outside any web root under STORAGE_DIR/<org>/<sha256-prefix>/<uuid>.bin. The client filename is
never used to build a path (no path traversal). Type is decided by magic bytes, not by extension or the client's
Content-Type, and must agree with the extension.
"""

from __future__ import annotations

import hashlib
import io
import re
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from finguard.core.config import get_settings
from finguard.core.errors import ValidationFailed

ALLOWED = {
    "application/pdf": {".pdf"},
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {".docx"},
}


@dataclass(frozen=True)
class ValidatedUpload:
    data: bytes
    mime_type: str
    sha256: str
    page_count: int
    safe_filename: str


def sniff_mime(data: bytes) -> str | None:
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = set(z.namelist())
                if "word/document.xml" in names:
                    # Zip-bomb guard: refuse archives that expand far beyond the upload limit.
                    if sum(i.file_size for i in z.infolist()) > 20 * get_settings().max_upload_bytes:
                        return None
                    return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        except zipfile.BadZipFile:
            return None
    return None


def sanitize_filename(name: str) -> str:
    base = Path(name or "document").name
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base).strip(" .") or "document"
    return base[:120]


def validate_upload(filename: str, data: bytes) -> ValidatedUpload:
    s = get_settings()
    if not data:
        raise ValidationFailed("empty file")
    if len(data) > s.max_upload_bytes:
        raise ValidationFailed(f"file exceeds {s.max_upload_bytes // (1024 * 1024)} MB limit")
    mime = sniff_mime(data)
    if mime is None:
        raise ValidationFailed("unsupported or unrecognized file type (allowed: PDF, PNG, JPEG, DOCX)")
    safe = sanitize_filename(filename)
    ext = Path(safe).suffix.lower()
    if ext not in ALLOWED[mime]:
        raise ValidationFailed(f"file extension {ext or '(none)'} does not match detected type {mime}")
    pages = 1
    if mime == "application/pdf":
        import fitz

        try:
            with fitz.open(stream=data, filetype="pdf") as doc:
                if doc.needs_pass or doc.is_encrypted:
                    raise ValidationFailed("encrypted PDFs are not accepted")
                pages = doc.page_count
        except ValidationFailed:
            raise
        except Exception as exc:
            raise ValidationFailed("PDF could not be parsed") from exc
        if pages < 1 or pages > s.max_pdf_pages:
            raise ValidationFailed(f"PDF must have between 1 and {s.max_pdf_pages} pages")
    elif mime.startswith("image/"):
        from PIL import Image

        try:
            with Image.open(io.BytesIO(data)) as im:
                im.verify()
                if im.size[0] * im.size[1] > 60_000_000:
                    raise ValidationFailed("image dimensions too large")
        except ValidationFailed:
            raise
        except Exception as exc:
            raise ValidationFailed("image could not be decoded") from exc
    return ValidatedUpload(data, mime, hashlib.sha256(data).hexdigest(), pages, safe)


def store(org_id: uuid.UUID, upload: ValidatedUpload) -> str:
    key = f"{org_id}/{upload.sha256[:2]}/{uuid.uuid4().hex}.bin"
    path = _path_for(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(upload.data)
    return key


def read(storage_key: str) -> bytes:
    return _path_for(storage_key).read_bytes()


def _path_for(key: str) -> Path:
    root = Path(get_settings().storage_dir).resolve()
    path = (root / key).resolve()
    if root not in path.parents:
        raise ValidationFailed("invalid storage key")
    return path
