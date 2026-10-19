"""Text-layer extraction with OCR fallback, preserving page numbers, line bounding boxes and OCR confidence.

* Digitally generated PDF pages: embedded text via PyMuPDF (`get_text("dict")`), confidence 100.
* Image-only / scanned pages (embedded chars < NATIVE_TEXT_MIN_CHARS): rendered at OCR_DPI and OCR'd with Tesseract
  (`image_to_data`), keeping word confidences and boxes, grouped into lines.
* PNG/JPEG: OCR directly. DOCX: paragraphs and table cells through python-docx (one logical page).

Bounding boxes are normalized to [0,1] page coordinates so the UI can highlight evidence on rendered pages.
The original text is kept; normalization (NFKC, whitespace) is a separate field.
"""

from __future__ import annotations

import io
import os
import unicodedata
from dataclasses import asdict, dataclass, field
from statistics import mean

from finguard.core.config import get_settings


@dataclass
class Line:
    text: str
    bbox: list[float] | None  # [x0, y0, x1, y1] normalized
    conf: float               # 0..100


@dataclass
class PageText:
    page: int
    method: str               # NATIVE | OCR | DOCX
    text: str
    lines: list[Line] = field(default_factory=list)
    ocr_confidence: float | None = None
    width: float | None = None
    height: float | None = None


@dataclass
class TextLayer:
    method: str               # NATIVE | OCR | MIXED | DOCX
    pages: list[PageText]
    mean_ocr_confidence: float | None

    @property
    def full_text(self) -> str:
        return "\n\n".join(f"[page {p.page}]\n{p.text}" for p in self.pages)

    def pages_json(self) -> list[dict]:
        return [asdict(p) for p in self.pages]


class OcrUnavailable(RuntimeError):
    pass


_CONTROL = {i: None for i in range(32) if i not in (9, 10)}
_CONTROL.update({0xA0: " ", 0xAD: "-", 0x2011: "-", 0x200B: None, 0xFEFF: None, 0x7F: None})


def clean_text(text: str) -> str:
    """Remove NUL/control characters and map no-break spaces / soft hyphens emitted by PDF text layers."""
    return text.translate(_CONTROL)


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace(" ", " ")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _tesseract():
    s = get_settings()
    if s.tessdata_prefix:
        os.environ["TESSDATA_PREFIX"] = s.tessdata_prefix
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
    except Exception as exc:  # binary missing
        raise OcrUnavailable("Tesseract OCR is not installed or not on PATH") from exc
    return pytesseract


def ocr_image(image, page_no: int) -> PageText:
    """OCR a PIL image into lines with confidences and normalized boxes."""
    pytesseract = _tesseract()
    from pytesseract import Output

    s = get_settings()
    img = image.convert("L")
    data = pytesseract.image_to_data(img, lang=s.ocr_language, config="--psm 4", output_type=Output.DICT)
    w, h = img.size
    groups: dict[tuple, list[int]] = {}
    for i, word in enumerate(data["text"]):
        if not word.strip():
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1
        if conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        groups.setdefault(key, []).append(i)
    lines: list[Line] = []
    for key in sorted(groups, key=lambda k: (min(data["top"][i] for i in groups[k]), k)):
        idx = groups[key]
        text = clean_text(" ".join(data["text"][i] for i in idx))
        x0 = min(data["left"][i] for i in idx) / w
        y0 = min(data["top"][i] for i in idx) / h
        x1 = max(data["left"][i] + data["width"][i] for i in idx) / w
        y1 = max(data["top"][i] + data["height"][i] for i in idx) / h
        lines.append(Line(text, [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)],
                          round(mean(float(data["conf"][i]) for i in idx), 1)))
    conf = round(mean(l.conf for l in lines), 1) if lines else 0.0
    return PageText(page_no, "OCR", "\n".join(l.text for l in lines), lines, conf, float(w), float(h))


def _native_page(page, page_no: int) -> PageText:
    d = page.get_text("dict")
    w, h = page.rect.width, page.rect.height
    lines: list[Line] = []
    for block in d.get("blocks", []):
        for ln in block.get("lines", []):
            text = clean_text("".join(span.get("text", "") for span in ln.get("spans", []))).strip()
            if not text:
                continue
            x0, y0, x1, y1 = ln["bbox"]
            lines.append(Line(text, [round(x0 / w, 4), round(y0 / h, 4), round(x1 / w, 4), round(y1 / h, 4)], 100.0))
    lines.sort(key=lambda l: (round(l.bbox[1], 2), l.bbox[0]) if l.bbox else (0, 0))
    return PageText(page_no, "NATIVE", "\n".join(l.text for l in lines), lines, None, w, h)


def extract_pdf(data: bytes) -> TextLayer:
    import fitz
    from PIL import Image

    s = get_settings()
    pages: list[PageText] = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        for i, page in enumerate(doc, start=1):
            native = _native_page(page, i)
            if len(native.text.strip()) >= s.native_text_min_chars:
                pages.append(native)
                continue
            pix = page.get_pixmap(dpi=s.ocr_dpi)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            pages.append(ocr_image(img, i))
    methods = {p.method for p in pages}
    method = "MIXED" if len(methods) > 1 else (methods.pop() if methods else "NATIVE")
    return TextLayer(method, pages, _mean_conf(pages))


def extract_image(data: bytes) -> TextLayer:
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    page = ocr_image(img, 1)
    return TextLayer("OCR", [page], page.ocr_confidence)


def extract_docx(data: bytes) -> TextLayer:
    import docx

    d = docx.Document(io.BytesIO(data))
    lines = [Line(clean_text(p.text).strip(), None, 100.0) for p in d.paragraphs if p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            cells = [clean_text(c.text).strip() for c in row.cells if c.text.strip()]
            if cells:
                lines.append(Line(" | ".join(cells), None, 100.0))
    return TextLayer("DOCX", [PageText(1, "DOCX", "\n".join(l.text for l in lines), lines)], None)


def extract(data: bytes, mime_type: str) -> TextLayer:
    if mime_type == "application/pdf":
        return extract_pdf(data)
    if mime_type.startswith("image/"):
        return extract_image(data)
    if mime_type.endswith("wordprocessingml.document"):
        return extract_docx(data)
    raise ValueError(f"unsupported mime type {mime_type}")


def _mean_conf(pages: list[PageText]) -> float | None:
    vals = [p.ocr_confidence for p in pages if p.ocr_confidence is not None]
    return round(mean(vals), 1) if vals else None


def render_page_png(data: bytes, mime_type: str, page_no: int, dpi: int = 110) -> bytes:
    """Render one page as PNG for the evidence viewer."""
    if mime_type.startswith("image/"):
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        img.thumbnail((1400, 1800))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG")
        return buf.getvalue()
    if mime_type == "application/pdf":
        import fitz

        with fitz.open(stream=data, filetype="pdf") as doc:
            if page_no < 1 or page_no > doc.page_count:
                raise ValueError("page out of range")
            return doc[page_no - 1].get_pixmap(dpi=dpi).tobytes("png")
    raise ValueError("page rendering is not available for this document type")
