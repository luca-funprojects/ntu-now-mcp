"""Turn downloaded module files into text so they can be read and searched."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".py", ".java", ".js", ".ts", ".c", ".cpp", ".h", ".sql", ".xml", ".yaml", ".yml"}
HTML_EXTS = {".html", ".htm", ".xhtml"}
SKIP_EXTS = {".mp4", ".mp3", ".m4a", ".mov", ".avi", ".wav", ".zip", ".rar", ".7z", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".exe", ".dmg"}


def html_to_text(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    parts: list[str] = []
    for i, page in enumerate(reader.pages, 1):
        try:
            t = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            t = ""
        parts.append(f"\n--- page {i} ---\n{t.strip()}")
    return "\n".join(parts).strip()


def _docx_text(path: Path) -> str:
    import docx

    d = docx.Document(str(path))
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts).strip()


def _pptx_text(path: Path) -> str:
    from pptx import Presentation

    prs = Presentation(str(path))
    parts: list[str] = []
    for i, slide in enumerate(prs.slides, 1):
        lines: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    t = "".join(run.text for run in para.runs).strip()
                    if t:
                        lines.append(t)
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells]
                    if any(cells):
                        lines.append(" | ".join(cells))
        notes = ""
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
        block = f"\n--- slide {i} ---\n" + "\n".join(lines)
        if notes:
            block += f"\n[notes] {notes}"
        parts.append(block)
    return "\n".join(parts).strip()


def extract_text(path: Path) -> tuple[str, str | None]:
    """Return (text, error).  Unsupported types return ('', reason)."""
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            return _pdf_text(path), None
        if ext == ".docx":
            return _docx_text(path), None
        if ext == ".pptx":
            return _pptx_text(path), None
        if ext in HTML_EXTS:
            return html_to_text(path.read_text(errors="replace")), None
        if ext in TEXT_EXTS:
            return path.read_text(errors="replace"), None
        if ext in SKIP_EXTS:
            return "", f"{ext} files are not text (media/archive/image)"
        # Unknown: try as text if it decodes cleanly.
        raw = path.read_bytes()[:200000]
        try:
            return raw.decode("utf-8"), None
        except UnicodeDecodeError:
            return "", f"unsupported file type {ext or '(none)'}"
    except Exception as exc:  # noqa: BLE001
        return "", f"extraction failed: {exc.__class__.__name__}: {str(exc)[:120]}"


class TextCache:
    """Caches extracted text next to the data dir so repeat reads and searches are instant."""

    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "_text_cache"
        self.root.mkdir(parents=True, exist_ok=True)

    def _key(self, path: Path) -> Path:
        st = path.stat()
        h = hashlib.sha1(f"{path.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()
        return self.root / f"{h}.json"

    def get(self, path: Path) -> tuple[str, str | None]:
        key = self._key(path)
        if key.is_file():
            data = json.loads(key.read_text())
            return data.get("text", ""), data.get("error")
        text, err = extract_text(path)
        key.write_text(json.dumps({"path": str(path), "text": text, "error": err}))
        return text, err


def snippets(text: str, query: str, width: int = 160, max_hits: int = 5) -> list[str]:
    out: list[str] = []
    low = text.lower()
    q = query.lower()
    start = 0
    while len(out) < max_hits:
        i = low.find(q, start)
        if i < 0:
            break
        a = max(0, i - width // 2)
        b = min(len(text), i + len(q) + width // 2)
        out.append(re.sub(r"\s+", " ", text[a:b]).strip())
        start = i + len(q)
    return out
