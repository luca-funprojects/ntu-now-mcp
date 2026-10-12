"""Browser-backed session management for NOW (Brightspace behind NTU's ADFS SSO).

Two jobs live here:

1. ``LoginFlow`` drives a Playwright browser through the NTU sign-in
   (now.ntu.ac.uk -> fs.ntu.ac.uk ADFS form -> MFA -> back to NOW) and saves
   the resulting cookies to a Playwright "storage state" file.  It can run
   headful (you type everything yourself) or headless (it fills the ADFS form
   from configured credentials and waits for you to approve the MFA prompt on
   your phone).  It is written as a small state machine so it copes with the
   pages NTU actually shows, and reports what it is looking at when it gets
   stuck instead of failing silently.

2. ``load_cookie_jar`` turns that storage state into an ``httpx`` cookie jar so
   the API client can talk to Brightspace without a browser.

Nothing here ever writes to NOW.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import Config

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# Hosts we expect to pass through during sign-in.  Anything else is reported.
MICROSOFT_HOSTS = ("login.microsoftonline.com", "login.live.com", "login.microsoft.com")


class NeedsLogin(Exception):
    """Raised when there is no usable NOW session and it could not be refreshed."""


@dataclass
class LoginStatus:
    status: str = "idle"  # idle | running | needs_input | done | failed
    message: str = ""
    url_host: str = ""
    page_text: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    history: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        now = time.time()
        end = self.finished_at or now
        return {
            "status": self.status,
            "message": self.message,
            "current_page_host": self.url_host,
            "page_text": self.page_text,
            "elapsed_seconds": round(end - self.started_at, 1) if self.started_at else 0,
            "history": self.history[-12:],
        }


# ----------------------------------------------------------------------------
# Storage state helpers
# ----------------------------------------------------------------------------

def state_exists(cfg: Config) -> bool:
    return cfg.state_file.is_file() and cfg.state_file.stat().st_size > 0


def state_age_seconds(cfg: Config) -> float | None:
    if not state_exists(cfg):
        return None
    return time.time() - cfg.state_file.stat().st_mtime


def load_cookie_jar(cfg: Config) -> httpx.Cookies:
    """Build an httpx cookie jar from the saved Playwright storage state."""
    jar = httpx.Cookies()
    if not state_exists(cfg):
        return jar
    data = json.loads(cfg.state_file.read_text())
    host = cfg.host
    for c in data.get("cookies", []):
        domain = c.get("domain", "")
        bare = domain.lstrip(".")
        # Keep cookies for the NOW host and any parent domain of it.
        if host == bare or host.endswith("." + bare):
            jar.set(c["name"], c["value"], domain=domain, path=c.get("path", "/"))
    return jar


def session_cookie_present(cfg: Config) -> bool:
    if not state_exists(cfg):
        return False
    data = json.loads(cfg.state_file.read_text())
    names = {c["name"] for c in data.get("cookies", []) if cfg.host.endswith(c.get("domain", "").lstrip("."))}
    return "d2lSecureSessionVal" in names


def _secure_write_note(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ----------------------------------------------------------------------------
# The login state machine
# ----------------------------------------------------------------------------

class LoginFlow:
    """Drive a browser through the NOW sign-in and persist the session.

    Usage (async)::

        flow = LoginFlow(cfg, headless=True, username=..., password=...)
        await flow.start()
        while (await flow.poll())["status"] in ("running", "needs_input"):
            ...
    """

    def __init__(
        self,
        cfg: Config,
        *,
        headless: bool = True,
        username: str | None = None,
        password: str | None = None,
        max_seconds: int = 300,
        reuse_state: bool = True,
    ) -> None:
        self.cfg = cfg
        self.headless = headless
        self.username = username or os.environ.get("NOW_USERNAME") or ""
        self.password = password or os.environ.get("NOW_PASSWORD") or ""
        self.max_seconds = max_seconds
        self.reuse_state = reuse_state
        self.state = LoginStatus()
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None
        self._task: asyncio.Task | None = None
        self._lp_version = "1.9"
        self._submitted_adfs = False
        self._submitted_ms_user = False
        self._submitted_ms_pass = False
        self._last_text = ""

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self.state = LoginStatus(status="running", started_at=time.time(), message="Opening NOW")
        self._pw = await async_playwright().start()
        launch_kwargs: dict[str, Any] = {"headless": self.headless}
        if os.environ.get("NOW_BROWSER_PROXY"):  # e.g. http://proxy:3128 on a locked-down server
            launch_kwargs["proxy"] = {"server": os.environ["NOW_BROWSER_PROXY"]}
        self._browser = await self._pw.chromium.launch(**launch_kwargs)
        ctx_kwargs: dict[str, Any] = {
            "user_agent": USER_AGENT,
            "viewport": {"width": 1280, "height": 900},
            "locale": "en-GB",
            # Debug aid for proxies with private CAs only; never set this in normal use.
            "ignore_https_errors": os.environ.get("NOW_INSECURE_TLS") == "1",
        }
        if self.reuse_state and state_exists(self.cfg):
            ctx_kwargs["storage_state"] = str(self.cfg.state_file)
        self._ctx = await self._browser.new_context(**ctx_kwargs)
        self._page = await self._ctx.new_page()
        try:
            await self._page.goto(self.cfg.base_url + "/d2l/home", wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:  # noqa: BLE001 - we keep going; the loop will report
            self._note(f"Initial navigation problem: {exc.__class__.__name__}")
        await self._discover_lp_version()
        self._task = asyncio.create_task(self._drive())

    async def poll(self) -> dict[str, Any]:
        if self._page is not None and self.state.status in ("running", "needs_input"):
            try:
                self.state.url_host = urlparse(self._page.url).hostname or ""
                self.state.page_text = await self._visible_text()
            except Exception:  # noqa: BLE001
                pass
        return self.state.as_dict()

    async def submit_code(self, code: str) -> dict[str, Any]:
        """Type a one-time code into whichever MFA input is on screen."""
        if self._page is None or self.state.status not in ("running", "needs_input"):
            return {"ok": False, "error": "No login in progress."}
        selectors = [
            "#verificationCodeInput",
            "#idTxtBx_SAOTCC_OTC",
            "input[name='otc']",
            "input[type='tel']",
            "input[autocomplete='one-time-code']",
        ]
        for sel in selectors:
            loc = self._page.locator(sel)
            if await loc.count() and await loc.first.is_visible():
                await loc.first.fill(code)
                await loc.first.press("Enter")
                self._note("Submitted one-time code")
                self.state.status = "running"
                self.state.message = "Code submitted, waiting for NOW"
                return {"ok": True}
        return {"ok": False, "error": "Could not find a code input on the current page.", "page_text": await self._visible_text()}

    async def cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.state.status = "failed"
        self.state.message = "Cancelled"
        self.state.finished_at = time.time()
        await self.close()

    async def wait(self, timeout: float | None = None) -> dict[str, Any]:
        """Block until the flow finishes (or ``timeout`` seconds pass)."""
        if self._task is None:
            return self.state.as_dict()
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        return await self.poll()

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
        self._ctx = self._browser = self._pw = self._page = None

    # -- internals -----------------------------------------------------------

    def _note(self, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.state.history.append(f"{stamp} {msg}")
        log.info("login: %s", msg)

    async def _discover_lp_version(self) -> None:
        try:
            resp = await self._page.request.get(self.cfg.base_url + "/d2l/api/versions/", timeout=15_000)
            if resp.ok:
                for item in await resp.json():
                    if item.get("ProductCode") == "lp":
                        self._lp_version = item.get("LatestVersion", self._lp_version)
        except Exception:  # noqa: BLE001
            pass

    async def _is_logged_in(self) -> bool:
        try:
            resp = await self._page.request.get(
                f"{self.cfg.base_url}/d2l/api/lp/{self._lp_version}/users/whoami",
                headers={"Accept": "application/json"},
                timeout=15_000,
                max_redirects=0,
            )
        except Exception:  # noqa: BLE001
            return False
        if resp.status != 200:
            return False
        try:
            data = await resp.json()
        except Exception:  # noqa: BLE001
            return False
        return isinstance(data, dict) and "Identifier" in data

    async def _visible_text(self, limit: int = 1200) -> str:
        try:
            text = await self._page.inner_text("body", timeout=3000)
        except Exception:  # noqa: BLE001
            return ""
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text).strip()
        return text[:limit]

    async def _visible(self, selector: str) -> bool:
        try:
            loc = self._page.locator(selector)
            return (await loc.count()) > 0 and await loc.first.is_visible()
        except Exception:  # noqa: BLE001
            return False

    async def _drive(self) -> None:
        deadline = self.state.started_at + self.max_seconds
        try:
            while time.time() < deadline:
                try:
                    if await self._is_logged_in():
                        await self._save_state()
                        self.state.status = "done"
                        self.state.message = "Signed in to NOW; session saved."
                        self.state.finished_at = time.time()
                        self._note("Signed in; storage state saved")
                        return
                    if self.state.status == "failed":
                        return
                    await self._step()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self._note(f"step error: {exc.__class__.__name__}: {str(exc)[:120]}")
                await asyncio.sleep(1.5)
            self.state.status = "failed"
            self.state.message = f"Timed out after {self.max_seconds}s without reaching NOW."
            self.state.finished_at = time.time()
        finally:
            await self.close()

    async def _save_state(self) -> None:
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        await self._ctx.storage_state(path=str(self.cfg.state_file))
        _secure_write_note(self.cfg.state_file)

    async def _step(self) -> None:
        page = self._page
        url = page.url or ""
        host = urlparse(url).hostname or ""
        self.state.url_host = host
        text = await self._visible_text()
        if text != self._last_text:
            self._last_text = text
            self.state.page_text = text

        # --- NTU ADFS form (fs.ntu.ac.uk/adfs/ls) -----------------------------
        if await self._visible("#userNameInput") or await self._visible("#passwordInput"):
            err = ""
            if await self._visible("#errorText"):
                err = (await page.locator("#errorText").first.inner_text()).strip()
            if err and self._submitted_adfs:
                self._fail(f"NTU sign-in rejected the credentials: {err}")
                return
            if not self.username or not self.password:
                self._need_input(
                    "NOW is asking for your NTU username and password and none are configured. "
                    "Either run the headful login on your laptop or set NOW_USERNAME / NOW_PASSWORD."
                )
                return
            if self._submitted_adfs:
                # Form shown again with no error text: wait a bit longer.
                self.state.message = "Waiting for NTU sign-in to move on"
                return
            await self._fill_adfs()
            return

        # --- Azure MFA adapter inside ADFS --------------------------------------
        if host.endswith("ntu.ac.uk") and await self._visible("#verificationCodeInput"):
            self._need_input(
                "MFA code required. Call now_login_submit_code with the code from your Authenticator app."
            )
            return
        if host.endswith("ntu.ac.uk") and re.search(r"authenticator|approve|verification|additional (security )?verification", text, re.I):
            number = self._find_mfa_number(text)
            hint = f" The number to match is {number}." if number else ""
            self._need_input(f"Approve the sign-in request in your Microsoft Authenticator app.{hint}")
            return

        # --- Microsoft login pages -------------------------------------------------
        if any(host.endswith(h) for h in MICROSOFT_HOSTS):
            await self._step_microsoft(text)
            return

        # --- Brightspace's own login page (if NTU ever shows it) ------------------
        if host == self.cfg.host and "/d2l/login" in url:
            for sel in ("a:has-text('NTU')", "a:has-text('Sign in')", "a:has-text('Login')", "button:has-text('Log in')"):
                if await self._visible(sel):
                    await page.locator(sel).first.click()
                    self._note(f"Clicked {sel}")
                    return
            self._need_input("Brightspace is showing its own login page; please sign in manually.")
            return

        # --- Otherwise: in transit ---------------------------------------------------
        self.state.status = "running"
        self.state.message = f"On {host or 'unknown host'}; waiting"

    async def _fill_adfs(self) -> None:
        page = self._page
        self._note("Filling NTU ADFS sign-in form")
        if await self._visible("#userNameInput"):
            await page.fill("#userNameInput", self.username)
        if await self._visible("#passwordInput"):
            await page.fill("#passwordInput", self.password)
        if await self._visible("#kmsiInput"):
            try:
                await page.check("#kmsiInput")  # "Keep me signed in"
            except Exception:  # noqa: BLE001
                pass
        self._submitted_adfs = True
        if await self._visible("#submitButton"):
            await page.click("#submitButton")
        else:
            await page.press("#passwordInput", "Enter")
        self.state.message = "Submitted NTU credentials"

    async def _step_microsoft(self, text: str) -> None:
        page = self._page
        # "Stay signed in?" -> Yes, so the Microsoft cookie persists.
        if re.search(r"stay signed in", text, re.I) and await self._visible("#idSIButton9"):
            if await self._visible("#KmsiCheckboxField"):
                try:
                    await page.check("#KmsiCheckboxField")
                except Exception:  # noqa: BLE001
                    pass
            await page.click("#idSIButton9")
            self._note("Accepted 'Stay signed in'")
            return
        # Account picker
        if await self._visible("#tilesHolder") or re.search(r"pick an account", text, re.I):
            tiles = page.locator("#tilesHolder .tile, [data-test-id='account-tile'], .table-row")
            n = await tiles.count()
            for i in range(n):
                t = tiles.nth(i)
                if self.username and self.username.lower() in (await t.inner_text()).lower():
                    await t.click()
                    self._note("Picked account tile")
                    return
            if n:
                await tiles.first.click()
                self._note("Picked first account tile")
                return
        # Username
        if await self._visible("input[name='loginfmt']") and not self._submitted_ms_user:
            if not self.username:
                self._need_input("Microsoft sign-in needs your username but none is configured.")
                return
            await page.fill("input[name='loginfmt']", self.username)
            await page.click("#idSIButton9")
            self._submitted_ms_user = True
            self._note("Submitted Microsoft username")
            return
        # Password (if Microsoft handles it rather than ADFS)
        if await self._visible("input[name='passwd']") and not self._submitted_ms_pass:
            if not self.password:
                self._need_input("Microsoft sign-in needs your password but none is configured.")
                return
            await page.fill("input[name='passwd']", self.password)
            await page.click("#idSIButton9")
            self._submitted_ms_pass = True
            self._note("Submitted Microsoft password")
            return
        if await self._visible("#passwordError") or await self._visible("#usernameError"):
            err = ""
            for sel in ("#passwordError", "#usernameError"):
                if await self._visible(sel):
                    err = (await page.locator(sel).first.inner_text()).strip()
            self._fail(f"Microsoft sign-in error: {err}")
            return
        # MFA: number matching / approve / code
        if await self._visible("#idRichContext_DisplaySign"):
            number = (await page.locator("#idRichContext_DisplaySign").first.inner_text()).strip()
            self._need_input(f"Approve the sign-in in Microsoft Authenticator. The number to match is {number}.")
            return
        if await self._visible("#idTxtBx_SAOTCC_OTC"):
            self._need_input("Enter the verification code: call now_login_submit_code with it.")
            return
        if re.search(r"approve|authenticator|verify your identity", text, re.I):
            self._need_input("Approve the sign-in request in Microsoft Authenticator.")
            return
        self.state.status = "running"
        self.state.message = "Waiting on Microsoft sign-in"

    @staticmethod
    def _find_mfa_number(text: str) -> str | None:
        m = re.search(r"\b(\d{2,3})\b", text)
        return m.group(1) if m else None

    def _need_input(self, msg: str) -> None:
        if self.state.message != msg:
            self._note(msg)
        self.state.status = "needs_input"
        self.state.message = msg

    def _fail(self, msg: str) -> None:
        self._note(msg)
        self.state.status = "failed"
        self.state.message = msg
        self.state.finished_at = time.time()
