"""Study packs: everything NOW holds for one week/lecture, bundled for reading in one go.

Pure helpers only (no I/O) so they are unit-testable; ``NowService.study_pack`` does
the orchestration.  A "unit" is the deepest Content folder a topic sits in
("Week 12: Sequential Data Structures", "Week 4 (Mobile Navigation)", "Ethics").
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlparse

WEEK_RE = re.compile(r"\b(?:week|wk|w)\s*0*(\d{1,2})\b", re.I)
NUMBERED_RE = re.compile(r"\b(lecture|lec|session|unit|activity|lab|exercise|practical|workshop)\s*0*(\d{1,2})\b", re.I)
TOKEN_RE = re.compile(r"[a-z0-9+#]+")
STOPWORDS = {"the", "a", "an", "and", "of", "to", "for", "in", "on", "with", "my", "me", "do", "lets", "let", "s", "study", "this", "that"}

# Reading order inside a pack: slides first, then the lecture page, notes, labs, readings.
KIND_ORDER = {"slides": 0, "lecture": 1, "notes": 2, "lab": 3, "reading": 4, "resource": 5, "page": 6, "recording": 7}
DOC_EXTS = {".pdf", ".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".csv", ".txt", ".md", ".cpp", ".h", ".hpp", ".c", ".java", ".py", ".json", ".sql", ".ipynb"}
ADMIN_UNITS = {"assessment", "module information", "start here", "welcome", "welcome start here", "technical guides",
               "module support resources", "revision and pre-requisites", "accessible lecture slides"}


def unit_of(entry: dict[str, Any]) -> str:
    """The deepest folder of a topic's Content path."""
    parts = [p.strip() for p in (entry.get("toc_path") or "").split(" / ") if p.strip()]
    return parts[-1] if parts else "(top level)"


def unit_week(unit: str) -> int | None:
    m = WEEK_RE.search(unit or "")
    return int(m.group(1)) if m else None


def classify_kind(entry: dict[str, Any]) -> str:
    """Rough role of a file in its week: slides / lecture / notes / lab / reading / resource / page / recording."""
    title = (entry.get("title") or "").lower()
    name = Path(entry.get("path") or "").name.lower()
    ext = Path(name).suffix
    text = f"{title} {name}"
    if "recording" in title:
        return "recording"
    if entry.get("kind") == "attachment":
        parent = (entry.get("parent_title") or "").lower()
        if ext in (".pdf", ".pptx", ".ppt") and ("lecture" in parent or "slide" in parent or "slide" in text or "lecture" in text):
            return "slides"
        return "resource"
    if "notes" in title or "pre-reading" in title or "pre reading" in title:
        return "notes"
    if ext in (".pdf", ".pptx", ".ppt") and ("lecture" in text or "slide" in text):
        return "slides"
    if "lecture" in title:
        return "lecture"
    if any(k in text for k in ("lab", "exercise", "activity", "practical", "tutorial", "workshop", "challenge", "developer resources", "worksheet")):
        return "lab"
    if any(k in text for k in ("reading", "further resources", "resources", "glossary", "cheat")):
        return "reading"
    return "page"


GENERIC_LINK_TEXT = {"here", "link", "this link", "from this link", "download", "slides", "the slides", "click here", "pdf", "file"}


def display_title(entry: dict[str, Any]) -> str:
    """A readable title: linked files named after the page that linked them, not the link's wording."""
    title = (entry.get("title") or "").strip()
    name = Path(entry.get("path") or "").name
    if entry.get("kind") == "attachment":
        parent = (entry.get("parent_title") or "").strip()
        if not title or title.lower() in GENERIC_LINK_TEXT or len(title) < 4:
            return f"{parent} — {name}" if parent else name
        return f"{parent} — {title}" if parent and parent.lower() not in title.lower() else title
    return title or name


def _tokens(s: str) -> list[str]:
    return [t for t in TOKEN_RE.findall((s or "").lower()) if t not in STOPWORDS]


def score_entry(entry: dict[str, Any], query: str | None, week: int | None) -> int:
    """How well a downloaded file matches what the student asked for (0 = not at all)."""
    unit = unit_of(entry)
    title = entry.get("title") or ""
    name = Path(entry.get("path") or "").name
    hay = f"{unit} {title} {name}".lower()
    score = 0
    uw = unit_week(unit)
    if week is not None and uw == week:
        score += 4
    if not query:
        return score
    qw = WEEK_RE.search(query)
    if qw and uw == int(qw.group(1)):
        score += 4
    for m in NUMBERED_RE.finditer(query):
        word, n = m.group(1).lower(), int(m.group(2))
        # "lecture 4" matches "Lecture 4 - ...", "lecture04-...", "Lecture 04"; "activity 3" matches "Activity 03".
        if re.search(rf"(?<![a-z]){word[:3]}[a-z]*[\s_-]*0*{n}(?!\d)", hay):
            score += 3
        elif uw == n:
            score += 1  # weekly folders numbered the same way as the lectures
    stripped = NUMBERED_RE.sub(" ", WEEK_RE.sub(" ", query))
    for tok in _tokens(stripped):
        if tok.isdigit():
            continue
        if tok in hay:
            score += 2 if tok in unit.lower() else 1
    return score


