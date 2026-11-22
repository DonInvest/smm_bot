"""Настройки кросспоста. Все значения читаются из окружения (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except ValueError:
        return default


@dataclass
class CrosspostConfig:
    enabled: bool
    dry_run: bool
    threads_token: str
    square_key: str
    imgbb_key: str
    delay_seconds: int
    poll_seconds: int
    max_attempts: int
    square_strip_links: bool
    square_max_chars: int
    notify_chat_id: Optional[int]
    db_path: str
    token_file: str
    platforms: set = field(default_factory=set)

    @classmethod
    def from_env(cls, base_dir: str, notify_chat_id: Optional[int]) -> "CrosspostConfig":
        threads_token = (os.getenv("THREADS_ACCESS_TOKEN") or "").strip()
        square_key = (os.getenv("BINANCE_SQUARE_API_KEY") or "").strip()
        platforms = set()
        if threads_token and _flag("CROSSPOST_THREADS", "1"):
            platforms.add("threads")
        if square_key and _flag("CROSSPOST_SQUARE", "1"):
            platforms.add("square")
        return cls(
            enabled=_flag("CROSSPOST_ENABLED", "1") and bool(platforms),
            dry_run=_flag("CROSSPOST_DRY_RUN", "1"),
            threads_token=threads_token,
            square_key=square_key,
            imgbb_key=(os.getenv("IMGBB_API_KEY") or "").strip(),
            delay_seconds=max(0, _int("CROSSPOST_DELAY_SECONDS", 60)),
            poll_seconds=max(3, _int("CROSSPOST_POLL_SECONDS", 10)),
            max_attempts=max(1, _int("CROSSPOST_MAX_ATTEMPTS", 5)),
            square_strip_links=_flag("SQUARE_STRIP_LINKS", "1"),
            square_max_chars=max(200, _int("SQUARE_MAX_CHARS", 2000)),
            notify_chat_id=notify_chat_id,
            db_path=os.path.join(base_dir, ".crosspost.sqlite3"),
            token_file=os.path.join(base_dir, ".threads_token.json"),
            platforms=platforms,
        )
