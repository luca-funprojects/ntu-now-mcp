"""The MCP server: read-only tools over NOW (Brightspace) for Claude.

Run locally for the Claude desktop app (stdio)::

    python -m now_mcp.server

Run as a remote connector (Streamable HTTP, behind HTTPS)::

    python -m now_mcp.server --http

Every tool returns plain JSON.  When the NOW session has lapsed the tools
return ``{"error": "needs_login", ...}`` with what to do next instead of
crashing, so Claude can relay it.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import logging
import os
import sys
import time
from typing import Annotated, Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .browser import BrowserSession
from .client import NowApiError, NowClient
from .config import Config
from .service import NowService
from .session import NeedsLogin, state_age_seconds, state_exists

log = logging.getLogger("now_mcp")

cfg = Config.load()
client = NowClient(cfg)
svc = NowService(cfg, client)
browser = BrowserSession(cfg, client.ensure_session)

INSTRUCTIONS = f"""
Personal read-only connector to NTU's NOW (Brightspace) for one student.
Base URL: {cfg.base_url}.  Times in *_local fields are {cfg.timezone}.

Start with list_modules to get org-unit ids (ou), then get_deadlines for the
big picture.  download_content + read_document/search_downloads let you read
lecture material.  If a tool returns error="needs_login", follow its message:
now_login_start begins a sign-in (the student may need to approve an MFA
prompt on their phone; now_login_poll reports progress).  now_api_get and
now_page_text are escape hatches for anything not covered by a dedicated tool;
now_browse / now_click / now_screenshot drive a real logged-in browser for pages
that only render with JavaScript, or when you need to see the layout.
"""

server = MCPServer("ntu-now", instructions=INSTRUCTIONS.strip(), version=__version__)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
SESSION = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)


def guarded(fn):
    """Turn exceptions into readable error payloads."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except NeedsLogin as exc:
            return {"error": "needs_login", "message": str(exc)}
        except NowApiError as exc:
            return {"error": "api_error", "status": exc.status, "path": exc.path, "detail": exc.detail}
        except ValueError as exc:
            return {"error": "bad_request", "message": str(exc)}
        except Exception as exc:  # noqa: BLE001
            log.exception("tool failure")
            return {"error": exc.__class__.__name__, "message": str(exc)[:500]}

    return wrapper


OU = Annotated[int, Field(description="Module org-unit id (from list_modules).")]
OU_OPT = Annotated[int | None, Field(description="Module org-unit id; omit for all current modules.")]


# ----------------------------------------------------------------- session

@server.tool(annotations=READ_ONLY)
@guarded
async def now_status() -> Any:
    """Check whether the NOW session is alive, who it belongs to, and when it was last refreshed."""
    logged_in = await client.is_logged_in()
    who = await client.whoami() if logged_in else None
    age = state_age_seconds(cfg)
    flow = client.flow.state.as_dict() if client.flow else None
    return {
        "logged_in": logged_in,
        "user": {"name": f"{who.get('FirstName', '')} {who.get('LastName', '')}".strip(), "username": who.get("UniqueName"), "id": who.get("Identifier")} if who else None,
        "saved_session": state_exists(cfg),
        "saved_session_age_hours": round(age / 3600, 1) if age is not None else None,
        "last_refresh_minutes_ago": round((time.time() - client.last_refresh) / 60, 1) if client.last_refresh else None,
        "api_versions": await client.versions(),
        "login_flow": flow,
        "credentials_configured": bool(os.environ.get("NOW_USERNAME") and os.environ.get("NOW_PASSWORD")),
        "data_dir": str(cfg.data_dir),
    }


@server.tool(annotations=SESSION)
@guarded
async def now_login_start(
    wait_seconds: Annotated[int, Field(description="How long to wait before returning progress (10-120).")] = 45,
) -> Any:
    """Start signing in to NOW (headless). Uses NOW_USERNAME/NOW_PASSWORD from the server's .env; the student may then need to approve an MFA prompt in Microsoft Authenticator. Returns progress; call now_login_poll to follow it."""
    wait = max(10, min(120, wait_seconds))
    status = await client.start_login(wait_seconds=wait)
    status["next"] = {
        "done": "Signed in. Carry on with the other tools.",
        "needs_input": "Ask the student to do what the message says, then call now_login_poll.",
        "running": "Still in progress: call now_login_poll in a few seconds.",
        "failed": "Sign-in failed. Check the message; the student may need to run the headful login on the laptop.",
    }.get(status["status"], "")
    return status


