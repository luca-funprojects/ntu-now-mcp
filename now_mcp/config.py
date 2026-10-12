"""Configuration, loaded from environment variables / a .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


def _find_dotenv() -> Path | None:
    """Look for a .env next to the package, then in the CWD."""
    for candidate in (Path(__file__).resolve().parent.parent / ".env", Path.cwd() / ".env"):
        if candidate.is_file():
            return candidate
    return None


@dataclass
class Config:
    base_url: str = "https://now.ntu.ac.uk"
    state_dir: Path = field(default_factory=lambda: Path("./state"))
    data_dir: Path = field(default_factory=lambda: Path("./data"))
    timezone: str = "Europe/London"
    http_secret: str = ""
    public_host: str = ""
    http_host: str = "127.0.0.1"
    http_port: int = 8765
    keepalive_minutes: int = 20

    @property
    def state_file(self) -> Path:
        return self.state_dir / "now.storage.json"

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            # Windows has no system tz database; `pip install tzdata` provides one.
            return ZoneInfo("UTC")

    @property
    def host(self) -> str:
        return self.base_url.split("://", 1)[-1].split("/", 1)[0]

    @classmethod
    def load(cls) -> "Config":
        dotenv = _find_dotenv()
        if dotenv:
            load_dotenv(dotenv)
        root = dotenv.parent if dotenv else Path.cwd()

        def path_of(var: str, default: str) -> Path:
            raw = os.environ.get(var, default)
            p = Path(raw).expanduser()
            return p if p.is_absolute() else (root / p).resolve()

        cfg = cls(
            base_url=os.environ.get("NOW_BASE_URL", cls.base_url).rstrip("/"),
            state_dir=path_of("NOW_STATE_DIR", "./state"),
            data_dir=path_of("NOW_DATA_DIR", "./data"),
            timezone=os.environ.get("NOW_TIMEZONE", cls.timezone),
            http_secret=os.environ.get("NOW_HTTP_SECRET", ""),
            public_host=os.environ.get("NOW_PUBLIC_HOST", ""),
            http_host=os.environ.get("NOW_HTTP_HOST", cls.http_host),
            http_port=int(os.environ.get("NOW_HTTP_PORT", cls.http_port)),
            keepalive_minutes=int(os.environ.get("NOW_KEEPALIVE_MINUTES", cls.keepalive_minutes)),
        )
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        return cfg
