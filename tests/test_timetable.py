"""Timetable parsing, the frame-aware browser and the timetable driver (against local fixture pages)."""

from __future__ import annotations

import asyncio
import http.server
import json
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from now_mcp.config import Config
from now_mcp.timetable import parse_ics, parse_timetable_table, parse_week_header, upcoming

TZ = ZoneInfo("Europe/London")

# Rows exactly as the NOW Timetable tool renders week 12 (header + one row per session; empty days keep a row).
WEEK12 = [
    ["Day", "From", "To", "Room", "Meeting", "Lecturer", "Type", "Module", "Group"],
    ["* Monday - 12/10/26 *", "12:00", "13:00", "(Clifton) Clifton Teaching & Learning Centre LT3 Lecture Theatre 3", "", "Arshad Sher", "Subject lecture", "Adv Analysis & Design (SOFT30121)", ""],
    ["* Monday - 12/10/26 *", "16:00", "17:00", "(Clifton) John Clare Lecture Theatres 005 Lecture Theatre 5", "", "Thomas Johnson", "Subject lecture", "Mobile Platform Applications (ITEC31041)", ""],
    ["Tuesday - 13/10/26", "10:00", "11:00", "(Clifton) Clifton Teaching & Learning Centre LT3 Lecture Theatre 3", "", "Ismahane Cheheb", "Subject lecture", "Service-Centric & Cloud Comp (COMP30231)", ""],
    ["Tuesday - 13/10/26", "11:00", "13:00", "(Clifton) Mary Ann Evans 206 IT Room", "", "Thomas Johnson , Arshad Sher", "Practical / Lab", "Mobile Platform Applications (ITEC31041)", "SE_1"],
    ["Tuesday - 13/10/26", "13:00", "14:00", "(Clifton) Pavilion 121 , (Clifton) Pavilion 122 , (Clifton) Pavilion 123", "", "Neil Sculthorpe", "Subject lecture", "Advanced Software Engineering (SOFT30161)", ""],
    ["Tuesday - 13/10/26", "18:00", "19:00", "(Clifton) ISTEC 030 Mobile and App Development Laboratory", "", "Neil Sculthorpe", "Practical / Lab", "Advanced Software Engineering (SOFT30161)", ""],
    ["Wednesday - 14/10/26", "", "", "", "", "", "", "", ""],
    ["Thursday - 15/10/26", "13:00", "15:00", "(Clifton) ISTEC 029 Games Development Laboratory", "", "Taha Osman", "Practical / Lab", "Service-Centric & Cloud Comp (COMP30231)", ""],
    ["Thursday - 15/10/26", "16:00", "17:00", "(Clifton) Clifton Teaching & Learning Centre LT3 Lecture Theatre 3", "", "Amin Safaei", "Subject lecture", "Project (COMP30151)", ""],
    ["Friday - 16/10/26", "09:00", "11:00", "(Clifton) Mary Ann Evans 203 IT Room", "", "Selwan Abdussalam", "Practical / Lab", "Adv Analysis & Design (SOFT30121)", ""],
    ["Saturday - 17/10/26", "", "", "", "", "", "", "", ""],
    ["Sunday - 18/10/26", "", "", "", "", "", "", "", ""],
    ["Duration total: 12 hours, 0 minutes"],
]


def test_parse_week_header():
    h = parse_week_header("Weekly timetable for my modules : Week 12 (12/10/26 - 18/10/26)")
    assert h == {"week": 12, "start": "2026-10-12", "end": "2026-10-18"}
    assert parse_week_header("nothing here") is None


def test_parse_timetable_table_rows():
    sessions = parse_timetable_table(WEEK12)
    assert len(sessions) == 9
    assert sessions[0] == {
        "day": "Monday", "date": "2026-10-12", "start": "12:00", "end": "13:00",
        "room": "(Clifton) Clifton Teaching & Learning Centre LT3 Lecture Theatre 3", "lecturer": "Arshad Sher",
        "type": "Subject lecture", "module": "Adv Analysis & Design", "module_code": "SOFT30121", "group": "",
    }
    tue_lab = sessions[3]
    assert tue_lab["lecturer"] == "Thomas Johnson; Arshad Sher" and tue_lab["group"] == "SE_1" and tue_lab["end"] == "13:00"
    assert sessions[4]["room"] == "(Clifton) Pavilion 121; (Clifton) Pavilion 122; (Clifton) Pavilion 123"
    assert [s["day"] for s in sessions].count("Wednesday") == 0          # nothing on Wednesdays
    assert sessions[7]["module_code"] == "COMP30151" and sessions[7]["day"] == "Thursday"
    assert parse_timetable_table([["Name", "Email"], ["x", "y"]]) == []   # not a timetable
    assert parse_timetable_table([]) == []