@server.tool(annotations=SESSION)
@guarded
async def now_login_poll(
    wait_seconds: Annotated[int, Field(description="Seconds to wait for progress before returning (0-90).")] = 20,
) -> Any:
    """Report progress of an in-flight sign-in (and wait up to wait_seconds for it to advance)."""
    if client.flow is None:
        return {"status": "idle", "message": "No sign-in in progress; call now_login_start."}
    await client.flow.wait(timeout=max(0, min(90, wait_seconds)))
    return await client.login_poll()


@server.tool(annotations=SESSION)
@guarded
async def now_login_submit_code(code: Annotated[str, Field(description="One-time verification code the student read out.")]) -> Any:
    """Type a verification code into the sign-in page when MFA asks for one."""
    if client.flow is None:
        return {"ok": False, "error": "No sign-in in progress."}
    result = await client.flow.submit_code(code.strip())
    await client.flow.wait(timeout=20)
    result["status"] = await client.login_poll()
    return result


@server.tool(annotations=SESSION)
@guarded
async def now_login_cancel() -> Any:
    """Abandon an in-flight sign-in."""
    if client.flow is None:
        return {"ok": True, "message": "Nothing to cancel."}
    await client.flow.cancel()
    return {"ok": True}


# ----------------------------------------------------------------- modules

@server.tool(annotations=READ_ONLY)
@guarded
async def list_modules(
    include_inactive: Annotated[bool, Field(description="Also return past/inactive modules.")] = False,
) -> Any:
    """List the student's modules (course offerings) with their org-unit ids."""
    return await svc.modules(include_inactive=include_inactive, refresh=True)


@server.tool(annotations=READ_ONLY)
@guarded
async def get_module(ou: OU) -> Any:
    """Basic details of one module."""
    info = await client.course_info(ou)
    return {
        "ou": ou,
        "code": info.get("Code"),
        "name": info.get("Name"),
        "start": info.get("StartDate"),
        "end": info.get("EndDate"),
        "description": (info.get("Description") or {}).get("Text") if isinstance(info.get("Description"), dict) else info.get("Description"),
        "semester": (info.get("Semester") or {}).get("Name"),
        "department": (info.get("Department") or {}).get("Name"),
        "home": f"{cfg.base_url}/d2l/home/{ou}",
    }


# -------------------------------------------------------------- assessments

@server.tool(annotations=READ_ONLY)
@guarded
async def list_assignments(
    ou: OU_OPT = None,
    include_status: Annotated[bool, Field(description="Also read the Assignments page for submitted/not-submitted status (slower).")] = True,
) -> Any:
    """Coursework assignments (Brightspace 'dropbox folders'): due dates, marks, group flag, instructions, attachments, submission status."""
    return await svc.assignments(ou, include_status=include_status)


@server.tool(annotations=READ_ONLY)
@guarded
async def list_quizzes(ou: OU_OPT = None) -> Any:
    """Quizzes/tests with their windows and due dates."""
    return await svc.quizzes(ou)


@server.tool(annotations=READ_ONLY)
@guarded
async def get_deadlines(
    days_back: Annotated[int, Field(description="Include items due up to this many days ago (to catch overdue work).")] = 14,
    days_ahead: Annotated[int, Field(description="Include items due up to this many days ahead.")] = 60,
) -> Any:
    """One merged, date-sorted list of everything due: assignments, quizzes and calendar events (exams, group milestones) across all current modules."""
    return await svc.deadlines(days_back=days_back, days_ahead=days_ahead)


