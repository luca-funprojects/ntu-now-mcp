"""Read-only Brightspace (NOW) API client authenticated with the saved browser session.

Brightspace's own web UI calls the same REST API with the session cookies, so a
logged-in cookie jar is all we need.  Every request is a GET.  If the session
has lapsed, the client tries a silent re-login through the saved SSO cookies
and, failing that, raises ``NeedsLogin`` with instructions.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import Config
from .extract import html_to_text
from .session import LoginFlow, NeedsLogin, USER_AGENT, load_cookie_jar, state_exists
from .util import to_utc_iso

log = logging.getLogger(__name__)


class NowApiError(Exception):
    def __init__(self, status: int, path: str, detail: str = "") -> None:
        self.status = status
        self.path = path
        self.detail = detail
        super().__init__(f"NOW API {status} for {path}: {detail[:200]}")


class NowClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._http = httpx.AsyncClient(
            base_url=cfg.base_url,
            cookies=load_cookie_jar(cfg),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.8, */*;q=0.5"},
            timeout=httpx.Timeout(60.0, connect=20.0),
            follow_redirects=False,
        )
        self._versions: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self.flow: LoginFlow | None = None
        self.last_ok: float | None = None
        self.last_refresh: float | None = None
        self._state_mtime = self._current_state_mtime()

    def _current_state_mtime(self) -> float:
        try:
            return self.cfg.state_file.stat().st_mtime
        except OSError:
            return 0.0

    def _reload_if_state_changed(self) -> None:
        """Pick up a session file copied in (e.g. by scp) after the server started."""
        mtime = self._current_state_mtime()
        if mtime != self._state_mtime:
            self._state_mtime = mtime
            self.reload_cookies()

    async def aclose(self) -> None:
        await self._http.aclose()
        if self.flow is not None:
            await self.flow.close()

    # ------------------------------------------------------------------ auth

    def reload_cookies(self) -> None:
        self._http.cookies = load_cookie_jar(self.cfg)

    async def is_logged_in(self) -> bool:
        self._reload_if_state_changed()
        try:
            r = await self._http.get(f"/d2l/api/lp/{await self.lp()}/users/whoami")
        except httpx.HTTPError:
            return False
        ok = r.status_code == 200 and "json" in r.headers.get("content-type", "")
        if ok:
            self.last_ok = time.time()
        return ok

    async def ensure_session(self, wait_seconds: int = 60) -> None:
        """Make sure we have a live session; try a silent re-login if not."""
        if await self.is_logged_in():
            return
        async with self._lock:
            if await self.is_logged_in():
                return
            if not state_exists(self.cfg):
                raise NeedsLogin(
                    "No saved NOW session. Run `python -m now_mcp.login` on the laptop, "
                    "or call now_login_start."
                )
            status = await self.start_login(wait_seconds=wait_seconds)
            if status["status"] == "done":
                return
            if status["status"] == "needs_input":
                raise NeedsLogin(
                    "The NOW session expired and signing back in needs you: "
                    + status["message"]
                    + " (The sign-in is still waiting: call now_login_poll to check it, "
                    "or now_login_submit_code if it asked for a code.)"
                )
            raise NeedsLogin(
                "The NOW session expired and silent re-login failed: " + status.get("message", "")
                + " Run the login again (now_login_start, or `python -m now_mcp.login` on the laptop)."
            )

    async def start_login(
        self,
        *,
        wait_seconds: int = 60,
        username: str | None = None,
        password: str | None = None,
        headless: bool = True,
        reuse_state: bool = True,
        target_url: str | None = None,
    ) -> dict[str, Any]:
        """Start (or join) a login flow and wait up to ``wait_seconds`` for it."""
        if self.flow is not None and self.flow.state.status in ("running", "needs_input"):
            return await self.flow.wait(timeout=wait_seconds)
        self.flow = LoginFlow(
            self.cfg, headless=headless, username=username, password=password, reuse_state=reuse_state, target_url=target_url
        )
        await self.flow.start()
        status = await self.flow.wait(timeout=wait_seconds)
        if status["status"] == "done":
            self.reload_cookies()
            self.last_refresh = time.time()
        return status

    async def login_poll(self) -> dict[str, Any]:
        if self.flow is None:
            return {"status": "idle", "message": "No login in progress."}
        status = await self.flow.poll()
        if status["status"] == "done":
            self.reload_cookies()
            self.last_refresh = time.time()
        return status

    # -------------------------------------------------------------- versions

    async def versions(self) -> dict[str, str]:
        if not self._versions:
            try:
                r = await self._http.get("/d2l/api/versions/")
                if r.status_code == 200:
                    for item in r.json():
                        self._versions[item["ProductCode"]] = item["LatestVersion"]
            except (httpx.HTTPError, ValueError, KeyError):
                pass
            self._versions.setdefault("lp", "1.9")
            self._versions.setdefault("le", "1.12")
        return self._versions

    async def lp(self) -> str:
        return (await self.versions())["lp"]

    async def le(self) -> str:
        return (await self.versions())["le"]

    # --------------------------------------------------------------- requests

    def _looks_logged_out(self, r: httpx.Response) -> bool:
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("location", "")
            host = urlparse(loc).hostname
            return (host is not None and host != self.cfg.host) or "/d2l/login" in loc or "/d2l/lp/auth" in loc
        if r.status_code == 401:
            return True
        final = r.url
        if (final.host or "") != self.cfg.host or final.path.startswith(("/d2l/login", "/d2l/lp/auth")):
            return True
        if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
            head = r.text[:3000]
            # Unauthenticated page requests get a tiny JS stub that bounces to /d2l/login;
            # the ADFS form has #userNameInput.  (HTML *content* legitimately comes back
            # from /d2l/api/.../content/topics/{id}/file, so a bare content-type check is wrong.)
            if ("location.replace" in head and "/d2l/login" in head) or 'id="userNameInput"' in head:
                return True
            if r.request.url.path.startswith("/d2l/api/") and "/file" not in r.request.url.path and "/attachments/" not in r.request.url.path:
                return True
        return False

    async def _request(self, path: str, params: dict[str, Any] | None = None, *, follow: bool = False,
                       accept: str | None = None, _retry: bool = True) -> httpx.Response:
        headers = {"Accept": accept} if accept else None
        r = await self._http.get(path, params=params, headers=headers, follow_redirects=follow)
        if r.status_code == 429 and _retry:
            await asyncio.sleep(float(r.headers.get("retry-after", "3")))
            return await self._request(path, params, follow=follow, accept=accept, _retry=False)
        if self._looks_logged_out(r) or (r.status_code == 403 and not await self.is_logged_in()):
            if not _retry:
                raise NeedsLogin("NOW rejected the session even after re-login.")
            await self.ensure_session()
            return await self._request(path, params, follow=follow, accept=accept, _retry=False)
        return r

    async def api_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a JSON API endpoint.  ``path`` may be absolute (a 'Next' link) or a /d2l/api path."""
        if path.startswith("http"):
            parsed = urlparse(path)
            path = parsed.path + ("?" + parsed.query if parsed.query else "")
        r = await self._request(path, params)
        if r.status_code != 200:
            raise NowApiError(r.status_code, path, r.text[:300])
        try:
            return r.json()
        except ValueError:
            raise NowApiError(r.status_code, path, "non-JSON response: " + r.text[:200])

    async def api_get_optional(self, path: str, params: dict[str, Any] | None = None) -> tuple[Any, str | None]:
        """Like api_get but returns (None, reason) instead of raising for 403/404."""
        try:
            return await self.api_get(path, params), None
        except NowApiError as exc:
            if exc.status in (403, 404):
                return None, f"{exc.status}: {'no permission' if exc.status == 403 else 'not available'}"
            raise

    async def get_bytes(self, path: str, params: dict[str, Any] | None = None) -> tuple[bytes, str | None, str]:
        """Download a file. Returns (content, filename, content_type)."""
        r = await self._request(path, params, follow=True, accept="*/*")
        ctype = r.headers.get("content-type", "")
        final_host = urlparse(str(r.url)).hostname or ""
        if r.status_code != 200:
            raise NowApiError(r.status_code, path, r.text[:300])
        if "text/html" in ctype and (final_host != self.cfg.host or b"userNameInput" in r.content[:20000]):
            raise NeedsLogin("Download was redirected to the sign-in page; the session has expired.")
        filename = None
        disp = r.headers.get("content-disposition", "")
        m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", disp, re.I) or re.search(r'filename="?([^";]+)"?', disp, re.I)
        if m:
            from urllib.parse import unquote
            filename = unquote(m.group(1).strip().strip('"'))
        return r.content, filename, ctype

    async def page_text(self, path: str, max_chars: int = 15000) -> dict[str, Any]:
        """Fetch any NOW page as readable text (for things the API doesn't expose)."""
        r = await self._request(path, follow=True, accept="text/html,application/xhtml+xml")
        final_host = urlparse(str(r.url)).hostname or ""
        if final_host != self.cfg.host:
            raise NeedsLogin(f"Page redirected to {final_host}; the session has expired.")
        text = html_to_text(r.text)
        return {"url": str(r.url), "status": r.status_code, "length": len(text), "text": text[:max_chars]}

    async def page_tables(self, path: str) -> dict[str, Any]:
        """Fetch a NOW page and return its tables as rows of cell text."""
        from bs4 import BeautifulSoup

        r = await self._request(path, follow=True, accept="text/html,application/xhtml+xml")
        if (urlparse(str(r.url)).hostname or "") != self.cfg.host:
            raise NeedsLogin("Page redirected away from NOW; the session has expired.")
        soup = BeautifulSoup(r.text, "lxml")
        tables: list[list[list[str]]] = []
        for table in soup.find_all("table"):
            rows: list[list[str]] = []
            for tr in table.find_all("tr"):
                cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
                if any(cells):
                    rows.append(cells)
            if rows:
                tables.append(rows)
        return {"url": str(r.url), "tables": tables, "text": html_to_text(r.text)[:6000] if not tables else ""}

    # ----------------------------------------------------------- endpoints

    async def whoami(self) -> dict[str, Any]:
        return await self.api_get(f"/d2l/api/lp/{await self.lp()}/users/whoami")

    async def my_enrollments(self, org_unit_type_id: int = 3) -> list[dict[str, Any]]:
        lp = await self.lp()
        items: list[dict[str, Any]] = []
        bookmark: str | None = None
        for _ in range(50):
            params: dict[str, Any] = {"orgUnitTypeId": org_unit_type_id}
            if bookmark:
                params["bookmark"] = bookmark
            page = await self.api_get(f"/d2l/api/lp/{lp}/enrollments/myenrollments/", params)
            items.extend(page.get("Items", []))
            paging = page.get("PagingInfo") or {}
            if not paging.get("HasMoreItems"):
                break
            bookmark = paging.get("Bookmark")
        return items

    async def course_info(self, ou: int) -> dict[str, Any]:
        return await self.api_get(f"/d2l/api/lp/{await self.lp()}/courses/{ou}")

    async def dropbox_folders(self, ou: int) -> list[dict[str, Any]]:
        return await self.api_get(f"/d2l/api/le/{await self.le()}/{ou}/dropbox/folders/")

    async def dropbox_folder(self, ou: int, folder_id: int) -> dict[str, Any]:
        return await self.api_get(f"/d2l/api/le/{await self.le()}/{ou}/dropbox/folders/{folder_id}")

    async def dropbox_attachment(self, ou: int, folder_id: int, file_id: int) -> tuple[bytes, str | None, str]:
        return await self.get_bytes(f"/d2l/api/le/{await self.le()}/{ou}/dropbox/folders/{folder_id}/attachments/{file_id}")

    async def assignment_status_rows(self, ou: int) -> dict[str, Any]:
        """The learner's Assignments page, which shows submission status per assignment."""
        return await self.page_tables(f"/d2l/lms/dropbox/user/folders_list.d2l?ou={ou}")

    async def _paged_objects(self, path: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        nxt: str | None = path
        for _ in range(100):
            if not nxt:
                break
            page = await self.api_get(nxt)
            if isinstance(page, list):
                out.extend(page)
                break
            out.extend(page.get("Objects", []))
            nxt = page.get("Next")
        return out

    async def quizzes(self, ou: int) -> list[dict[str, Any]]:
        return await self._paged_objects(f"/d2l/api/le/{await self.le()}/{ou}/quizzes/")

    async def news(self, ou: int) -> list[dict[str, Any]]:
        return await self.api_get(f"/d2l/api/le/{await self.le()}/{ou}/news/")

    async def my_events(self, start, end, ou: int | None = None) -> list[dict[str, Any]]:
        le = await self.le()
        params = {"startDateTime": to_utc_iso(start), "endDateTime": to_utc_iso(end)}
        if ou is None:
            path = f"/d2l/api/le/{le}/calendar/events/myEvents/"
        else:
            path = f"/d2l/api/le/{le}/{ou}/calendar/events/myEvents/"
        data = await self.api_get(path, params)
        if isinstance(data, dict) and "Objects" in data:
            return data["Objects"]
        return data

    async def toc(self, ou: int) -> dict[str, Any]:
        return await self.api_get(f"/d2l/api/le/{await self.le()}/{ou}/content/toc")

    async def topic_file(self, ou: int, topic_id: int) -> tuple[bytes, str | None, str]:
        return await self.get_bytes(f"/d2l/api/le/{await self.le()}/{ou}/content/topics/{topic_id}/file")

    async def my_grades(self, ou: int) -> list[dict[str, Any]]:
        return await self.api_get(f"/d2l/api/le/{await self.le()}/{ou}/grades/values/myGradeValues/")

    async def my_final_grade(self, ou: int) -> tuple[Any, str | None]:
        return await self.api_get_optional(f"/d2l/api/le/{await self.le()}/{ou}/grades/final/values/myGradeValue")

    async def group_categories(self, ou: int) -> tuple[Any, str | None]:
        return await self.api_get_optional(f"/d2l/api/lp/{await self.lp()}/{ou}/groupcategories/")

    async def groups(self, ou: int, category_id: int) -> tuple[Any, str | None]:
        return await self.api_get_optional(f"/d2l/api/lp/{await self.lp()}/{ou}/groupcategories/{category_id}/groups/")

    async def classlist(self, ou: int) -> tuple[Any, str | None]:
        return await self.api_get_optional(f"/d2l/api/le/{await self.le()}/{ou}/classlist/")

    async def forums(self, ou: int) -> tuple[Any, str | None]:
        return await self.api_get_optional(f"/d2l/api/le/{await self.le()}/{ou}/discussions/forums/")

    async def topics(self, ou: int, forum_id: int) -> tuple[Any, str | None]:
        return await self.api_get_optional(f"/d2l/api/le/{await self.le()}/{ou}/discussions/forums/{forum_id}/topics/")

    async def posts(self, ou: int, forum_id: int, topic_id: int) -> list[dict[str, Any]]:
        return await self._paged_objects(
            f"/d2l/api/le/{await self.le()}/{ou}/discussions/forums/{forum_id}/topics/{topic_id}/posts/"
        )
