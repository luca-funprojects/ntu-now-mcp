"""Shapes raw Brightspace responses into the compact, useful structures the tools return.

Pure functions (``flatten_toc``, ``build_deadlines``, ``match_status_row`` ...)
have no I/O so they are unit-testable; ``NowService`` does the orchestration.
"""

from __future__ import annotations

import json
import mimetypes
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .client import NowApiError, NowClient
from .config import Config
from .extract import TextCache, snippets
from .util import clip, days_until, fmt_local, norm, parse_iso, safe_name, text_of, utcnow, window

DOWNLOADABLE_TYPES = {"File", "ContentService"}


# ---------------------------------------------------------------- pure helpers

def shape_module(enrollment: dict[str, Any]) -> dict[str, Any]:
    ou = enrollment.get("OrgUnit", {}) or {}
    access = enrollment.get("Access", {}) or {}
    return {
        "ou": ou.get("Id"),
        "code": ou.get("Code"),
        "name": ou.get("Name"),
        "type": (ou.get("Type") or {}).get("Name"),
        "is_active": access.get("IsActive"),
        "can_access": access.get("CanAccess"),
        "role": access.get("ClasslistRoleName"),
        "start": access.get("StartDate"),
        "end": access.get("EndDate"),
        "last_accessed": access.get("LastAccessed"),
        "pinned": bool(enrollment.get("PinDate")),
    }


def is_current_module(mod: dict[str, Any], now: datetime | None = None) -> bool:
    """Active, accessible, and not ended more than ~2 weeks ago."""
    now = now or utcnow()
    if mod.get("is_active") is False or mod.get("can_access") is False:
        return False
    end = parse_iso(mod.get("end"))
    if end and end < now - timedelta(days=14):
        return False
    start = parse_iso(mod.get("start"))
    if start and start > now + timedelta(days=60):
        return False
    return True


