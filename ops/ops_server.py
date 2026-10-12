"""Server maintenance over MCP, so Claude can look after the box without SSH.

Runs on the host (outside Docker) as a systemd service, bound to localhost,
and exposed by Caddy at https://<host>/<OPS_SECRET>/mcp.  The URL is the
credential: it is long, lives only in .env, and travels only inside
claude.ai's connector settings.

Tools: status, logs, update, restart, read_file, write_file — and `shell`
only when OPS_ALLOW_SHELL=1 is set in .env (off by default).
Standalone on purpose: only the `mcp` package is needed.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

APP_DIR = Path(os.environ.get("NOW_APP_DIR", "/opt/ntu-now-mcp"))
SECRET = os.environ.get("OPS_SECRET", "")
HOST = os.environ.get("OPS_HOST", "127.0.0.1")
PORT = int(os.environ.get("OPS_PORT", "8766"))
PUBLIC_HOST = os.environ.get("NOW_PUBLIC_HOST", "")
MAX_OUT = 20_000

server = MCPServer(
    "ntu-now-ops",
    instructions=(
        f"Maintenance tools for the NOW connector server ({PUBLIC_HOST or 'this host'}). "
        f"Code lives in {APP_DIR}; the connector runs via docker compose there. "
        "Use status first; update pulls main and rebuilds; restart bounces the connector."
    ),
)
OPS = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)
RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)


def _run(cmd: str, timeout: int = 120, cwd: Path | None = None) -> dict[str, Any]:
    started = time.time()
    try:
        p = subprocess.run(
            ["bash", "-lc", cmd], cwd=str(cwd or APP_DIR), capture_output=True, text=True, timeout=timeout
        )
        out, err, code = p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = f"timed out after {timeout}s"
        code = -1
    return {
        "command": cmd,
        "exit_code": code,
        "seconds": round(time.time() - started, 1),
        "stdout": out[-MAX_OUT:],
        "stderr": err[-MAX_OUT:],
    }


@server.tool(annotations=RO)
async def status() -> Any:
    """Health of the box and the connector: uptime, load, memory, disk, containers, git revision, last update log, connector health."""
    parts = {
        "uptime": _run("uptime")["stdout"].strip(),
        "memory": _run("free -h | sed -n '1,2p'")["stdout"],
        "disk": _run("df -h / | tail -1")["stdout"].strip(),
        "git": _run("git log -1 --format='%h %ci %s' && git status --short | head -20")["stdout"],
        "containers": _run("docker compose ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'")["stdout"],
        "last_update_log": _run("tail -5 /var/log/now-mcp-update.log 2>/dev/null")["stdout"],
        "connector_health": _run("curl -s -m 5 http://127.0.0.1:8765/healthz")["stdout"],
        "ops_service": _run("systemctl is-active ntu-now-ops")["stdout"].strip(),
    }
    return parts


@server.tool(annotations=RO)
async def logs(
    service: Annotated[str, Field(description="Compose service: now-mcp or caddy.")] = "now-mcp",
    lines: Annotated[int, Field(description="How many recent lines.")] = 200,
) -> Any:
    """Recent container logs."""
    return _run(f"docker compose logs --no-color --tail {max(10, min(2000, lines))} {service}")


@server.tool(annotations=OPS)
async def update() -> Any:
    """Pull the latest code from GitHub (main) and rebuild/restart the connector if it changed; also restarts this ops service a few seconds later if its own code changed."""
    result = _run("bash deploy/auto-update.sh", timeout=900)
    if "updating" in result["stdout"]:
        # Restart ourselves after replying; systemd brings the service straight back.
        asyncio.get_running_loop().call_later(3, lambda: subprocess.Popen(["systemctl", "restart", "ntu-now-ops"]))
        result["note"] = "ops service restarting in 3s to load its own new code"
    return result


@server.tool(annotations=OPS)
async def restart(
    service: Annotated[str, Field(description="Compose service to restart: now-mcp or caddy; 'all' for both.")] = "now-mcp",
) -> Any:
    """Restart the connector (or Caddy)."""
    target = "" if service == "all" else service
    return _run(f"docker compose restart {target}", timeout=300)


if os.environ.get("OPS_ALLOW_SHELL") == "1":
    # Opt-in only: set OPS_ALLOW_SHELL=1 in .env to let Claude run arbitrary commands.
    # Without it, maintenance is limited to the structured tools above and below.
    @server.tool(annotations=OPS)
    async def shell(
        command: Annotated[str, Field(description="Bash command line to run in the app directory.")],
        timeout: Annotated[int, Field(description="Seconds before the command is killed (max 900).")] = 120,
    ) -> Any:
        """Run a command on the server. Output is capped at 20k characters per stream."""
        return _run(command, timeout=max(5, min(900, timeout)))


@server.tool(annotations=RO)
async def read_file(
    path: Annotated[str, Field(description="Absolute path, or relative to the app directory.")],
    max_chars: Annotated[int, Field(description="Maximum characters to return.")] = 20000,
) -> Any:
    """Read a text file on the server."""
    p = Path(path) if path.startswith("/") else APP_DIR / path
    if not p.is_file():
        return {"error": f"no such file: {p}"}
    text = p.read_text(errors="replace")
    return {"path": str(p), "chars": len(text), "text": text[: max(100, min(200_000, max_chars))]}


@server.tool(annotations=OPS)
async def write_file(
    path: Annotated[str, Field(description="Absolute path, or relative to the app directory.")],
    content: Annotated[str, Field(description="Full new contents of the file.")],
    mode: Annotated[str | None, Field(description="Optional octal permissions, e.g. '600'.")] = None,
) -> Any:
    """Write a text file on the server (creates parent directories; keeps a .bak of any existing file)."""
    p = Path(path) if path.startswith("/") else APP_DIR / path
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        shutil.copy2(p, p.with_suffix(p.suffix + ".bak"))
    p.write_text(content)
    if mode:
        os.chmod(p, int(mode, 8))
    return {"path": str(p), "bytes": len(content.encode())}


async def main() -> None:
    if len(SECRET) < 16:
        sys.exit("OPS_SECRET must be set to a long random string")
    from mcp.server.transport_security import TransportSecuritySettings

    security = (
        TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[PUBLIC_HOST, f"{PUBLIC_HOST}:443", "localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"],
            allowed_origins=[f"https://{PUBLIC_HOST}"],
        )
        if PUBLIC_HOST
        else TransportSecuritySettings(enable_dns_rebinding_protection=False)
    )
    await server.run_streamable_http_async(
        host=HOST, port=PORT, streamable_http_path=f"/{SECRET}/mcp", stateless_http=True, transport_security=security
    )


if __name__ == "__main__":
    asyncio.run(main())
