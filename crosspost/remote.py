"""CLI для внешних агентов (DeBank-агент на Mac ходит сюда по SSH).

  python -m crosspost.remote claim debank         -> JSON задания или {}
  python -m crosspost.remote done <id> <url>
  python -m crosspost.remote fail <id> <text> [--retry]
  python -m crosspost.remote uncertain <id> <text>

Пишет в ту же SQLite-очередь, что и бот. Итог шлёт в Telegram напрямую через Bot API.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from typing import Optional

import requests
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)

from . import store as st  # noqa: E402
from .config import CrosspostConfig  # noqa: E402
from .service import PLATFORM_TITLES, RETRY_CALLBACK_PREFIX, CrosspostService  # noqa: E402

STALE_SENDING_SEC = 20 * 60


def _notify_chat() -> Optional[int]:
    try:
        return int((os.getenv("AUTOPOST_NOTIFY_CHAT_ID") or "").strip())
    except ValueError:
        return None


def _tg(method: str, **payload):
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        return None
    try:
        return requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=30)
    except requests.RequestException:
        return None


def _send(text: str, retry_delivery: Optional[int] = None, title: str = "") -> None:
    chat = _notify_chat()
    if not chat:
        return
    payload = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    if retry_delivery:
        payload["reply_markup"] = {"inline_keyboard": [[{
            "text": f"🔄 Повторить {title}", "callback_data": f"{RETRY_CALLBACK_PREFIX}{retry_delivery}"}]]}
    _tg("sendMessage", **payload)


def _photo_b64(file_id: Optional[str]) -> Optional[str]:
    if not file_id:
        return None
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    r = _tg("getFile", file_id=file_id)
    path = ((r.json() if r is not None else {}) or {}).get("result", {}).get("file_path")
    if not path:
        return None
    img = requests.get(f"https://api.telegram.org/file/bot{token}/{path}", timeout=60)
    return base64.b64encode(img.content).decode("ascii") if img.ok else None


def _service() -> CrosspostService:
    cfg = CrosspostConfig.from_env(BASE_DIR, _notify_chat())
    return CrosspostService(cfg, log=lambda m: print(m, file=sys.stderr))


def cmd_claim(platform: str) -> dict:
    svc = _service()
    store = svc.store
    # Агент умер посреди публикации: не повторяем вслепую, помечаем «проверь вручную».
    for d_id in [r["id"] for r in store._q(
            "SELECT id FROM deliveries WHERE platform=? AND status=? AND updated_at<?",
            (platform, st.SENDING, int(time.time()) - STALE_SENDING_SEC))]:
        store.update_delivery(d_id, status=st.UNCERTAIN, error="агент не отчитался за 20 минут")
        _send(f"❓ {PLATFORM_TITLES.get(platform, platform)}: агент не отчитался, проверь профиль вручную",
              d_id, PLATFORM_TITLES.get(platform, platform))
    # Старые задания (Mac был выключен) не публикуем задним числом.
    max_age = svc.cfg.remote_max_age_hours * 3600
    for r in store._q(
            "SELECT d.id FROM deliveries d JOIN posts p ON p.id=d.post_id "
            "WHERE d.platform=? AND d.status=? AND p.created_at<?",
            (platform, st.PENDING, int(time.time()) - max_age)):
        store.update_delivery(r["id"], status=st.CANCELLED, error=f"старше {svc.cfg.remote_max_age_hours} ч")
    due = store.claim_due(int(time.time()), limit=1, platforms=(platform,))
    if not due:
        return {}
    job = svc.remote_job(due[0])
    job["photo_b64"] = _photo_b64(job.pop("photo_file_id"))
    return job


def cmd_done(delivery_id: int, url: str) -> None:
    store = _service().store
    d = store.get_delivery(delivery_id)
    store.update_delivery(delivery_id, status=st.DONE, url=url, error="")
    title = PLATFORM_TITLES.get(d.platform, d.platform) if d else "?"
    _send(f"✅ {title} (кросспост #{d.post_id if d else '?'}): {url}")


def cmd_fail(delivery_id: int, text: str, retry: bool, uncertain: bool = False) -> None:
    svc = _service()
    d = svc.store.get_delivery(delivery_id)
    if not d:
        return
    title = PLATFORM_TITLES.get(d.platform, d.platform)
    if uncertain:
        svc.store.update_delivery(delivery_id, status=st.UNCERTAIN, error=text)
        _send(f"❓ {title} (кросспост #{d.post_id}): статус неизвестен, проверь профиль. {text}", delivery_id, title)
    elif retry and d.attempts < svc.cfg.max_attempts:
        svc.store.update_delivery(delivery_id, status=st.PENDING, next_at=int(time.time()) + 600, error=text)
    else:
        svc.store.update_delivery(delivery_id, status=st.FAILED, error=text)
        _send(f"❌ {title} (кросспост #{d.post_id}): {text}", delivery_id, title)


def main(argv) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "claim":
        print(json.dumps(cmd_claim(argv[2]), ensure_ascii=False))
    elif cmd == "done":
        cmd_done(int(argv[2]), argv[3])
    elif cmd == "fail":
        cmd_fail(int(argv[2]), argv[3], retry="--retry" in argv)
    elif cmd == "uncertain":
        cmd_fail(int(argv[2]), argv[3], retry=False, uncertain=True)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