def pick_unit(entries: list[dict[str, Any]], query: str | None, week: int | None) -> tuple[str | None, dict[str, int]]:
    """Choose the unit (folder) whose files best match; returns (unit, best score per unit)."""
    totals: dict[str, int] = {}
    best: dict[str, int] = {}
    for e in entries:
        u = unit_of(e)
        s = score_entry(e, query, week)
        totals[u] = totals.get(u, 0) + s
        best[u] = max(best.get(u, 0), s)
    if not best or max(best.values()) == 0:
        return None, best
    # Highest single-file score wins; ties go to the folder with more matching files, then the earlier week.
    ranked = sorted(best, key=lambda u: (-best[u], -totals[u], unit_week(u) or 0))
    return ranked[0], best


def units_summary(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per folder: week number, how many files and of which kinds, a few titles."""
    out: dict[str, dict[str, Any]] = {}
    for e in entries:
        u = unit_of(e)
        row = out.setdefault(u, {"unit": u, "week": unit_week(u), "files": 0, "kinds": {}, "titles": []})
        row["files"] += 1
        k = classify_kind(e)
        row["kinds"][k] = row["kinds"].get(k, 0) + 1
        if len(row["titles"]) < 8:
            row["titles"].append(display_title(e))
    rows = list(out.values())
    rows.sort(key=lambda r: (r["week"] is None, r["week"] or 0, r["unit"].lower()))
    return rows


def assemble_pack(entries: list[dict[str, Any]], texts: dict[str, tuple[str, str | None]], max_chars: int) -> dict[str, Any]:
    """Order a unit's files for reading and fit as much text as the budget allows."""
    ordered = sorted(entries, key=lambda e: (KIND_ORDER.get(classify_kind(e), 9), (e.get("title") or "").lower()))
    files, later = [], []
    budget = max_chars
    total = 0
    for e in ordered:
        text, err = texts.get(e["path"], ("", "not extracted"))
        kind = classify_kind(e)
        row: dict[str, Any] = {
            "title": display_title(e),
            "kind": kind,
            "path": e["path"],
            "chars": len(text),
            "source": "linked file" if e.get("kind") == "attachment" else "topic",
        }
        if err:
            row["error"] = err
        total += len(text)
        if not text:
            files.append(row)
            continue
        if len(text) <= budget:
            row["text"] = text
            budget -= len(text)
            files.append(row)
        elif budget >= 6000:
            row["text"] = text[:budget]
            row["truncated"] = True
            row["next_offset"] = budget
            budget = 0
            files.append(row)
        else:
            later.append({"title": display_title(e), "kind": kind, "path": e["path"], "chars": len(text)})
    return {"files": files, "read_separately": later, "total_chars": total, "included_chars": max_chars - budget}


def extract_file_links(html: str, page_url: str, base_url: str, host: str) -> list[dict[str, str]]:
    """Links in a NOW HTML page that point at downloadable documents on NOW (slides, sheets, code).

    Returns encoded request paths (with query), the file name and the link text.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    if page_url and page_url.startswith("http"):
        page_abs = page_url
    else:
        page_abs = base_url.rstrip("/") + "/" + (page_url or "").lstrip("/")
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for a in soup.find_all(["a", "object", "embed", "iframe"]):
        href = (a.get("href") or a.get("data") or a.get("src") or "").strip()
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        parsed = urlparse(urljoin(page_abs, href))
        if (parsed.hostname or "") != host:
            continue
        raw_path = unquote(parsed.path)
        ext = Path(raw_path).suffix.lower()
        if ext not in DOC_EXTS:
            continue
        if raw_path in seen:
            continue
        seen.add(raw_path)
        path_q = quote(raw_path, safe="/:@!$&'()*+,;=")
        text = a.get_text(" ", strip=True) if hasattr(a, "get_text") else ""
        out.append({"url": path_q + ("?" + parsed.query if parsed.query else ""), "name": Path(raw_path).name, "text": text[:120]})
    return out