def test_upcoming_window():
    sessions = parse_timetable_table(WEEK12)
    now = datetime(2026, 10, 12, 17, 30, tzinfo=TZ)       # Monday evening
    nxt = upcoming(sessions, now, hours=24)
    assert [(s["start"], s["module_code"]) for s in nxt] == [("10:00", "COMP30231"), ("11:00", "ITEC31041"), ("13:00", "SOFT30161")]
    assert nxt[0]["starts_in_hours"] == 16.5
    assert upcoming(sessions, datetime(2026, 10, 17, 9, 0, tzinfo=TZ), hours=48) == []


def test_parse_ics_folding_and_fields():
    ics = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:1\r\nDTSTART;TZID=Europe/London:20261013T100000\r\nDTEND;TZID=Europe/London:20261013T110000\r\n"
        "SUMMARY:Service-Centric & Cloud Comp (COMP30231) - Subject lec\r\n ture\r\nLOCATION:(Clifton) CTLC LT3\\, Clifton\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    ev = parse_ics(ics)
    assert len(ev) == 1
    assert ev[0]["summary"] == "Service-Centric & Cloud Comp (COMP30231) - Subject lecture"
    assert ev[0]["location"] == "(Clifton) CTLC LT3, Clifton" and ev[0]["dtstart"] == "20261013T100000"


# ------------------------------------------------------------------ browser (local fixture pages)

def _table_html(rows):
    return "<table>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) + "</table>"


def _week_page(week: int, nxt: int) -> str:
    rows = [r for r in WEEK12]
    if week != 12:
        rows = [WEEK12[0]] + [[c.replace("/10/26", "/10/26") for c in r] for r in WEEK12[1:3]]  # shorter second week
    return f"""<html><body>
    <form method='get' action='week{nxt}.html'>Select a week to view <select name='w'><option>12/10/2026 - 18/10/2026 : Week 12</option></select>
    <input type='submit' value='Update timetable'> <input type='submit' value='Previous week'> <input type='submit' value='Next week'></form>
    <h2>Weekly timetable for my modules : Week {week} ({12 + (week - 12) * 7}/10/26 - {18 + (week - 12) * 7}/10/26)</h2>
    <a href='/tt/feed.ics'>Subscribe to iCal</a> - allows you to access your timetable
    {_table_html(rows)}</body></html>"""


PAGES = {
    "/d2l/home": "<html><body><nav><a href='/d2l/lp/navbars/6605/customlinks/external/15376'>Timetable</a><a href='/x'>Discover</a></nav>Luca Georgiou</body></html>",
    "/d2l/lp/navbars/6605/customlinks/external/15376": "<html><head><title>Timetable - NTU Online Workspace</title></head><body>Luca Georgiou Timetable My Tools<iframe src='/tt/week12.html' width='1000' height='800'></iframe></body></html>",
    "/tt/week12.html": _week_page(12, 13),
    "/tt/week13.html": _week_page(13, 14),
    "/tt/feed.ics": "BEGIN:VCALENDAR\nEND:VCALENDAR\n",
}


class _Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        body = PAGES.get(path)
        if body is None:
            self.send_response(404); self.end_headers(); return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/calendar" if path.endswith(".ics") else "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def site():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _chromium_available(), reason="playwright not installed")
def test_browser_reads_frames_and_timetable(site, tmp_path: Path):
    from now_mcp.browser import BrowserSession
    from now_mcp.service import NowService

    cfg = Config(base_url=site, state_dir=tmp_path / "s", data_dir=tmp_path / "d")
    cfg.state_dir.mkdir(); cfg.data_dir.mkdir()

    async def noop():
        return None

    async def drive():
        b = BrowserSession(cfg, noop)
        try:
            page = await b.browse("/d2l/lp/navbars/6605/customlinks/external/15376")
            assert page["frames"] and "Weekly timetable" in page["text"]           # frame text surfaced as the page text
            assert page["frames"][0]["tables"][0][0][0] == "Day" and "note" in page
            assert any(l["text"] == "Subscribe to iCal" for l in page["frames"][0]["links"])

            clicked = await b.click("Next week")                                   # clicking inside the frame works
            assert "Week 13" in clicked["text"]

            raw = await b.timetable(path=None, weeks_ahead=1)                      # discovers the navbar link itself
            assert raw["path"].endswith("/customlinks/external/15376")
            assert raw["ical_url"].endswith("/tt/feed.ics")
            assert [w["header"] for w in raw["weeks"]] == ["Week 12 (12/10/26 - 18/10/26)", "Week 13 (19/10/26 - 25/10/26)"]
            return raw
        finally:
            await b.close()

    raw = asyncio.run(drive())

    svc = NowService(cfg, client=None)
    data = svc.timetable_from_raw(raw)
    assert [w["week"] for w in data["weeks"]] == [12, 13]
    assert len(data["weeks"][0]["sessions"]) == 9 and len(data["weeks"][1]["sessions"]) == 2
    assert json.loads((cfg.data_dir / "_timetable.json").read_text())["ical_url"] == raw["ical_url"]
    assert svc.cached_timetable() is not None
    view = svc.timetable_view(data, hours_ahead=48)
    assert view["weeks"] and "upcoming" in view and view["ical_url"]
    with pytest.raises(ValueError):
        asyncio.run(svc.fetch_ics("https://example.com/feed.ics"))               # only NTU hosts


