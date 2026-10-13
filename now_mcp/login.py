"""One-time sign-in: opens a browser, you log in to NOW, the session is saved.

    python -m now_mcp.login            # opens a window; sign in yourself (MFA etc.)
    python -m now_mcp.login --headless # fills NOW_USERNAME/NOW_PASSWORD, waits for MFA
    python -m now_mcp.login --fresh    # ignore any saved session and start clean

The saved session lives at NOW_STATE_DIR/now.storage.json.  Copy that file to
the server if you run the connector somewhere else.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import warnings

from .config import Config
from .session import LoginFlow


async def run(headless: bool, fresh: bool, timeout: int, target_url: str | None = None) -> int:
    cfg = Config.load()
    print(f"NOW: {cfg.base_url}")
    print(f"Session file: {cfg.state_file}")
    flow = LoginFlow(cfg, headless=headless, max_seconds=timeout, reuse_state=not fresh, target_url=target_url)
    await flow.start()
    if not headless:
        print("\nA browser window has opened. Sign in to NOW as normal (username, password, MFA).")
        print("This window closes by itself once NOW's home page loads.\n")
    last = ""
    while True:
        status = await flow.poll()
        line = f"[{status['status']}] {status['message']}"
        if line != last:
            print(line)
            last = line
        if status["status"] in ("done", "failed"):
            break
        if status["status"] == "needs_input" and headless and "code" in status["message"].lower():
            code = await asyncio.get_event_loop().run_in_executor(None, input, "Enter the verification code: ")
            print(await flow.submit_code(code.strip()))
        await asyncio.sleep(2)
    if status["status"] == "done":
        print(f"\nSaved. Keep {cfg.state_file} private: it is your NOW login.")
        return 0
    print("\nSign-in did not complete:", status["message"])
    if status.get("page_text"):
        print("Last page said:\n", status["page_text"][:600])
    return 1


def main() -> None:
    p = argparse.ArgumentParser(description="Sign in to NOW and save the session for the MCP server.")
    p.add_argument("--headless", action="store_true", help="no window; use NOW_USERNAME/NOW_PASSWORD from .env")
    p.add_argument("--fresh", action="store_true", help="ignore any previously saved session")
    p.add_argument("--timeout", type=int, default=600, help="seconds to wait for the sign-in (default 600)")
    p.add_argument("--url", default=None, help="sign in to another NTU site (e.g. https://myntuac.sharepoint.com/) and add its cookies to the saved session")
    args = p.parse_args()
    warnings.filterwarnings("ignore", category=ResourceWarning)  # Windows pipe noise at exit
    code = asyncio.run(run(args.headless, args.fresh, args.timeout, args.url))
    sys.exit(code)


if __name__ == "__main__":
    main()
