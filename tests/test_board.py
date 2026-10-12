"""The Study Board served by the connector: page + JSON API on the secret path."""

from __future__ import annotations

import json
import os
from pathlib import Path


def test_board_page_and_api(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOW_STATE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("NOW_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("NOW_HTTP_SECRET", "s3cret")
    monkeypatch.setenv("NOW_PUBLIC_HOST", "")
    (tmp_path / "s").mkdir(); (tmp_path / "d").mkdir()
    import importlib
    import now_mcp.server as srv
    srv = importlib.reload(srv)
    from starlette.testclient import TestClient

    # A module with one downloaded unit and a cached timetable, no NOW access needed.
    svc = srv.svc
    svc._modules = {1174484: {"ou": 1174484, "code": "SOFT30161_X", "name": "ASE", "is_active": True, "can_access": True, "pinned": False}}
    svc._modules_at = 10**12
    f = tmp_path / "d" / "lec.html"; f.write_text("x")
    (tmp_path / "d" / "_index.json").write_text(json.dumps({"1174484:1": {"path": str(f), "ou": 1174484, "title": "Lecture: TDD", "toc_path": "Week 9: Test-driven Development"}}))
    (tmp_path / "d" / "_timetable.json").write_text(json.dumps({"fetched_at": "2099-01-01T00:00:00+00:00", "source": "ical", "ical_url": "https://services.ntu.ac.uk/x.ics", "weeks": [], "all_sessions": [
        {"day": "Tuesday", "date": "2026-09-22", "start": "13:00", "end": "14:00", "room": "PAV121", "type": "Subject lecture", "lecturer": "N", "module_code": "SOFT30161", "group": ""}]}))

    app = srv.build_http_app()
    with TestClient(app) as c:
        assert c.get("/healthz").json()["ok"] is True
        page = c.get("/s3cret/board")
        assert page.status_code == 200 and "Study Board" in page.text and "api/" in page.text
        assert c.get("/wrong/board").status_code == 404

        plan = c.get("/s3cret/api/plan/1174484").json()
        assert plan["module_code"] == "SOFT30161_X" and plan["units"][0]["week"] == 9
        assert plan["units"][0]["sessions"][0]["room"] == "PAV121" and plan["units"][0]["status"] == "not_started"

        r = c.post("/s3cret/api/log", json={"ou": 1174484, "unit": "Week 9: Test-driven Development", "status": "done", "note": "ok"})
        assert r.status_code == 200 and r.json()["logged"]["status"] == "done"
        assert c.get("/s3cret/api/plan/1174484").json()["progress"] == {"done": 1, "total": 1}
        assert c.get("/s3cret/api/study-log").json()["count"] == 1
        bad = c.post("/s3cret/api/log", json={"ou": 1174484, "unit": "Week 9: Test-driven Development", "status": "finished"})
        assert bad.status_code == 400 and bad.json()["error"] == "bad_request"

        tt = c.get("/s3cret/api/timetable?hours=72").json()
        assert tt["source"] == "ical" and "upcoming" in tt and "all_sessions" not in tt


def test_files_route_and_now_links(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOW_STATE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("NOW_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("NOW_HTTP_SECRET", "s3cret")
    monkeypatch.setenv("NOW_PUBLIC_HOST", "")
    (tmp_path / "s").mkdir(); (tmp_path / "d").mkdir()
    import importlib
    import now_mcp.server as srv
    srv = importlib.reload(srv)
    from starlette.testclient import TestClient

    svc = srv.svc
    svc._modules = {1174484: {"ou": 1174484, "code": "SOFT30161_X", "name": "ASE", "is_active": True, "can_access": True, "pinned": False}}
    svc._modules_at = 10**12
    week = tmp_path / "d" / "SOFT30161_X" / "Week 12_ Sequential Data Structures"; week.mkdir(parents=True)
    page = week / "14935650_Lecture Standard Containers.html"; page.write_text("<html><head><title>L</title></head><body><img src='/content/enforced/x/pic.png'></body></html>")
    deck = week / "14935650_att_lecture04.pdf"; deck.write_bytes(b"%PDF-1.4 fake")
    (tmp_path / "d" / "_index.json").write_text(json.dumps({
        "1174484:14935650": {"path": str(page), "ou": 1174484, "title": "Lecture: Efficient Sequential Data Structures", "toc_path": "Week 12: Sequential Data Structures"},
        "1174484:14935650:lecture04.pdf": {"path": str(deck), "ou": 1174484, "title": "from this link", "toc_path": "Week 12: Sequential Data Structures", "kind": "attachment",
                                            "parent_title": "Lecture: Efficient Sequential Data Structures", "source_url": "/content/enforced/x/slides/lecture04.pdf?isCourseFile=true"},
    }))
    app = srv.build_http_app()
    with TestClient(app) as c:
        plan = c.get("/s3cret/api/plan/1174484").json()
        files = plan["units"][0]["files"]
        assert files[0]["kind"] == "slides" and files[0]["now_url"] == "https://now.ntu.ac.uk/content/enforced/x/slides/lecture04.pdf?isCourseFile=true"
        assert files[1]["now_url"] == "https://now.ntu.ac.uk/d2l/le/content/1174484/viewContent/14935650/View"
        assert files[1]["rel_path"] == "SOFT30161_X/Week 12_ Sequential Data Structures/14935650_Lecture Standard Containers.html"

        r = c.get("/s3cret/files/" + files[0]["rel_path"])
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
        h = c.get("/s3cret/files/" + files[1]["rel_path"].replace(" ", "%20"))
        assert h.status_code == 200 and '<base href="https://now.ntu.ac.uk/">' in h.text
        assert c.get("/s3cret/files/_index.json").status_code == 404
        assert c.get("/s3cret/files/../../etc/passwd").status_code in (400, 404)
