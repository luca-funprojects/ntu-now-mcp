"""The student's teaching timetable, read from the Timetable tool embedded in NOW.

Pure parsing here (unit-testable); ``BrowserSession.timetable`` drives the page and
``NowService.timetable`` caches the result for the watcher and the study skill.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

WEEK_HEADER_RE = re.compile(r"Week\s+(\d+)\s*\((\d{2}/\d{2}/\d{2,4})\s*-\s*(\d{2}/\d{2}/\d{2,4})\)")
DAY_RE = re.compile(r"\*?\s*(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*-\s*(\d{2}/\d{2}/\d{2,4})", re.I)
MODULE_CODE_RE = re.compile(r"\(([A-Z]{3,5}\d{5})\)")
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
HEADER_WORDS = ("day", "from", "to", "room", "lecturer", "type", "module")


def _parse_date(s: str) -> date | None:
    for fmt in ("%d/%m/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_week_header(text: str) -> dict[str, Any] | None:
    m = WEEK_HEADER_RE.search(text or "")
    if not m:
        return None
    start, end = _parse_date(m.group(2)), _parse_date(m.group(3))
    return {"week": int(m.group(1)), "start": start.isoformat() if start else m.group(2), "end": end.isoformat() if end else m.group(3)}


def parse_timetable_table(rows: list[list[str]]) -> list[dict[str, Any]]:
    """Turn the timetable grid (header + rows of cell text) into one record per session."""
    if not rows:
        return []
    header = [c.strip().lower() for c in rows[0]]
    if sum(1 for w in HEADER_WORDS if w in header) < 4:
        return []
    col = {name: header.index(name) for name in header if name}
    out: list[dict[str, Any]] = []
    current_day: tuple[str, date | None] | None = None
    for r in rows[1:]:
        cells = [c.strip() for c in r] + [""] * (len(header) - len(r))
        day_cell = cells[col.get("day", 0)]
        m = DAY_RE.search(day_cell)
        if m:
            current_day = (m.group(1).capitalize(), _parse_date(m.group(2)))
        start, end = cells[col.get("from", 1)], cells[col.get("to", 2)]
        if not (TIME_RE.match(start) and TIME_RE.match(end)):
            continue  # a day with nothing on, or a spacer row
        module = cells[col["module"]] if "module" in col else ""
        code = MODULE_CODE_RE.search(module)
        day_name, day_date = current_day if current_day else ("", None)
        out.append(
            {
                "day": day_name,
                "date": day_date.isoformat() if day_date else None,
                "start": start,
                "end": end,
                "room": re.sub(r"\s*,\s*", "; ", cells[col["room"]]) if "room" in col else "",
                "lecturer": re.sub(r"\s*,\s*", "; ", cells[col["lecturer"]]) if "lecturer" in col else "",
                "type": cells[col["type"]] if "type" in col else "",
                "module": re.sub(r"\s*\([A-Z]{3,5}\d{5}\)\s*$", "", module).strip(),
                "module_code": code.group(1) if code else None,
                "group": cells[col["group"]] if "group" in col else "",
            }
        )
    return out


def upcoming(sessions: list[dict[str, Any]], now: datetime, hours: int = 48) -> list[dict[str, Any]]:
    """Sessions starting between now and now + hours, soonest first, with a local start timestamp."""
    tz = now.tzinfo or ZoneInfo("Europe/London")
    out = []
    for s in sessions:
        if not s.get("date"):
            continue
        try:
            start = datetime.combine(date.fromisoformat(s["date"]), datetime.strptime(s["start"], "%H:%M").time(), tzinfo=tz)
        except ValueError:
            continue
        if now <= start <= now + timedelta(hours=hours):
            out.append({**s, "starts_in_hours": round((start - now).total_seconds() / 3600, 1)})
    out.sort(key=lambda s: (s["date"], s["start"]))
    return out


def parse_ics(text: str) -> list[dict[str, Any]]:
    """Minimal iCalendar parser: one record per VEVENT (summary, start, end, location, description)."""
    events: list[dict[str, Any]] = []
    lines: list[str] = []
    for raw in (text or "").splitlines():
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]  # folded continuation
        else:
            lines.append(raw.rstrip("\r"))
    cur: dict[str, Any] | None = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            key, value = line.split(":", 1)
            name = key.split(";")[0].upper()
            if name in ("SUMMARY", "LOCATION", "DESCRIPTION", "DTSTART", "DTEND", "UID"):
                cur[name.lower()] = value.replace("\\,", ",").replace("\\n", "\n").strip()
    return events