@server.tool(annotations=READ_ONLY)
@guarded
async def get_calendar(
    days_back: Annotated[int, Field(description="Days back from today.")] = 7,
    days_ahead: Annotated[int, Field(description="Days ahead from today.")] = 30,
) -> Any:
    """The student's NOW calendar events (lectures, exams, due dates added by tutors)."""
    return await svc.calendar(days_back, days_ahead)


@server.tool(annotations=READ_ONLY)
@guarded
async def list_announcements(
    ou: OU_OPT = None,
    days_back: Annotated[int, Field(description="How far back to look.")] = 30,
) -> Any:
    """Module announcements (news), newest first."""
    return await svc.announcements(ou, days_back=days_back)


@server.tool(annotations=READ_ONLY)
@guarded
async def get_grades(ou: OU) -> Any:
    """Released grades and feedback for a module."""
    return await svc.grades(ou)


# ------------------------------------------------------------------ content

@server.tool(annotations=READ_ONLY)
@guarded
async def list_content(
    ou: OU,
    unread_only: Annotated[bool, Field(description="Only topics the student has not opened yet.")] = False,
) -> Any:
    """The module's Content table of contents: every lecture/file/link with its path, type, unread flag and whether it is downloaded locally."""
    return await svc.content(ou, unread_only=unread_only)


@server.tool(annotations=READ_ONLY)
@guarded
async def download_content(
    ou: OU,
    topic_ids: Annotated[list[int] | None, Field(description="Specific topic ids; omit for every downloadable file in the module.")] = None,
    force: Annotated[bool, Field(description="Re-download even if unchanged.")] = False,
) -> Any:
    """Download the module's files (PDF/PPTX/DOCX/HTML pages) into the data directory and extract their text for reading/searching. Unchanged files are skipped."""
    return await svc.download_content(ou, topic_ids=topic_ids, force=force)


@server.tool(annotations=READ_ONLY)
@guarded
async def download_assignment_attachments(ou: OU, folder_id: Annotated[int, Field(description="Assignment id from list_assignments.")]) -> Any:
    """Download an assignment's brief/attachments and its instructions."""
    return await svc.download_assignment_attachments(ou, folder_id)


@server.tool(annotations=READ_ONLY)
@guarded
async def list_downloads(ou: OU_OPT = None) -> Any:
    """What has been downloaded so far (paths usable with read_document)."""
    return svc.list_downloads(ou)


@server.tool(annotations=READ_ONLY)
@guarded
async def read_document(
    path: Annotated[str, Field(description="Path from list_downloads/download_content (absolute or relative to the data dir).")],
    offset: Annotated[int, Field(description="Character offset to start from, for long documents.")] = 0,
    max_chars: Annotated[int, Field(description="Maximum characters to return (up to 60000).")] = 15000,
) -> Any:
    """Read the extracted text of a downloaded file (PDF, PPTX, DOCX, HTML, text), paged by character offset."""
    return svc.read_document(path, offset=max(0, offset), max_chars=max(500, min(60000, max_chars)))


@server.tool(annotations=READ_ONLY)
@guarded
async def search_downloads(
    query: Annotated[str, Field(description="Case-insensitive phrase to look for.")],
    ou: OU_OPT = None,
    max_results: Annotated[int, Field(description="Maximum files to return.")] = 20,
) -> Any:
    """Search the text of all downloaded files for a phrase; returns matching files with snippets."""
    return svc.search_downloads(query, ou=ou, max_results=max_results)


# -------------------------------------------------------------- people/groups

@server.tool(annotations=READ_ONLY)
@guarded
async def list_groups(ou: OU) -> Any:
    """Group-work groups in a module, marking the student's own group and its members."""
    return await svc.groups(ou)


@server.tool(annotations=READ_ONLY)
@guarded
async def get_classlist(ou: OU) -> Any:
    """People enrolled in a module (names, roles, emails where visible)."""
    return await svc.classlist(ou)


@server.tool(annotations=READ_ONLY)
@guarded
async def list_discussions(ou: OU) -> Any:
    """Discussion forums and topics in a module."""
    return await svc.discussions(ou)