# ------------------------------------------------------------------ iCal feed route

FEED = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:a1
DTSTART;TZID=Europe/London:20261013T100000
DTEND;TZID=Europe/London:20261013T110000
SUMMARY:Service-Centric & Cloud Comp (COMP30231) - Subject lecture
LOCATION:(Clifton) Clifton Teaching & Learning Centre LT3 Lecture Theatre 3
DESCRIPTION:Lecturer: Ismahane Cheheb\\nType: Subject lecture\\nGroup: 
END:VEVENT
BEGIN:VEVENT
UID:a2
DTSTART:20261013T100000Z
DTEND:20261013T120000Z
SUMMARY:Mobile Platform Applications (ITEC31041) - Practical / Lab
LOCATION:(Clifton) Mary Ann Evans 206 IT Room
DESCRIPTION:Lecturer: Thomas Johnson\\, Arshad Sher\\nGroup: SE_1
END:VEVENT
BEGIN:VEVENT
UID:a3
DTSTART;VALUE=DATE:20261023
SUMMARY:PPD deadline
END:VEVENT
BEGIN:VEVENT
UID:a4
DTSTART;TZID=Europe/London:20261020T130000
DTEND;TZID=Europe/London:20261020T140000
SUMMARY:Advanced Software Engineering (SOFT30161) - Subject lecture
LOCATION:(Clifton) Pavilion 121
END:VEVENT
END:VCALENDAR
"""


def test_ics_feed_becomes_sessions_and_weeks(tmp_path: Path):
    from datetime import date
    from now_mcp.service import NowService
    from now_mcp.timetable import ics_events_to_sessions, ntu_week_of, parse_ics_full, sessions_to_weeks

    events = parse_ics_full(FEED)
    assert len(events) == 4 and events[0]["dtstart_params"] == "TZID=Europe/London"
    sessions = ics_events_to_sessions(events, TZ)
    assert [s["module_code"] for s in sessions] == ["COMP30231", "ITEC31041", "SOFT30161", None]   # sorted by date
    lec = sessions[0]
    assert (lec["day"], lec["date"], lec["start"], lec["end"]) == ("Tuesday", "2026-10-13", "10:00", "11:00")
    assert lec["lecturer"] == "Ismahane Cheheb" and lec["type"] == "Subject lecture" and lec["module"] == "Service-Centric & Cloud Comp"
    lab = sessions[1]
    assert lab["start"] == "11:00" and lab["end"] == "13:00"              # UTC converted to BST
    assert lab["lecturer"] == "Thomas Johnson, Arshad Sher" and lab["group"] == "SE_1"
    assert sessions[3]["start"] == "all day" and sessions[3]["module"] == "PPD deadline"
    assert ntu_week_of(date(2026, 10, 12)) == 12 and ntu_week_of(date(2026, 10, 18)) == 12 and ntu_week_of(date(2026, 9, 21)) == 9

    weeks = sessions_to_weeks(sessions, date(2026, 10, 12), 2)
    assert [w["week"] for w in weeks] == [12, 13]
    assert len(weeks[0]["sessions"]) == 2 and len(weeks[1]["sessions"]) == 2

    cfg = Config(state_dir=tmp_path / "s", data_dir=tmp_path / "d")
    cfg.state_dir.mkdir(); cfg.data_dir.mkdir()
    (cfg.data_dir / "_timetable.ics").write_text(FEED)
    svc = NowService(cfg, client=None)
    data = svc.timetable_from_ics("https://services.ntu.ac.uk/x.ics", weeks_ahead=1)
    assert data["source"] == "ical" and data["ical"]["events"] == 4 and len(data["weeks"]) == 2
    view = svc.timetable_view(data, hours_ahead=24)
    assert view["sessions_known_until"] == "2026-10-23" and view["ical_url"].endswith("x.ics")
