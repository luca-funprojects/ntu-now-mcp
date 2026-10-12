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


# ---------------------------------------------------------------- iCal feed → sessions

TYPE_WORDS = ("practical", "lab", "lecture", "seminar", "tutorial", "workshop", "drop-in", "drop in", "exam", "meeting")
TEACHING_WEEK1_MONDAY = date(2026, 9, 21)   # NTU week 9 == teaching week 1 of 2026/27
NTU_WEEK_OFFSET = 9


def ntu_week_of(d: date) -> int | None:
    """NTU's week number as the Timetable tool shows it (week 12 = w/c 12 Oct 2026)."""
    monday = d - timedelta(days=d.weekday())
    delta = (monday - TEACHING_WEEK1_MONDAY).days // 7
    return NTU_WEEK_OFFSET + delta if 0 <= delta < 60 else None


def _ics_datetime(value: str, params: str, tz: ZoneInfo) -> tuple[datetime | None, bool]:
    """Parse an iCal DTSTART/DTEND value into a tz-aware datetime; returns (dt, all_day)."""
    v = (value or "").strip()
    if not v:
        return None, False
    if len(v) == 8 and v.isdigit():
        return datetime.strptime(v, "%Y%m%d").replace(tzinfo=tz), True
    try:
        if v.endswith("Z"):
            dt = datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
        else:
            m = re.search(r"TZID=([^;:]+)", params or "")
            src = ZoneInfo(m.group(1)) if m else tz
            dt = datetime.strptime(v, "%Y%m%dT%H%M%S").replace(tzinfo=src).astimezone(tz)
    except (ValueError, KeyError):
        return None, False
    return dt, False


def parse_ics_full(text: str) -> list[dict[str, Any]]:
    """Like parse_ics but keeps each property's parameters (needed for TZID)."""
    events: list[dict[str, Any]] = []
    lines: list[str] = []
    for raw in (text or "").splitlines():
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
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
            name, _, params = key.partition(";")
            name = name.upper()
            if name in ("SUMMARY", "LOCATION", "DESCRIPTION", "DTSTART", "DTEND", "UID"):
                cur[name.lower()] = value.replace("\\,", ",").replace("\\;", ";").replace("\\n", "\n").strip()
                if params:
                    cur[name.lower() + "_params"] = params
    return events


def _field(description: str, label: str) -> str:
    m = re.search(rf"(?:{label})\s*[:\-]\s*(.+)", description or "", re.I)
    return m.group(1).strip() if m else ""


def ics_events_to_sessions(events: list[dict[str, Any]], tz: ZoneInfo) -> list[dict[str, Any]]:
    """Shape iCal events like the Timetable grid's rows so both sources look the same downstream."""
    out: list[dict[str, Any]] = []
    for ev in events:
        start, all_day = _ics_datetime(ev.get("dtstart", ""), ev.get("dtstart_params", ""), tz)
        end, _ = _ics_datetime(ev.get("dtend", ""), ev.get("dtend_params", ""), tz)
        if start is None:
            continue
        summary = ev.get("summary", "")
        desc = ev.get("description", "")
        blob = f"{summary} {desc}"
        code = MODULE_CODE_RE.search(blob)
        kind = next((w for w in TYPE_WORDS if w in blob.lower()), "")
        module = re.sub(r"\s*\([A-Z]{3,5}\d{5}\)\s*", " ", summary)
        module = re.split(r"\s+[-–]\s+", module)[0].strip() if module else summary
        out.append(
            {
                "day": start.strftime("%A"),
                "date": start.date().isoformat(),
                "start": "all day" if all_day else start.strftime("%H:%M"),
                "end": "" if all_day or end is None else end.strftime("%H:%M"),
                "room": ev.get("location", ""),
                "lecturer": _field(desc, "Lecturer|Staff|Tutor"),
                "type": _field(desc, "Type|Activity") or (kind.capitalize() if kind else ""),
                "module": module,
                "module_code": code.group(1) if code else None,
                "group": _field(desc, "Group"),
                "summary": summary,
                "description": desc[:300],
            }
        )
    out.sort(key=lambda s: (s["date"], s["start"]))
    return out


def sessions_to_weeks(sessions: list[dict[str, Any]], from_day: date, weeks: int) -> list[dict[str, Any]]:
    """Group sessions into Monday-based weeks from the week containing ``from_day``, ``weeks`` weeks long."""
    monday = from_day - timedelta(days=from_day.weekday())
    out = []
    for i in range(max(1, weeks)):
        start = monday + timedelta(days=7 * i)
        end = start + timedelta(days=6)
        rows = [s for s in sessions if s.get("date") and start.isoformat() <= s["date"] <= end.isoformat()]
        out.append({"week": ntu_week_of(start), "start": start.isoformat(), "end": end.isoformat(), "sessions": rows})
    return out
