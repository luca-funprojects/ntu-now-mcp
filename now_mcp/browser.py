"""A persistent, logged-in browser on the server that Claude can drive and look at.

The API client covers the structured data; this covers everything else: pages
that only render in a browser (Brightspace's Work-To-Do widget, the newer
content view, StudentHub-style pages), screenshots so layout is visible, and
link maps for finding where things live.

One Chromium context is kept open between tool calls, seeded from the saved
storage state.  Navigation and clicks only; nothing here types into forms.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urlparse

from .config import Config
from .session import NeedsLogin, USER_AGENT, state_exists

log = logging.getLogger(__name__)

LOGIN_HOST_HINTS = ("fs.ntu.ac.uk", "login.microsoftonline.com", "cas.ntu.ac.uk")


class BrowserSession:
    def __init__(self, cfg: Config, ensure_session) -> None:
        """``ensure_session`` is an async callable that refreshes the saved state (the API client's)."""
        self.cfg = cfg
        self._ensure_session = ensure_session
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None
        self._lock = asyncio.Lock()
        self._state_loaded_at = 0.0
        self.last_used = 0.0

    # -- lifecycle ------------------------------------------------------------

    async def _start(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        launch: dict[str, Any] = {"headless": True}
        if os.environ.get("NOW_BROWSER_PROXY"):
            launch["proxy"] = {"server": os.environ["NOW_BROWSER_PROXY"]}
        self._browser = await self._pw.chromium.launch(**launch)
        await self._new_context()

    async def _new_context(self) -> None:
        if self._ctx is not None:
            try:
                await self._ctx.close()
            except Exception:  # noqa: BLE001
                pass
        kwargs: dict[str, Any] = {
            "user_agent": USER_AGENT,
            "viewport": {"width": 1280, "height": 1600},
            "locale": "en-GB",
            "timezone_id": self.cfg.timezone,
            "ignore_https_errors": os.environ.get("NOW_INSECURE_TLS") == "1",
        }
        if state_exists(self.cfg):
            kwargs["storage_state"] = str(self.cfg.state_file)
            self._state_loaded_at = self.cfg.state_file.stat().st_mtime
        self._ctx = await self._browser.new_context(**kwargs)
        self._page = await self._ctx.new_page()

    async def _ensure(self) -> None:
        if self._browser is None or not self._browser.is_connected():
            await self._start()
        elif self._page is None or self._page.is_closed():
            self._page = await self._ctx.new_page()
        # If the API client refreshed the session since we loaded it, pick up the new cookies.
        if state_exists(self.cfg) and self.cfg.state_file.stat().st_mtime > self._state_loaded_at:
            await self._new_context()

    async def close(self) -> None:
        for closer in (self._ctx, self._browser):
            try:
                if closer is not None:
                    await closer.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._pw = self._browser = self._ctx = self._page = None

    async def save_state(self) -> None:
        if self._ctx is not None:
            await self._ctx.storage_state(path=str(self.cfg.state_file))
            self._state_loaded_at = self.cfg.state_file.stat().st_mtime

    # -- helpers ----------------------------------------------------------------

    def _url(self, target: str) -> str:
        if target.startswith("http"):
            host = (urlparse(target).hostname or "").lower()
            allowed = [self.cfg.host.split(":")[0].lower(), "ntu.ac.uk", "microsoftonline.com", "live.com"]
            allowed += [h.strip().lower() for h in os.environ.get("NOW_BROWSER_ALLOWED_HOSTS", "").split(",") if h.strip()]
            if not any(host == a or host.endswith("." + a) for a in allowed):
                raise ValueError(f"The browser only opens NTU pages (and Microsoft sign-in); {host} is not allowed. Add it to NOW_BROWSER_ALLOWED_HOSTS if it is one of NTU's services.")
            return target
        return self.cfg.base_url + (target if target.startswith("/") else "/" + target)

    def _on_login_page(self) -> bool:
        host = urlparse(self._page.url).hostname or ""
        return any(host.endswith(h) for h in LOGIN_HOST_HINTS) or "/d2l/login" in self._page.url

    async def _settle(self, timeout_ms: int) -> None:
        try:
            await self._page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:  # noqa: BLE001 - busy pages never go idle; fine
            pass

    async def _text(self, max_chars: int, root=None) -> str:
        root = root or self._page
        try:
            text = await root.inner_text("body", timeout=5000)
        except Exception:  # noqa: BLE001
            return ""
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text).strip()
        return text[:max_chars]

    _LINKS_JS = """() => {
        const out = [];
        const walk = (root) => {
            for (const a of root.querySelectorAll('a[href]')) {
                const text = (a.innerText || a.textContent || a.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
                if (text && !a.href.startsWith('javascript:')) out.push({text, href: a.href});
            }
            for (const el of root.querySelectorAll('*')) if (el.shadowRoot) walk(el.shadowRoot);
        };
        walk(document);
        return out;
    }"""

    _TABLES_JS = """(maxRows) => {
        const out = [];
        for (const t of document.querySelectorAll('table')) {
            const rows = [];
            for (const tr of t.querySelectorAll('tr')) {
                const cells = Array.from(tr.querySelectorAll('th,td')).map(c => (c.innerText || c.textContent || '').trim().replace(/\\s+/g, ' '));
                if (cells.some(c => c)) rows.push(cells);
                if (rows.length >= maxRows) break;
            }
            if (rows.length) out.push(rows);
            if (out.length >= 25) break;
        }
        return out;
    }"""

    async def _links(self, root=None, limit: int = 150) -> list[dict[str, str]]:
        root = root or self._page
        try:
            raw = await root.evaluate(self._LINKS_JS)
        except Exception:  # noqa: BLE001
            return []
        seen, out = set(), []
        for link in raw:
            key = (link["text"], link["href"])
            if key in seen:
                continue
            seen.add(key)
            out.append(link)
            if len(out) >= limit:
                break
        return out

    async def _tables(self, root=None, max_rows: int = 120) -> list[list[list[str]]]:
        """Every <table> as rows of cell text (frames included when ``root`` is a frame)."""
        root = root or self._page
        try:
            return await root.evaluate(self._TABLES_JS, max_rows)
        except Exception:  # noqa: BLE001
            return []

    def _frames(self):
        """Embedded frames (NOW's external tools, such as the Timetable, live in one)."""
        return [f for f in self._page.frames if f != self._page.main_frame]

    async def _iframe_srcs(self) -> list[dict[str, str]]:
        try:
            return await self._page.evaluate(
                "() => Array.from(document.querySelectorAll('iframe')).map(f => ({src: f.src || f.getAttribute('src') || '', id: f.id || '', name: f.name || ''}))"
            )
        except Exception:  # noqa: BLE001
            return []

    async def _wait_for_frame_content(self, seconds: float) -> None:
        """External tools load through a redirect chain inside their frame; give them a moment."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            for fr in self._frames():
                if fr.url not in ("", "about:blank") and (await self._text(200, fr)):
                    return
            if not await self._iframe_srcs():
                return
            await asyncio.sleep(0.5)

    async def _page_summary(self, max_chars: int, with_links: bool) -> dict[str, Any]:
        info: dict[str, Any] = {
            "url": self._page.url,
            "title": await self._page.title(),
            "text": await self._text(max_chars),
        }
        if with_links:
            info["links"] = await self._links()
        tables = await self._tables()
        if tables:
            info["tables"] = tables
        frames = []
        for fr in self._frames():
            entry: dict[str, Any] = {"url": fr.url, "name": fr.name or ""}
            text = await self._text(max_chars, fr)
            if text:
                entry["text"] = text
                if with_links:
                    entry["links"] = await self._links(fr)
                ftables = await self._tables(fr)
                if ftables:
                    entry["tables"] = ftables
            frames.append(entry)
        iframes = await self._iframe_srcs()
        if iframes:
            info["iframes"] = iframes
        if frames:
            info["frames"] = frames
            with_text = [f for f in frames if f.get("text")]
            if with_text and len(info["text"]) < 400:
                main = info["text"]
                info["text"] = max(with_text, key=lambda f: len(f["text"]))["text"]
                info["note"] = f"The page's own text was only the NOW shell ({main[:60]!r}...); 'text' shows the embedded frame. All frames are under 'frames'."
            elif not with_text:
                info["note"] = "The page embeds frames that rendered no text (still loading, blocked, or a sign-in page that refuses to be framed); their URLs are under 'frames'."
        return info

    # -- tools ------------------------------------------------------------------

    async def browse(self, target: str, max_chars: int = 15000, with_links: bool = True, timeout_ms: int = 30000) -> dict[str, Any]:
        async with self._lock:
            await self._ensure()
            self.last_used = time.time()
            await self._page.goto(self._url(target), wait_until="domcontentloaded", timeout=timeout_ms)
            await self._settle(timeout_ms)
            if self._on_login_page():
                log.info("browser: landed on sign-in; refreshing session")
                await self._ensure_session()  # raises NeedsLogin if it can't
                await self._new_context()
                await self._page.goto(self._url(target), wait_until="domcontentloaded", timeout=timeout_ms)
                await self._settle(timeout_ms)
                if self._on_login_page():
                    raise NeedsLogin("The browser keeps landing on the sign-in page; run the login flow.")
            await self._wait_for_frame_content(12)
            await self.save_state()
            return await self._page_summary(max_chars, with_links)

    async def click(self, target: str, max_chars: int = 15000, timeout_ms: int = 15000) -> dict[str, Any]:
        """Click a link/button by visible text (preferred) or a CSS selector, then describe the page."""
        async with self._lock:
            await self._ensure()
            self.last_used = time.time()
            page = self._page
            roots = [page] + self._frames()
            clicked = False
            for root in roots:
                candidates = [
                    root.get_by_role("link", name=target, exact=False),
                    root.get_by_role("button", name=target, exact=False),
                    root.get_by_text(target, exact=False),
                ]
                for loc in candidates:
                    try:
                        if await loc.count():
                            await loc.first.click(timeout=timeout_ms)
                            clicked = True
                            break
                    except Exception:  # noqa: BLE001
                        continue
                if clicked:
                    break
            if not clicked:
                for root in roots:
                    try:
                        await root.locator(target).first.click(timeout=timeout_ms)
                        clicked = True
                        break
                    except Exception:  # noqa: BLE001
                        continue
            if not clicked:
                return {"error": f"Nothing matched '{target}' as link/button text, visible text or selector (page and embedded frames searched).", "url": page.url}
            await self._settle(timeout_ms)
            if self._on_login_page():
                raise NeedsLogin("That click led to the sign-in page; the session has expired.")
            await self.save_state()
            return await self._page_summary(max_chars, True)

    async def screenshot(self, full_page: bool = False, target: str | None = None) -> bytes:
        async with self._lock:
            await self._ensure()
            self.last_used = time.time()
            if target:
                await self._page.goto(self._url(target), wait_until="domcontentloaded", timeout=30000)
                await self._settle(30000)
            return await self._page.screenshot(full_page=full_page, type="png")

    async def current(self, max_chars: int = 15000) -> dict[str, Any]:
        async with self._lock:
            await self._ensure()
            return await self._page_summary(max_chars, True)

    async def timetable(self, path: str | None, weeks_ahead: int = 1, timeout_ms: int = 30000) -> dict[str, Any]:
        """Read the Timetable tool embedded in NOW: this week and ``weeks_ahead`` more, as table rows.

        Returns raw material (week header text, table rows, the iCal link); parsing lives in
        ``now_mcp.timetable`` so it can be tested without a browser.
        """
        from .timetable import WEEK_HEADER_RE

        async with self._lock:
            await self._ensure()
            self.last_used = time.time()
            if not path:
                # Find the navbar link called "Timetable" from the home page.
                await self._page.goto(self._url("/d2l/home"), wait_until="domcontentloaded", timeout=timeout_ms)
                await self._settle(timeout_ms)
                if self._on_login_page():
                    raise NeedsLogin("The browser landed on the sign-in page; run the login flow.")
                for link in await self._links():
                    if link["text"].strip().lower() == "timetable":
                        path = link["href"]
                        break
                if not path:
                    return {"error": "No 'Timetable' link found in the NOW navigation bar; pass its path explicitly."}
            await self._page.goto(self._url(path), wait_until="domcontentloaded", timeout=timeout_ms)
            await self._settle(timeout_ms)
            if self._on_login_page():
                raise NeedsLogin("The browser landed on the sign-in page; run the login flow.")

            async def find_frame():
                # The tool may sit in a nested frame: prefer the one that holds a table, else the wordiest.
                best, best_len = None, 0
                for fr in self._frames():
                    if await self._tables(fr):
                        return fr
                    n = len(await self._text(4000, fr))
                    if n > best_len:
                        best, best_len = fr, n
                return best

            await self._wait_for_frame_content(25)
            weeks: list[dict[str, Any]] = []
            ical = None
            frame = await find_frame()
            if frame is None:
                return {
                    "error": "The Timetable tool's frame rendered nothing in the server's browser.",
                    "url": self._page.url,
                    "frames": [{"url": f.url, "name": f.name or ""} for f in self._frames()],
                    "iframes": await self._iframe_srcs(),
                    "hint": "If a frame URL is on another NTU host, that tool needs its own sign-in: open it with now_browse (full URL) to see what it shows.",
                }
            for i in range(max(0, weeks_ahead) + 1):
                if i:
                    btn = frame.get_by_role("button", name="Next week")
                    if not await btn.count():
                        btn = frame.locator("input[value='Next week'], button:has-text('Next week')")
                    if not await btn.count():
                        break
                    await btn.first.click(timeout=timeout_ms)
                    try:
                        await frame.wait_for_load_state("networkidle", timeout=timeout_ms)
                    except Exception:  # noqa: BLE001
                        pass
                    frame = await find_frame() or frame
                text = await self._text(20000, frame)
                m = WEEK_HEADER_RE.search(text)
                tables = await self._tables(frame)
                if ical is None:
                    for link in await self._links(frame):
                        if "ical" in link["text"].lower():
                            ical = link["href"]
                            break
                weeks.append({"header": m.group(0) if m else None, "tables": tables, "frame_url": frame.url})
            await self.save_state()
            return {"path": path, "weeks": weeks, "ical_url": ical}

    async def idle_close(self, idle_seconds: int = 900) -> None:
        """Close the browser if nobody has used it for a while (frees ~200 MB)."""
        if self._browser is not None and self.last_used and time.time() - self.last_used > idle_seconds:
            async with self._lock:
                await self.close()