@server.tool(annotations=READ_ONLY)
@guarded
async def get_discussion_posts(
    ou: OU,
    forum_id: int,
    topic_id: int,
    max_posts: Annotated[int, Field(description="Newest N posts.")] = 50,
) -> Any:
    """Posts in a discussion topic, newest first."""
    return await svc.discussion_posts(ou, forum_id, topic_id, max_posts=max_posts)


# ----------------------------------------------------------------- watcher

@server.tool(annotations=READ_ONLY)
@guarded
async def list_new_content(
    hours: Annotated[int, Field(description="Look-back window in hours (topics modified/released within it).")] = 72,
    ou: OU_OPT = None,
    mark_seen: Annotated[bool, Field(description="Remember these items so the next call only returns newer ones.")] = False,
    only_unseen: Annotated[bool, Field(description="Hide items already marked seen by an earlier call.")] = True,
) -> Any:
    """What has appeared or changed on NOW: new/updated content topics (slides, lab sheets), announcements and assignment folders across current modules. Designed for a scheduled check: call with mark_seen=true to get only genuinely new items each time."""
    return await svc.new_content(hours=hours, ou=ou, mark_seen=mark_seen, only_unseen=only_unseen)


@server.tool(annotations=READ_ONLY)
@guarded
async def download_file(
    url: Annotated[str, Field(description="A file URL or path on NOW, e.g. a slides link found with now_browse.")],
    subdir: Annotated[str, Field(description="Folder under the data dir to save into.")] = "_files",
) -> Any:
    """Download any NOW-hosted file (slides linked from a page, attachments) into the data directory and extract its text for read_document."""
    return await svc.download_file(url, subdir=subdir)


# ---------------------------------------------------------------- everything

@server.tool(annotations=READ_ONLY)
@guarded
async def sync_all(
    download: Annotated[bool, Field(description="Also download every module's new/changed files.")] = True,
) -> Any:
    """Pull everything (modules, deadlines, announcements, grades, content) into data/snapshot.json and optionally download all new files. Slow on first run."""
    return await svc.sync_all(download=download)


# ------------------------------------------------------------- escape hatches

@server.tool(annotations=READ_ONLY)
@guarded
async def now_api_get(
    path: Annotated[str, Field(description="A Brightspace API path starting with /d2l/api/, e.g. /d2l/api/lp/1.47/users/whoami")],
    params: Annotated[dict[str, Any] | None, Field(description="Optional query parameters.")] = None,
) -> Any:
    """Raw read-only GET against the Brightspace API, for anything the other tools don't cover (see docs.valence.desire2learn.com). Current versions: now_status.api_versions."""
    if not path.startswith("/d2l/api/"):
        raise ValueError("path must start with /d2l/api/")
    return await client.api_get(path, params)


@server.tool(annotations=READ_ONLY)
@guarded
async def now_page_text(
    path: Annotated[str, Field(description="A NOW page path such as /d2l/home or /d2l/lms/dropbox/user/folders_list.d2l?ou=12345")],
    max_chars: Annotated[int, Field(description="Maximum characters of text to return.")] = 15000,
) -> Any:
    """Fetch any NOW web page with the student's session and return its readable text (for things the API doesn't expose)."""
    if not path.startswith("/"):
        raise ValueError("path must be a site-relative path starting with /")
    return await client.page_text(path, max_chars=max(500, min(60000, max_chars)))


# ------------------------------------------------------------ live browser

@server.tool(annotations=READ_ONLY)
@guarded
async def now_browse(
    target: Annotated[str, Field(description="A NOW path (/d2l/home, /d2l/le/content/12345/Home) or a full URL on NOW.")],
    max_chars: Annotated[int, Field(description="Maximum characters of page text to return.")] = 15000,
    screenshot: Annotated[bool, Field(description="Also return a screenshot of the viewport.")] = False,
) -> Any:
    """Open a page in the server's logged-in browser (JavaScript rendered, exactly as the student sees it) and return its text, title and links — optionally with a screenshot."""
    info = await browser.browse(target, max_chars=max(500, min(60000, max_chars)))
    if screenshot:
        return [info, Image(data=await browser.screenshot(), format="png")]
    return info


