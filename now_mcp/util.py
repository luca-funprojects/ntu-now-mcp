"""Small helpers: dates, names, text."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


def parse_iso(value: Any) -> datetime | None:
    """Parse Brightspace's ISO-8601 UTC timestamps ('2026-10-30T12:00:00.000Z')."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def fmt_local(value: Any, tz: ZoneInfo) -> str | None:
    dt = value if isinstance(value, datetime) else parse_iso(value)
    if dt is None:
        return None
    return dt.astimezone(tz).strftime("%a %d %b %Y, %H:%M")


def days_until(value: Any, now: datetime | None = None) -> float | None:
    dt = value if isinstance(value, datetime) else parse_iso(value)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return round((dt - now).total_seconds() / 86400, 1)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def window(days_back: int, days_ahead: int) -> tuple[datetime, datetime]:
    now = utcnow()
    return now - timedelta(days=days_back), now + timedelta(days=days_ahead)


_SAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_name(name: str, limit: int = 120) -> str:
    """Make a string safe to use as a file or directory name."""
    name = (name or "").strip()
    name = _SAFE.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" ._") or "untitled"
    return name[:limit]


def norm(s: str) -> str:
    """Normalise a title for fuzzy comparisons."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def text_of(block: Any) -> str:
    """Brightspace RichText blocks are {"Text": ..., "Html": ...}, sometimes nested one level deeper
    ({"Text": {"Text": ..., "Html": ...}, "IsDisplayed": ...} on quizzes)."""
    if isinstance(block, dict):
        inner = block.get("Text")
        if isinstance(inner, dict):
            return text_of(inner)
        if isinstance(inner, str):
            return inner.strip()
        html = block.get("Html")
        return html.strip() if isinstance(html, str) else ""
    if isinstance(block, str):
        return block.strip()
    return ""


def clip(s: str | None, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"