def flatten_toc(toc: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten Brightspace's nested TOC into a list of topics with their module path."""
    out: list[dict[str, Any]] = []

    def walk(modules: list[dict[str, Any]], path: list[str]) -> None:
        for m in modules or []:
            here = path + [m.get("Title") or "Untitled"]
            for t in m.get("Topics") or []:
                out.append(
                    {
                        "topic_id": t.get("TopicId") or t.get("Id"),
                        "module_id": m.get("ModuleId"),
                        "path": " / ".join(here),
                        "title": t.get("Title"),
                        "type": t.get("TypeIdentifier"),
                        "activity_type": t.get("ActivityType"),
                        "url": t.get("Url"),
                        "unread": t.get("Unread"),
                        "hidden": t.get("IsHidden"),
                        "locked": t.get("IsLocked"),
                        "start": t.get("StartDateTime"),
                        "end": t.get("EndDateTime"),
                        "due": t.get("DueDate"),
                        "last_modified": t.get("LastModifiedDate"),
                        "description": clip(text_of(t.get("Description")), 300) or None,
                        "downloadable": t.get("TypeIdentifier") in DOWNLOADABLE_TYPES,
                    }
                )
            walk(m.get("Modules") or [], here)

    walk(toc.get("Modules") or [], [])
    return out


def shape_assignment(f: dict[str, Any], mod: dict[str, Any], base_url: str, tz) -> dict[str, Any]:
    avail = f.get("Availability") or {}
    due = f.get("DueDate")
    end = avail.get("EndDate") or f.get("EndDate")
    assessment = f.get("Assessment") or {}
    attachments = [
        {"file_id": a.get("FileId"), "name": a.get("FileName"), "size": a.get("Size")}
        for a in (f.get("Attachments") or [])
    ]
    ou = mod.get("ou")
    return {
        "type": "assignment",
        "ou": ou,
        "module_code": mod.get("code"),
        "module_name": mod.get("name"),
        "id": f.get("Id"),
        "title": f.get("Name"),
        "due": due,
        "due_local": fmt_local(due, tz),
        "available_from": avail.get("StartDate") or f.get("StartDate"),
        "available_until": end,
        "available_until_local": fmt_local(end, tz),
        "days_left": days_until(due or end),
        "is_group": f.get("GroupTypeId") is not None,
        "marks": assessment.get("ScoreDenominator"),
        "has_rubric": bool(assessment.get("Rubrics")),
        "hidden": f.get("IsHidden"),
        "submission_type": f.get("SubmissionType"),
        "instructions": clip(text_of(f.get("CustomInstructions")), 1500) or None,
        "attachments": attachments,
        "link": f"{base_url}/d2l/lms/dropbox/user/folder_submit_files.d2l?db={f.get('Id')}&ou={ou}",
    }


def shape_quiz(q: dict[str, Any], mod: dict[str, Any], base_url: str, tz) -> dict[str, Any]:
    due = q.get("DueDate") or q.get("EndDate")
    attempts = q.get("Attempts") or {}
    limit = q.get("SubmissionTimeLimit") or {}
    ou = mod.get("ou")
    return {
        "type": "quiz",
        "ou": ou,
        "module_code": mod.get("code"),
        "module_name": mod.get("name"),
        "id": q.get("QuizId"),
        "title": q.get("Name"),
        "active": q.get("IsActive"),
        "start": q.get("StartDate"),
        "due": due,
        "due_local": fmt_local(due, tz),
        "days_left": days_until(due),
        "attempts_allowed": attempts.get("NumberOfAttemptsAllowed"),
        "time_limit_minutes": limit.get("TimeLimitValue") if limit.get("IsEnabled") else None,
        "description": clip(text_of(q.get("Description")), 600) or None,
        "instructions": clip(text_of(q.get("Instructions")), 600) or None,
        "link": f"{base_url}/d2l/lms/quizzing/user/quiz_summary.d2l?qi={q.get('QuizId')}&ou={ou}",
    }


def shape_event(e: dict[str, Any], modules: dict[int, dict[str, Any]], tz) -> dict[str, Any]:
    ou = e.get("OrgUnitId")
    mod = modules.get(ou, {}) if ou is not None else {}
    assoc = e.get("AssociatedEntity") or {}
    return {
        "type": "event",
        "ou": ou,
        "module_code": mod.get("code"),
        "module_name": mod.get("name"),
        "id": e.get("CalendarEventId"),
        "title": e.get("Title"),
        "start": e.get("StartDateTime"),
        "start_local": fmt_local(e.get("StartDateTime"), tz),
        "end": e.get("EndDateTime"),
        "end_local": fmt_local(e.get("EndDateTime"), tz),
        "all_day": bool(e.get("StartDay")) and not e.get("StartDateTime"),
        "location": e.get("LocationName"),
        "associated": assoc.get("AssociatedEntityType") or assoc.get("Name"),
        "description": clip(text_of(e.get("Description")), 500) or None,
        "link": e.get("CalendarEventViewUrl"),
    }


def shape_news(n: dict[str, Any], mod: dict[str, Any], tz) -> dict[str, Any]:
    return {
        "ou": mod.get("ou"),
        "module_code": mod.get("code"),
        "module_name": mod.get("name"),
        "id": n.get("Id"),
        "title": n.get("Title"),
        "posted": n.get("StartDate") or n.get("CreatedDate"),
        "posted_local": fmt_local(n.get("StartDate") or n.get("CreatedDate"), tz),
        "body": clip(text_of(n.get("Body")), 2000),
        "attachments": [a.get("FileName") for a in (n.get("Attachments") or [])],
    }


def match_status_row(name: str, tables: list[list[list[str]]]) -> dict[str, Any] | None:
    """Find the row on the learner's Assignments page that matches an assignment name."""
    key = norm(name)
    if not key:
        return None
    best: list[str] | None = None
    for table in tables:
        for row in table:
            if row and key in norm(row[0]):
                best = row
                break
        if best:
            break
    if not best:
        return None
    joined = " ".join(best).lower()
    submitted: bool | None = None
    if "not submitted" in joined or "not completed" in joined:
        submitted = False
    elif "submitted" in joined or "completed" in joined:
        submitted = True
    return {"submitted": submitted, "row": best}


def build_deadlines(
    assignments: list[dict[str, Any]],
    quizzes: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    days_back: int,
    days_ahead: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Merge assignments, quizzes and calendar events into one sorted deadline list."""
    now = now or utcnow()
    lo, hi = now - timedelta(days=days_back), now + timedelta(days=days_ahead)
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(item: dict[str, Any], when: str | None) -> None:
        dt = parse_iso(when)
        if dt is None or dt < lo or dt > hi:
            return
        key = (norm(item.get("title") or ""), dt.date().isoformat())
        if key in seen:
            return
        seen.add(key)
        row = dict(item)
        row["when"] = when
        row["days_left"] = days_until(dt, now)
        row["overdue"] = dt < now
        out.append(row)

    for a in assignments:
        add(a, a.get("due") or a.get("available_until"))
    for q in quizzes:
        add(q, q.get("due"))
    for e in events:
        add(e, e.get("start"))
    out.sort(key=lambda r: r["when"])
    return out


# ------------------------------------------------------------------ service

class NowService:
    def __init__(self, cfg: Config, client: NowClient) -> None:
        self.cfg = cfg
        self.client = client
        self.text_cache = TextCache(cfg.data_dir)
        self._modules: dict[int, dict[str, Any]] = {}
        self._modules_at = 0.0
        self._classlists: dict[int, tuple[float, dict[int, dict[str, Any]]]] = {}
        self._index_file = cfg.data_dir / "_index.json"

    # -- modules --------------------------------------------------------------

    async def modules(self, include_inactive: bool = False, refresh: bool = False) -> list[dict[str, Any]]:
        if refresh or not self._modules or time.time() - self._modules_at > 600:
            raw = await self.client.my_enrollments()
            mods = [shape_module(e) for e in raw]
            self._modules = {m["ou"]: m for m in mods if m.get("ou") is not None}
            self._modules_at = time.time()
        mods = list(self._modules.values())
        if not include_inactive:
            mods = [m for m in mods if is_current_module(m)]
        mods.sort(key=lambda m: (not m["pinned"], m.get("code") or ""))
        return mods

    async def module(self, ou: int) -> dict[str, Any]:
        await self.modules(include_inactive=True)
        if ou in self._modules:
            return self._modules[ou]
        info = await self.client.course_info(ou)
        return {"ou": ou, "code": info.get("Code"), "name": info.get("Name"), "start": info.get("StartDate"), "end": info.get("EndDate")}

    async def _targets(self, ou: int | None) -> list[dict[str, Any]]:
        return [await self.module(ou)] if ou is not None else await self.modules()

    # -- assessments ------------------------------------------------------------

    async def assignments(self, ou: int | None = None, include_status: bool = True) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for mod in await self._targets(ou):
            try:
                folders = await self.client.dropbox_folders(mod["ou"])
            except NowApiError as exc:
                out.append({"ou": mod["ou"], "module_code": mod.get("code"), "error": str(exc)})
                continue
            shaped = [shape_assignment(f, mod, self.cfg.base_url, self.cfg.tz) for f in folders]
            if include_status and shaped:
                try:
                    page = await self.client.assignment_status_rows(mod["ou"])
                    for a in shaped:
                        a["submission"] = match_status_row(a["title"] or "", page.get("tables", []))
                except Exception as exc:  # noqa: BLE001 - status is best-effort
                    for a in shaped:
                        a["submission"] = {"error": f"status page unavailable: {exc.__class__.__name__}"}
            out.extend(shaped)
        out.sort(key=lambda a: a.get("due") or a.get("available_until") or "9999")
        return out

    async def quizzes(self, ou: int | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for mod in await self._targets(ou):
            try:
                qs = await self.client.quizzes(mod["ou"])
            except NowApiError as exc:
                if exc.status in (403, 404):
                    continue
                raise
            out.extend(shape_quiz(q, mod, self.cfg.base_url, self.cfg.tz) for q in qs)
        out.sort(key=lambda q: q.get("due") or "9999")
        return out

    async def calendar(self, days_back: int = 7, days_ahead: int = 30) -> list[dict[str, Any]]:
        start, end = window(days_back, days_ahead)
        mods = {m["ou"]: m for m in await self.modules()}
        try:
            events = await self.client.my_events(start, end)
        except NowApiError:
            events = []
            for ou in mods:
                try:
                    events.extend(await self.client.my_events(start, end, ou))
                except NowApiError:
                    continue
        shaped = [shape_event(e, mods, self.cfg.tz) for e in events]
        shaped.sort(key=lambda e: e.get("start") or "")
        return shaped

    async def deadlines(self, days_back: int = 14, days_ahead: int = 60) -> dict[str, Any]:
        assignments = await self.assignments()
        quizzes = await self.quizzes()
        events = await self.calendar(days_back, days_ahead)
        items = build_deadlines(
            [a for a in assignments if "error" not in a], quizzes, events, days_back=days_back, days_ahead=days_ahead
        )
        for it in items:
            it["when_local"] = fmt_local(it["when"], self.cfg.tz)
        overdue = [i for i in items if i["overdue"] and i["type"] == "assignment" and (i.get("submission") or {}).get("submitted") is not True]
        return {
            "generated_at": fmt_local(utcnow(), self.cfg.tz),
            "counts": {"total": len(items), "overdue_unsubmitted_assignments": len(overdue)},
            "items": items,
        }

    async def announcements(self, ou: int | None = None, days_back: int = 30) -> list[dict[str, Any]]:
        since = utcnow() - timedelta(days=days_back)
        out: list[dict[str, Any]] = []
        for mod in await self._targets(ou):
            try:
                items = await self.client.news(mod["ou"])
            except NowApiError:
                continue
            for n in items:
                posted = parse_iso(n.get("StartDate") or n.get("CreatedDate"))
                if posted and posted < since:
                    continue
                out.append(shape_news(n, mod, self.cfg.tz))
        out.sort(key=lambda n: n.get("posted") or "", reverse=True)
        return out

    # -- content ----------------------------------------------------------------

    async def content(self, ou: int, unread_only: bool = False) -> dict[str, Any]:
        mod = await self.module(ou)
        toc = await self.client.toc(ou)
        topics = flatten_toc(toc)
        index = self._load_index()
        for t in topics:
            entry = index.get(f"{ou}:{t['topic_id']}")
            t["downloaded_path"] = entry["path"] if entry else None
            t["link"] = f"{self.cfg.base_url}/d2l/le/content/{ou}/viewContent/{t['topic_id']}/View"
        if unread_only:
            topics = [t for t in topics if t.get("unread")]
        return {
            "ou": ou,
            "module_code": mod.get("code"),
            "module_name": mod.get("name"),
            "counts": {
                "topics": len(topics),
                "downloadable": sum(1 for t in topics if t["downloadable"]),
                "unread": sum(1 for t in topics if t.get("unread")),
                "downloaded": sum(1 for t in topics if t["downloaded_path"]),
            },
            "topics": topics,
        }

    def _load_index(self) -> dict[str, Any]:
        if self._index_file.is_file():
            try:
                return json.loads(self._index_file.read_text())
            except ValueError:
                return {}
        return {}

    def _save_index(self, index: dict[str, Any]) -> None:
        self._index_file.write_text(json.dumps(index, indent=1))

    def _module_dir(self, mod: dict[str, Any]) -> Path:
        return self.cfg.data_dir / safe_name(mod.get("code") or f"ou{mod.get('ou')}")

    def _dest_path(self, mod: dict[str, Any], topic: dict[str, Any], filename: str | None, ctype: str) -> Path:
        name = filename or Path((topic.get("url") or "").split("?")[0]).name or safe_name(topic.get("title") or "untitled")
        if "." not in name:
            ext = mimetypes.guess_extension(ctype.split(";")[0].strip()) or (".html" if "html" in ctype else "")
            name = name + ext
        folder = self._module_dir(mod)
        for part in (topic.get("path") or "").split(" / "):
            if part:
                folder = folder / safe_name(part, 48)
        stem, ext = os.path.splitext(safe_name(name, 150))
        dest = folder / (stem[:90] + ext)
        # Windows caps paths at 260 chars unless the extended-length prefix is used.
        if os.name == "nt" and len(str(dest)) > 240:
            dest = folder / (stem[:40] + ext)
        folder.mkdir(parents=True, exist_ok=True)
        return dest

    async def download_content(self, ou: int, topic_ids: list[int] | None = None, force: bool = False) -> dict[str, Any]:
        mod = await self.module(ou)
        toc = await self.client.toc(ou)
        topics = flatten_toc(toc)
        wanted = set(topic_ids or [])
        index = self._load_index()
        saved, skipped, failed, links = [], [], [], []
        for t in topics:
            if wanted and t["topic_id"] not in wanted:
                continue
            key = f"{ou}:{t['topic_id']}"
            if not t["downloadable"]:
                if t.get("url") and (t.get("type") == "Link" or str(t.get("url")).startswith("http")):
                    links.append({"title": t["title"], "url": t["url"], "path": t["path"]})
                continue
            prior = index.get(key)
            if prior and not force and Path(prior["path"]).is_file() and prior.get("last_modified") == t.get("last_modified"):
                skipped.append(prior["path"])
                continue
            try:
                data, filename, ctype = await self.client.topic_file(ou, t["topic_id"])
            except Exception as exc:  # noqa: BLE001
                failed.append({"topic_id": t["topic_id"], "title": t["title"], "error": f"{exc.__class__.__name__}: {str(exc)[:160]}"})
                continue
            dest = self._dest_path(mod, t, filename, ctype)
            if dest.exists() and (not prior or Path(prior["path"]) != dest):
                dest = dest.with_name(f"{t['topic_id']}_{dest.name}")
            dest.write_bytes(data)
            index[key] = {
                "path": str(dest),
                "ou": ou,
                "module_code": mod.get("code"),
                "title": t["title"],
                "toc_path": t["path"],
                "last_modified": t.get("last_modified"),
                "size": len(data),
                "downloaded_at": utcnow().isoformat(),
            }
            text, err = self.text_cache.get(dest)
            saved.append({"path": str(dest), "size": len(data), "text_chars": len(text), "text_error": err})
        self._save_index(index)
        return {
            "ou": ou,
            "module_code": mod.get("code"),
            "saved": saved,
            "skipped_unchanged": len(skipped),
            "failed": failed,
            "external_links_not_downloadable": links,
        }

    async def download_assignment_attachments(self, ou: int, folder_id: int) -> dict[str, Any]:
        mod = await self.module(ou)
        folder = await self.client.dropbox_folder(ou, folder_id)
        dest_dir = self._module_dir(mod) / "_assignments" / safe_name(folder.get("Name") or str(folder_id), 80)
        dest_dir.mkdir(parents=True, exist_ok=True)
        saved, failed = [], []
        for a in folder.get("Attachments") or []:
            try:
                data, filename, _ = await self.client.dropbox_attachment(ou, folder_id, a["FileId"])
                dest = dest_dir / safe_name(filename or a.get("FileName") or str(a["FileId"]), 150)
                dest.write_bytes(data)
                text, err = self.text_cache.get(dest)
                saved.append({"path": str(dest), "size": len(data), "text_chars": len(text), "text_error": err})
            except Exception as exc:  # noqa: BLE001
                failed.append({"file": a.get("FileName"), "error": f"{exc.__class__.__name__}: {str(exc)[:160]}"})
        instructions = text_of(folder.get("CustomInstructions"))
        if instructions:
            (dest_dir / "instructions.txt").write_text(instructions)
        return {"assignment": folder.get("Name"), "folder": str(dest_dir), "saved": saved, "failed": failed, "instructions": clip(instructions, 3000) or None}

    # -- local files ----------------------------------------------------------------

    def _resolve(self, path: str) -> Path:
        p = Path(path)
        p = p if p.is_absolute() else self.cfg.data_dir / p
        p = p.resolve()
        if self.cfg.data_dir.resolve() not in p.parents and p != self.cfg.data_dir.resolve():
            raise ValueError("Path is outside the data directory.")
        return p

    def list_downloads(self, ou: int | None = None) -> dict[str, Any]:
        index = self._load_index()
        files = [v for v in index.values() if ou is None or v.get("ou") == ou]
        files.sort(key=lambda v: (v.get("module_code") or "", v.get("toc_path") or "", v.get("title") or ""))
        extra = []
        for p in self.cfg.data_dir.rglob("*"):
            if p.is_file() and "_assignments" in p.parts:
                extra.append(str(p))
        return {"data_dir": str(self.cfg.data_dir), "content_files": files, "assignment_files": extra}

    def read_document(self, path: str, offset: int = 0, max_chars: int = 15000) -> dict[str, Any]:
        p = self._resolve(path)
        if not p.is_file():
            return {"error": f"No such file: {p}"}
        text, err = self.text_cache.get(p)
        chunk = text[offset: offset + max_chars]
        return {
            "path": str(p),
            "total_chars": len(text),
            "offset": offset,
            "next_offset": offset + len(chunk) if offset + len(chunk) < len(text) else None,
            "error": err,
            "text": chunk,
        }

    def search_downloads(self, query: str, ou: int | None = None, max_results: int = 20) -> list[dict[str, Any]]:
        hits: list[dict[str, Any]] = []
        for p in sorted(self.cfg.data_dir.rglob("*")):
            if not p.is_file() or "_text_cache" in p.parts or p.name in ("_index.json", "snapshot.json"):
                continue
            if ou is not None:
                index = self._load_index()
                if not any(v.get("ou") == ou and v.get("path") == str(p) for v in index.values()):
                    continue
            text, _ = self.text_cache.get(p)
            if not text:
                continue
            found = snippets(text, query)
            if found:
                hits.append({"path": str(p), "hits": len(found), "snippets": found})
            if len(hits) >= max_results:
                break
        return hits

    # -- people, grades, discussions ----------------------------------------------

    async def classlist(self, ou: int) -> dict[str, Any]:
        cached = self._classlists.get(ou)
        if cached and time.time() - cached[0] < 600:
            return {"source": "cache", "people": list(cached[1].values())}
        data, reason = await self.client.classlist(ou)
        if data is None:
            return {"error": reason, "people": []}
        people = {
            int(p["Identifier"]): {
                "id": int(p["Identifier"]),
                "name": p.get("DisplayName") or f"{p.get('FirstName', '')} {p.get('LastName', '')}".strip(),
                "email": p.get("Email"),
                "role": p.get("ClasslistRoleDisplayName"),
            }
            for p in data
            if p.get("Identifier")
        }
        self._classlists[ou] = (time.time(), people)
        return {"source": "api", "people": list(people.values())}

    async def groups(self, ou: int) -> dict[str, Any]:
        me = await self.client.whoami()
        my_id = int(me.get("Identifier", 0))
        cats, reason = await self.client.group_categories(ou)
        if cats is None:
            page = await self.client.page_text(f"/d2l/lms/group/my_groups.d2l?ou={ou}", max_chars=8000)
            return {"source": "page", "note": f"Groups API: {reason}; showing the My Groups page text instead.", "text": page["text"]}
        people = {p["id"]: p for p in (await self.classlist(ou))["people"]}
        out = []
        any_members = False
        for c in cats:
            groups, g_reason = await self.client.groups(ou, c["GroupCategoryId"])
            entry = {"category": c.get("Name"), "category_id": c.get("GroupCategoryId"), "description": clip(text_of(c.get("Description")), 300) or None, "group_count": len(groups or []), "my_groups": [], "error": g_reason}
            for g in groups or []:
                members = [int(x) for x in (g.get("Enrollments") or [])]
                any_members = any_members or bool(members)
                if my_id in members:
                    entry["my_groups"].append({
                        "group_id": g.get("GroupId"),
                        "name": g.get("Name"),
                        "members": [people.get(m, {"id": m, "name": f"user {m}"}) for m in members],
                    })
            out.append(entry)
        note = None if any_members else "The API returned no memberships (students often cannot see enrolments); ask the student or check the module's Teams site."
        return {"source": "api", "my_user_id": my_id, "note": note, "categories": out}

    async def grades(self, ou: int) -> dict[str, Any]:
        mod = await self.module(ou)
        try:
            values = await self.client.my_grades(ou)
        except NowApiError as exc:
            return {"ou": ou, "module_code": mod.get("code"), "error": str(exc)}
        final, _ = await self.client.my_final_grade(ou)
        items = []
        for g in values:
            num, den = g.get("PointsNumerator"), g.get("PointsDenominator")
            items.append({
                "name": g.get("GradeObjectName"),
                "type": g.get("GradeObjectTypeName"),
                "displayed": g.get("DisplayedGrade"),
                "points": f"{num}/{den}" if num is not None and den else None,
                "weighted": f"{g.get('WeightedNumerator')}/{g.get('WeightedDenominator')}" if g.get("WeightedDenominator") else None,
                "released": fmt_local(g.get("ReleasedDate"), self.cfg.tz),
                "comments": clip(text_of(g.get("Comments")), 500) or None,
            })
        return {"ou": ou, "module_code": mod.get("code"), "final": (final or {}).get("DisplayedGrade") if isinstance(final, dict) else None, "grades": items}

    async def discussions(self, ou: int) -> dict[str, Any]:
        forums, reason = await self.client.forums(ou)
        if forums is None:
            return {"ou": ou, "error": reason, "forums": []}
        out = []
        for f in forums:
            topics, _ = await self.client.topics(ou, f["ForumId"])
            out.append({
                "forum_id": f.get("ForumId"),
                "name": f.get("Name"),
                "description": clip(text_of(f.get("Description")), 300) or None,
                "topics": [{"topic_id": t.get("TopicId"), "name": t.get("Name"), "description": clip(text_of(t.get("Description")), 300) or None} for t in (topics or [])],
            })
        return {"ou": ou, "forums": out}

    async def discussion_posts(self, ou: int, forum_id: int, topic_id: int, max_posts: int = 50) -> list[dict[str, Any]]:
        posts = await self.client.posts(ou, forum_id, topic_id)
        people = {p["id"]: p for p in (await self.classlist(ou))["people"]}
        posts.sort(key=lambda p: p.get("DatePosted") or "", reverse=True)
        out = []
        for p in posts[:max_posts]:
            uid = p.get("PostingUserId")
            out.append({
                "post_id": p.get("PostId"),
                "parent_id": p.get("ParentPostId"),
                "subject": p.get("Subject"),
                "author": (people.get(uid) or {}).get("name") or f"user {uid}",
                "posted_local": fmt_local(p.get("DatePosted"), self.cfg.tz),
                "message": clip(text_of(p.get("Message")), 2000),
            })
        return out

    # -- what's new (for the scheduled watcher) --------------------------------------

    def _seen_file(self) -> Path:
        return self.cfg.data_dir / "_seen.json"

    def _load_seen(self) -> dict[str, str]:
        p = self._seen_file()
        if p.is_file():
            try:
                return json.loads(p.read_text())
            except ValueError:
                return {}
        return {}

    async def new_content(self, hours: int = 72, ou: int | None = None, mark_seen: bool = False,
                          only_unseen: bool = True) -> dict[str, Any]:
        """New or changed topics, announcements and assignments across current modules.

        A topic counts as new when its last_modified (or start date) is within ``hours``
        and, if ``only_unseen``, it has not been returned by an earlier call that passed
        ``mark_seen=True``.  The seen-set lives in data/_seen.json keyed by
        "ou:topic_id:last_modified", so a re-uploaded file shows up again.
        """
        since = utcnow() - timedelta(hours=hours)
        seen = self._load_seen()
        found_topics: list[dict[str, Any]] = []
        found_news: list[dict[str, Any]] = []
        found_assignments: list[dict[str, Any]] = []
        index = self._load_index()
        errors: list[str] = []
        for mod in await self._targets(ou):
            m_ou = mod["ou"]
            try:
                toc = await self.client.toc(m_ou)
            except NowApiError as exc:
                errors.append(f"{mod.get('code')}: {exc}")
                continue
            for t in flatten_toc(toc):
                stamp = t.get("last_modified") or t.get("start")
                dt = parse_iso(stamp)
                if dt is None or dt < since or t.get("hidden"):
                    continue
                key = f"{m_ou}:{t['topic_id']}:{stamp}"
                if only_unseen and key in seen:
                    continue
                entry = index.get(f"{m_ou}:{t['topic_id']}")
                found_topics.append({
                    "key": key,
                    "ou": m_ou,
                    "module_code": mod.get("code"),
                    "module_name": mod.get("name"),
                    "topic_id": t["topic_id"],
                    "path": t["path"],
                    "title": t["title"],
                    "type": t["type"],
                    "url": t.get("url"),
                    "modified": stamp,
                    "modified_local": fmt_local(stamp, self.cfg.tz),
                    "downloadable": t["downloadable"],
                    "downloaded_path": entry["path"] if entry else None,
                    "link": f"{self.cfg.base_url}/d2l/le/content/{m_ou}/viewContent/{t['topic_id']}/View",
                })
            try:
                for n in await self.client.news(m_ou):
                    posted = parse_iso(n.get("StartDate") or n.get("CreatedDate"))
                    key = f"{m_ou}:news:{n.get('Id')}"
                    if posted is None or posted < since or (only_unseen and key in seen):
                        continue
                    item = shape_news(n, mod, self.cfg.tz)
                    item["key"] = key
                    found_news.append(item)
            except NowApiError:
                pass
            try:
                for f in await self.client.dropbox_folders(m_ou):
                    key = f"{m_ou}:dropbox:{f.get('Id')}"
                    if only_unseen and key in seen:
                        continue
                    a = shape_assignment(f, mod, self.cfg.base_url, self.cfg.tz)
                    a["key"] = key
                    found_assignments.append(a)
            except NowApiError:
                pass
        if mark_seen:
            stamp = utcnow().isoformat()
            for item in found_topics + found_news + found_assignments:
                seen[item["key"]] = stamp
            self._seen_file().write_text(json.dumps(seen, indent=0))
        found_topics.sort(key=lambda t: t["modified"] or "", reverse=True)
        return {
            "checked_at": fmt_local(utcnow(), self.cfg.tz),
            "window_hours": hours,
            "counts": {"topics": len(found_topics), "announcements": len(found_news), "assignments": len(found_assignments)},
            "topics": found_topics,
            "announcements": found_news,
            "assignments": found_assignments,
            "errors": errors,
        }

    async def download_file(self, url: str, subdir: str = "_files") -> dict[str, Any]:
        """Download any file hosted on NOW (e.g. slides linked from a page) and extract its text."""
        from urllib.parse import unquote, urlparse

        parsed = urlparse(url if url.startswith("http") else self.cfg.base_url + url)
        if (parsed.hostname or "") != self.cfg.host:
            raise ValueError(f"Only files on {self.cfg.host} can be downloaded.")
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        data, filename, ctype = await self.client.get_bytes(path)
        name = filename or unquote(Path(parsed.path).name) or "download"
        folder = self.cfg.data_dir / safe_name(subdir, 60)
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / safe_name(name, 120)
        if "." not in dest.name:
            ext = mimetypes.guess_extension(ctype.split(";")[0].strip()) or ""
            dest = dest.with_name(dest.name + ext)
        dest.write_bytes(data)
        text, err = self.text_cache.get(dest)
        return {"path": str(dest), "size": len(data), "content_type": ctype, "text_chars": len(text), "text_error": err}

    # -- everything at once ---------------------------------------------------------

    async def sync_all(self, download: bool = True) -> dict[str, Any]:
        mods = await self.modules(refresh=True)
        snapshot: dict[str, Any] = {"generated_at": utcnow().isoformat(), "modules": mods}
        snapshot["deadlines"] = await self.deadlines(days_back=30, days_ahead=365)
        snapshot["announcements"] = await self.announcements(days_back=60)
        downloads = []
        content_counts = {}
        for m in mods:
            try:
                c = await self.content(m["ou"])
                content_counts[m.get("code") or m["ou"]] = c["counts"]
            except NowApiError as exc:
                content_counts[m.get("code") or m["ou"]] = {"error": str(exc)}
                continue
            if download:
                rep = await self.download_content(m["ou"])
                downloads.append({"module": m.get("code"), "new_files": len(rep["saved"]), "failed": len(rep["failed"]), "links": len(rep["external_links_not_downloadable"])})
            try:
                snapshot.setdefault("grades", {})[m.get("code") or m["ou"]] = await self.grades(m["ou"])
            except Exception as exc:  # noqa: BLE001
                snapshot.setdefault("grades", {})[m.get("code") or m["ou"]] = {"error": str(exc)[:200]}
        snapshot["content_counts"] = content_counts
        snapshot["downloads"] = downloads
        (self.cfg.data_dir / "snapshot.json").write_text(json.dumps(snapshot, indent=1, default=str))
        return {
            "snapshot_file": str(self.cfg.data_dir / "snapshot.json"),
            "modules": [m.get("code") for m in mods],
            "deadline_counts": snapshot["deadlines"]["counts"],
            "announcements": len(snapshot["announcements"]),
            "content": content_counts,
            "downloads": downloads,
        }