@server.tool(annotations=READ_ONLY)
@guarded
async def now_click(
    target: Annotated[str, Field(description="Visible text of a link/button (preferred) or a CSS selector.")],
    max_chars: Annotated[int, Field(description="Maximum characters of page text to return.")] = 15000,
) -> Any:
    """Click something on the current browser page (navigation only: links, tabs, buttons) and return the resulting page."""
    return await browser.click(target, max_chars=max(500, min(60000, max_chars)))


@server.tool(annotations=READ_ONLY)
@guarded
async def now_screenshot(
    target: Annotated[str | None, Field(description="Optional page to open first; omit to shoot the current page.")] = None,
    full_page: Annotated[bool, Field(description="Capture the whole scrollable page rather than the viewport.")] = False,
) -> Any:
    """Screenshot of the browser page (PNG) so layout and visual elements can be inspected."""
    png = await browser.screenshot(full_page=full_page, target=target)
    return [{"url": (await browser.current(500))["url"], "bytes": len(png)}, Image(data=png, format="png")]


@server.tool(annotations=READ_ONLY)
@guarded
async def now_browser_page(
    max_chars: Annotated[int, Field(description="Maximum characters of page text to return.")] = 15000,
) -> Any:
    """Re-read the browser's current page (text, title, links) without navigating."""
    return await browser.current(max_chars=max(500, min(60000, max_chars)))


# ------------------------------------------------------------------ runtime

async def keepalive_loop() -> None:
    """Ping NOW periodically so the Brightspace session never idles out."""
    minutes = cfg.keepalive_minutes
    if minutes <= 0:
        return
    while True:
        await asyncio.sleep(minutes * 60)
        try:
            await browser.idle_close()
            if await client.is_logged_in():
                log.info("keepalive ok")
            else:
                log.warning("keepalive: session gone; attempting silent refresh")
                await client.ensure_session(wait_seconds=60)
        except NeedsLogin as exc:
            log.warning("keepalive: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("keepalive error: %s", exc)


async def run_stdio() -> None:
    task = asyncio.create_task(keepalive_loop())
    try:
        await server.run_stdio_async()
    finally:
        task.cancel()
        await browser.close()
        await client.aclose()


async def run_http() -> None:
    import contextlib

    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    if not cfg.http_secret or cfg.http_secret == "change-me":
        sys.exit("Set NOW_HTTP_SECRET in .env to a long random string before running in HTTP mode.")

    if cfg.public_host:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[cfg.public_host, f"{cfg.public_host}:443", "localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"],
            allowed_origins=[f"https://{cfg.public_host}", "http://localhost", "http://127.0.0.1"],
        )
    else:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

    path = f"/{cfg.http_secret}/mcp"
    app = server.streamable_http_app(streamable_http_path=path, stateless_http=True, transport_security=security, host=cfg.http_host)

    async def healthz(_request):  # noqa: ANN001
        return JSONResponse({"ok": True, "version": __version__, "saved_session": state_exists(cfg)})

    app.router.routes.insert(0, Route("/healthz", healthz))

    inner_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a):  # noqa: ANN001
        task = asyncio.create_task(keepalive_loop())
        try:
            async with inner_lifespan(a):
                yield
        finally:
            task.cancel()
            await browser.close()
            await client.aclose()

    app.router.lifespan_context = lifespan

    log.info("MCP endpoint: http://%s:%s%s", cfg.http_host, cfg.http_port, path)
    config = uvicorn.Config(app, host=cfg.http_host, port=cfg.http_port, log_level="info", proxy_headers=True, forwarded_allow_ips="*", timeout_graceful_shutdown=3)
    await uvicorn.Server(config).serve()


def main() -> None:
    parser = argparse.ArgumentParser(description="NTU NOW MCP server")
    parser.add_argument("--http", action="store_true", help="serve over Streamable HTTP instead of stdio")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        stream=sys.stderr,  # stdout is the MCP channel in stdio mode
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run_http() if args.http else run_stdio())


if __name__ == "__main__":
    main()
