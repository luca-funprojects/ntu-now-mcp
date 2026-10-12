"""Study packs, link following and the download index (concurrency, self-healing)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from now_mcp.config import Config
from now_mcp.service import NowService
from now_mcp.study import (
    assemble_pack,
    classify_kind,
    extract_file_links,
    pick_unit,
    score_entry,
    unit_of,
    unit_week,
    units_summary,
)

ASE = [  # shaped like the real SOFT30161 index entries
    {"path": "/d/ASE/Week 12_ Sequential Data Structures/14935650_Lecture Standard Containers.html", "ou": 1, "title": "Lecture: Efficient Sequential Data Structures", "toc_path": "Week 12: Sequential Data Structures"},
    {"path": "/d/ASE/Week 12_ Sequential Data Structures/14935650_att_lecture04-Efficient_Sequential_Data_Structures.pdf", "ou": 1, "title": "this link", "toc_path": "Week 12: Sequential Data Structures", "kind": "attachment", "parent_title": "Lecture: Efficient Sequential Data Structures"},
    {"path": "/d/ASE/Week 12_ Sequential Data Structures/14935651_Pre-reading Standard Library Containers.html", "ou": 1, "title": "Lecture Notes: Standard Sequence Containers", "toc_path": "Week 12: Sequential Data Structures"},
    {"path": "/d/ASE/Week 12_ Sequential Data Structures/14935636_Coding Exercise Singly Linked List.html", "ou": 1, "title": "Exercise [Advanced]: Building a Singly Linked List", "toc_path": "Week 12: Sequential Data Structures"},
    {"path": "/d/ASE/Week 11_ Storing Data in Memory/14935638_Lecture 01 Data Structures.html", "ou": 1, "title": "Lecture: Storing Data in Memory", "toc_path": "Week 11: Storing Data in Memory"},
    {"path": "/d/ASE/Week 11_ Storing Data in Memory/14935638_att_lecture03-Storing_Data_in_Memory.pdf", "ou": 1, "title": "this link", "toc_path": "Week 11: Storing Data in Memory", "kind": "attachment", "parent_title": "Lecture: Storing Data in Memory"},
    {"path": "/d/ASE/Assessment/14641340_01 Assessment Details.html", "ou": 1, "title": "Assessment Overview", "toc_path": "Assessment"},
]
MPA = [
    {"path": "/d/MPA/Term 1/Week 4 _Mobile Navigation/Lecture 4 - Navigation_ Android Fragments _ SwiftUI Views - Copy.pdf", "ou": 2, "title": "Lecture 4 - Navigation, Android Fragments & SwiftUI Views ", "toc_path": "Term 1 - Weekly Resources / Week 4 (Mobile Navigation)"},
    {"path": "/d/MPA/Term 1/Week 4 _Mobile Navigation/Android Developer Resources - Copy _2_.html", "ou": 2, "title": "Android Developer Resources", "toc_path": "Term 1 - Weekly Resources / Week 4 (Mobile Navigation)"},
    {"path": "/d/MPA/Term 1/Week 3 _Life Cycle/Lecture 3 - Android_ iOS life cycles.pdf", "ou": 2, "title": "Lecture 3 - Android, iOS life cycles & UI Layout - Lecture 2(26-27)", "toc_path": "Term 1 - Weekly Resources / Week 3 (Introduction to Android & iOS Life Cycles and UI Layout)"},
]
SCCC = [
    {"path": "/d/SCCC/Week 3_ RESTful Services Fundamentals/15243208_COMP30231_04.pptx", "ou": 3, "title": "[Lecture Slides] Modern APIs and REST Fundamentals", "toc_path": "Week 3: RESTful Services Fundamentals"},
    {"path": "/d/SCCC/Week 3_ RESTful Services Fundamentals/15143369_Activity 03 Build a RESTful service with SpringBoot.html", "ou": 3, "title": "Activity 03: Build a RESTful service with SpringBoot", "toc_path": "Week 3: RESTful Services Fundamentals"},
    {"path": "/d/SCCC/Week 2_ Service Oriented Architecture/15228801_COMP30231_02_2026.pptx", "ou": 3, "title": "[Lecture Slides] Service-Oriented Architecture", "toc_path": "Week 2: Service Oriented Architecture"},
    {"path": "/d/SCCC/Week 2_ Service Oriented Architecture/14986034_Activity 02 Writing a client for a web service.html", "ou": 3, "title": "Activity 02: Consuming a Remote Web Service and Handling JSON Arrays and Nested Structures", "toc_path": "Week 2: Service Oriented Architecture"},
]


def test_unit_and_week_parsing():
    assert unit_of(MPA[0]) == "Week 4 (Mobile Navigation)"
    assert unit_week("Week 12: Sequential Data Structures") == 12
    assert unit_week("Week 4 (Mobile Navigation)") == 4
    assert unit_week("Assessment") is None
    assert unit_week("Wk 3 - Requirements") == 3


def test_classify_kind():
    kinds = [classify_kind(e) for e in ASE]
    assert kinds[:4] == ["lecture", "slides", "notes", "lab"]
    assert classify_kind(MPA[0]) == "slides"
    assert classify_kind(MPA[1]) == "lab"          # "Developer Resources" pages are the MPA lab sheets
    assert classify_kind(SCCC[1]) == "lab"         # Activity 03
    assert classify_kind({"path": "/x/15233577_html", "title": "[Lecture Recording] SOA", "toc_path": "Week 2"}) == "recording"


def test_pick_unit_by_lecture_number_week_number_and_words():
    assert pick_unit(ASE, "lecture 4", None)[0] == "Week 12: Sequential Data Structures"   # slides are lecture04-*.pdf
    assert pick_unit(ASE, "lecture 3", None)[0] == "Week 11: Storing Data in Memory"
    assert pick_unit(ASE, "week 12", None)[0] == "Week 12: Sequential Data Structures"
    assert pick_unit(ASE, None, 11)[0] == "Week 11: Storing Data in Memory"
    assert pick_unit(ASE, "sequential data structures", None)[0] == "Week 12: Sequential Data Structures"
    assert pick_unit(ASE, "linked list exercise", None)[0] == "Week 12: Sequential Data Structures"
    assert pick_unit(MPA, "lecture 4", None)[0] == "Week 4 (Mobile Navigation)"
    assert pick_unit(MPA, "fragments", None)[0] == "Week 4 (Mobile Navigation)"
    assert pick_unit(SCCC, "activity 03", None)[0] == "Week 3: RESTful Services Fundamentals"
    assert pick_unit(SCCC, "activity 2", None)[0] == "Week 2: Service Oriented Architecture"
    # SCCC decks are numbered one ahead of the week (deck _04 is week 3): "lecture 3" falls back to week 3.
    assert pick_unit(SCCC, "lecture 3", None)[0] == "Week 3: RESTful Services Fundamentals"
    assert pick_unit(ASE, "quantum chromodynamics", None)[0] is None
    assert score_entry(ASE[6], "assessment", None) > 0


def test_units_summary_orders_weeks_first():
    rows = units_summary(ASE)
    assert [r["unit"] for r in rows] == ["Week 11: Storing Data in Memory", "Week 12: Sequential Data Structures", "Assessment"]
    assert rows[1]["files"] == 4 and rows[1]["kinds"]["slides"] == 1


def test_assemble_pack_orders_and_budgets():
    chosen = [e for e in ASE if unit_of(e).startswith("Week 12")]
    texts = {e["path"]: ("x" * n, None) for e, n in zip(chosen, [400, 5000, 3000, 9000])}
    pack = assemble_pack(chosen, texts, max_chars=9000)
    kinds = [f["kind"] for f in pack["files"]]
    assert kinds[:3] == ["slides", "lecture", "notes"]
    assert all("text" in f for f in pack["files"][:3])
    assert pack["read_separately"] and pack["read_separately"][0]["kind"] == "lab"
    assert pack["total_chars"] == 17400
    big = assemble_pack(chosen, texts, max_chars=50000)
    assert not big["read_separately"] and big["included_chars"] == 17400


def test_extract_file_links_resolves_relative_and_filters():
    html = """
    <p>The lecture slides are available <a href="../../slides/lecture04-Efficient_Sequential_Data_Structures.pdf?isCourseFile=true">from this link</a>.
    I recommend this week's <a href="Pre-reading.html">lecture notes</a>.</p>
    <a href="https://ntu.cloud.panopto.eu/Panopto/Pages/Viewer.aspx?id=1">Recording</a>
    <a href="#top">Go to top</a>
    <a href="/content/enforced/1174484-SOFT30161/code/Week%2012%20Starter.cpp">starter</a>
    <a href="/content/enforced/1174484-SOFT30161/code/Week%2012%20Starter.cpp">again</a>
    """
    page = "/content/enforced/1174484-SOFT30161/!TEMPLATE FILES/05 Week 1/Lecture Standard Containers.html"
    links = extract_file_links(html, page, "https://now.ntu.ac.uk", "now.ntu.ac.uk")
    assert [l["name"] for l in links] == ["lecture04-Efficient_Sequential_Data_Structures.pdf", "Week 12 Starter.cpp"]
    assert links[0]["url"] == "/content/enforced/1174484-SOFT30161/slides/lecture04-Efficient_Sequential_Data_Structures.pdf?isCourseFile=true"
    assert links[0]["text"] == "from this link"
    assert links[1]["url"] == "/content/enforced/1174484-SOFT30161/code/Week%2012%20Starter.cpp"


# ---------------------------------------------------------------- service level

STUB_HTML = (
    "<html><body><h1>Lecture: Efficient Sequential Data Structures</h1>"
    "<p>The lecture slides are available <a href='../slides/lecture04-Efficient.pdf?isCourseFile=true'>from this link</a>.</p></body></html>"
)


class FakeClient:
    """Serves a two-topic module: an HTML stub that links to a PDF, and a lab sheet."""

    def __init__(self) -> None:
        self.fetched: list[str] = []

    async def toc(self, ou):
        return {"Modules": [{"ModuleId": 1, "Title": "Week 12: Sequential Data Structures", "Topics": [
            {"TopicId": 50, "Title": "Lecture: Efficient Sequential Data Structures", "TypeIdentifier": "File", "LastModifiedDate": "2026-09-30T14:43:00.343Z",
             "Url": "/content/enforced/1-ASE/pages/Lecture Standard Containers.html"},
            {"TopicId": 36, "Title": "Exercise: Singly Linked List", "TypeIdentifier": "File", "LastModifiedDate": "2026-10-08T14:57:06.067Z",
             "Url": "/content/enforced/1-ASE/pages/Coding Exercise Singly Linked List.html"},
        ], "Modules": []}]}

    async def topic_file(self, ou, topic_id):
        self.fetched.append(f"topic:{topic_id}")
        if topic_id == 50:
            return STUB_HTML.encode(), "Lecture Standard Containers.html", "text/html"
        return b"<html><body><h1>Exercise</h1><p>Build a linked list of Node {char data; Node* nextNode;}.</p></body></html>", "Coding Exercise Singly Linked List.html", "text/html"

    async def get_bytes(self, path, params=None):
        self.fetched.append(path)
        assert path == "/content/enforced/1-ASE/slides/lecture04-Efficient.pdf?isCourseFile=true"
        return b"%PDF-1.4 fake", "lecture04-Efficient.pdf", "application/pdf"


def _service(tmp_path: Path) -> tuple[NowService, FakeClient]:
    cfg = Config(state_dir=tmp_path / "s", data_dir=tmp_path / "d")
    cfg.state_dir.mkdir(); cfg.data_dir.mkdir()
    client = FakeClient()
    svc = NowService(cfg, client)
    svc._modules = {1: {"ou": 1, "code": "SOFT30161", "name": "ASE", "is_active": True, "can_access": True, "pinned": False}}
    svc._modules_at = 10**12
    return svc, client


def test_download_follows_links_then_study_pack(tmp_path: Path, monkeypatch):
    svc, client = _service(tmp_path)
    # The fake PDF has no extractable text; pretend extraction gives slide text.
    monkeypatch.setattr(svc.text_cache, "get", lambda p: ("--- slide 1 ---\nvector vs list" if p.suffix == ".pdf" else Path(p).read_text(), None))
    rep = asyncio.run(svc.download_content(1))
    assert len(rep["saved"]) == 2 and len(rep["linked_files"]) == 1
    linked = Path(rep["linked_files"][0]["path"])
    assert linked.name == "50_att_lecture04-Efficient.pdf" and linked.is_file()
    index = json.loads((svc.cfg.data_dir / "_index.json").read_text())
    assert index["1:50:lecture04-Efficient.pdf"]["kind"] == "attachment"
    assert index["1:50"]["links_checked"] == "2026-09-30T14:43:00.343Z"

    # Second run: nothing re-fetched, links not re-parsed.
    n = len(client.fetched)
    rep2 = asyncio.run(svc.download_content(1))
    assert rep2["skipped_unchanged"] == 2 and not rep2["linked_files"] and len(client.fetched) == n

    pack = asyncio.run(svc.study_pack(1, query="lecture 4", refresh=False))
    assert pack["unit"] == "Week 12: Sequential Data Structures" and pack["week"] == 12
    assert [f["kind"] for f in pack["files"]] == ["slides", "lecture", "lab"]
    assert pack["files"][0]["source"] == "linked file" and "vector vs list" in pack["files"][0]["text"]
    assert "Node* nextNode" in pack["files"][2]["text"]
    assert pack["chars"]["total"] == pack["chars"]["included"]

    units = asyncio.run(svc.list_units(1))
    assert units["units"][0]["week"] == 12 and units["units"][0]["files"] == 3
    miss = asyncio.run(svc.study_pack(1, query="hash tables", refresh=False))
    assert miss["error"] == "no_match" and miss["units"]


def test_index_survives_concurrent_downloads_and_adopts_orphans(tmp_path: Path, monkeypatch):
    svc, client = _service(tmp_path)
    monkeypatch.setattr(svc.text_cache, "get", lambda p: ("text", None))

    # Two downloads at once used to overwrite each other's index entries.
    async def both():
        return await asyncio.gather(svc.download_content(1), svc.download_content(1))
    asyncio.run(both())
    index = json.loads((svc.cfg.data_dir / "_index.json").read_text())
    assert {"1:50", "1:36", "1:50:lecture04-Efficient.pdf"} <= set(index)

    # Lose the index: the files stay on disk and are adopted instead of re-downloaded or duplicated.
    (svc.cfg.data_dir / "_index.json").unlink()
    before = sorted(p.name for p in (svc.cfg.data_dir / "SOFT30161" / "Week 12_ Sequential Data Structures").iterdir())
    n = len(client.fetched)
    rep = asyncio.run(svc.download_content(1))
    assert rep["adopted_existing"] == 2 and not rep["saved"] and len(client.fetched) == n
    after = sorted(p.name for p in (svc.cfg.data_dir / "SOFT30161" / "Week 12_ Sequential Data Structures").iterdir())
    assert before == after
    index = json.loads((svc.cfg.data_dir / "_index.json").read_text())
    assert index["1:50"]["adopted"] is True and "1:50:lecture04-Efficient.pdf" in index
