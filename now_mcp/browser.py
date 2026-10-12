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

    async def _text(self, max_chars: int) -> str:
        try:
            text = await self._page.inner_text("body", timeout=5000)
        except Exception:  # noqa: BLE001
            return ""
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text).strip()
        return text[:max_chars]

    async def _links(self, limit: int = 150) -> list[dict[str, str]]:
        try:
            raw = await self._page.evaluate(
                """() => {
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
            )
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

    async def _page_summary(self, max_chars: int, with_links: bool) -> dict[str, Any]:
        info: dict[str, Any] = {
            "url": self._page.url,
            "title": await self._page.title(),
            "text": await self._text(max_chars),
        }
        if with_links:
            info["links"] = await self._links()
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
            await self.save_state()
            return await self._page_summary(max_chars, with_links)

    async def click(self, target: str, max_chars: int = 15000, timeout_ms: int = 15000) -> dict[str, Any]:
        """Click a link/button by visible text (preferred) or a CSS selector, then describe the page."""
        async with self._lock:
            await self._ensure()
            self.last_used = time.time()
            page = self._page
            candidates = [
                page.get_by_role("link", name=target, exact=False),
                page.get_by_role("button", name=target, exact=False),
                page.get_by_text(target, exact=False),
            ]
            clicked = False
            for loc in candidates:
                try:
                    if await loc.count():
                        await loc.first.click(timeout=timeout_ms)
                        clicked = True
                        break
                except Exception:  # noqa: BLE001
                    continue
            if not clicked:
                try:
                    await page.locator(target).first.click(timeout=timeout_ms)
                    clicked = True
                except Exception as exc:  # noqa: BLE001
                    return {"error": f"Nothing matched '{target}' as text or selector ({exc.__class__.__name__}).", "url": page.url}
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

    async def idle_close(self, idle_seconds: int = 900) -> None:
        """Close the browser if nobody has used it for a while (frees ~200 MB)."""
        if self._browser is not None and self.last_used and time.time() - self.last_used > idle_seconds:
            async with self._lock:
                await self.close()
