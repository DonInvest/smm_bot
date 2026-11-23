"""DeBank-агент: публикует кросспосты в DeBank Stream из твоего Mac.

API для постов у DeBank нет, поэтому агент работает через браузер: Playwright
управляет настоящим Google Chrome в отдельном профиле, где лежит твоя сессия DeBank.
Задания и отчёты ходят на сервер бота по SSH (crosspost/remote.py), порты не открываются.

  python agent.py import-session ~/debank_session.json   # один раз: перенести сессию
  python agent.py check                                  # залогинен ли профиль
  python agent.py run-once                               # забрать и опубликовать одно задание
  python agent.py try "текст" --dry                      # заполнить форму без очереди и без Post
"""
from __future__ import annotations

import base64
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

SSH_HOST = os.getenv("DEBANK_AGENT_SSH_HOST", "hetzner-bot")
REMOTE_CMD = "cd /root/smm_bot && ./venv/bin/python -m crosspost.remote"
APP_DIR = Path.home() / "Library" / "Application Support" / "smm_bot"
PROFILE_DIR = APP_DIR / "debank-profile"
LOG_DIR = Path.home() / "Library" / "Logs" / "smm_bot"
NEW_POST_URL = "https://debank.com/new-stream"
EDITOR = '[class*="TextPost_editorClassName"]'
HEADLESS = os.getenv("DEBANK_AGENT_HEADLESS", "1") != "0"


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG_DIR / "debank_agent.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def remote(*args: str, timeout: int = 120) -> str:
    cmd = REMOTE_CMD + " " + " ".join(shlex.quote(a) for a in args)
    res = subprocess.run(["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", SSH_HOST, cmd],
                         capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"ssh {args[0]} failed: {res.stderr.strip()[-300:]}")
    return res.stdout.strip()


class NotLoggedIn(Exception):
    pass


def _context(pw, headless: bool):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return pw.chromium.launch_persistent_context(
        str(PROFILE_DIR), channel="chrome", headless=headless,
        viewport={"width": 1400, "height": 900}, locale="en-US",
    )


def _is_logged_in(page) -> bool:
    page.goto(NEW_POST_URL, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_selector(EDITOR, state="visible", timeout=30000)
    except Exception:
        return False
    page.wait_for_timeout(2500)
    if page.get_by_text("Log in to Start").count() and page.get_by_text("Log in to Start").first.is_visible():
        return False
    return True


def import_session(path: str) -> None:
    """Переносит localStorage debank.com из твоего Chrome (файл делаешь сам, значения не печатаются)."""
    from playwright.sync_api import sync_playwright

    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "connected_dict" not in data:
        sys.exit("В файле нет connected_dict: скопирован не тот localStorage")
    with sync_playwright() as pw:
        ctx = _context(pw, headless=True)
        page = ctx.new_page()
        page.goto("https://debank.com/", wait_until="domcontentloaded", timeout=60000)
        page.evaluate("items => { for (const [k, v] of Object.entries(items)) localStorage.setItem(k, v) }", data)
        ok = _is_logged_in(page)
        ctx.close()
    log(f"import-session: {'залогинен' if ok else 'НЕ залогинен'} ({len(data)} ключей)")
    if not ok:
        sys.exit(1)


def check() -> bool:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = _context(pw, headless=HEADLESS)
        ok = _is_logged_in(ctx.new_page())
        ctx.close()
    log(f"check: {'залогинен' if ok else 'НЕ залогинен'}")
    return ok


def publish(text: str, photo_path: Optional[str], dry: bool = False) -> str:
    """Публикует пост, возвращает ссылку. NotLoggedIn, если сессия слетела."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = _context(pw, headless=HEADLESS)
        page = ctx.new_page()
        api_posts = []
        page.on("response", lambda r: api_posts.append(r)
                if r.request.method == "POST" and "api.debank.com" in r.url else None)
        try:
            if not _is_logged_in(page):
                raise NotLoggedIn("сессия DeBank не активна")
            editor = page.locator(EDITOR).first
            if photo_path:
                box = editor.locator("xpath=ancestor::*[.//input[@type='file']][1]")
                box.locator("input[type=file]").first.set_input_files(photo_path)
                page.wait_for_timeout(4000)
            editor.click()
            lines = text.split("\n")
            for i, line in enumerate(lines):
                if line:
                    page.keyboard.insert_text(line)
                if i < len(lines) - 1:
                    page.keyboard.press("Enter")
            page.wait_for_timeout(1500)
            post_btn = page.locator("button:visible").filter(has_text=re.compile(r"^\s*Post\s*$")).last
            shot = LOG_DIR / f"debank_{int(time.time())}.png"
            page.screenshot(path=str(shot), full_page=True)
            if dry:
                log(f"dry: форма заполнена, скрин {shot}")
                return "dry-run"
            if post_btn.get_attribute("disabled") is not None:
                raise RuntimeError("кнопка Post неактивна")
            api_posts.clear()
            post_btn.click()
            deadline = time.time() + 45
            post_id = None
            while time.time() < deadline and not post_id:
                page.wait_for_timeout(1000)
                for r in list(api_posts):
                    try:
                        body = r.json()
                    except Exception:
                        continue
                    data = body.get("data") if isinstance(body, dict) else None
                    if r.ok and isinstance(data, dict) and (data.get("id") or data.get("article_id")):
                        post_id = data.get("id") or data.get("article_id")
                        log(f"post api: {r.url.split('?')[0]}")
                        break
                if page.url.rstrip("/") != NEW_POST_URL and "/stream/" in page.url:
                    return page.url
            if post_id:
                return f"https://debank.com/stream/{post_id}"
            page.screenshot(path=str(LOG_DIR / f"debank_after_{int(time.time())}.png"), full_page=True)
            raise TimeoutError("нажал Post, но подтверждения публикации не дождался")
        finally:
            ctx.close()


def run_once(dry: bool = False) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(APP_DIR / "debank_agent.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return  # прошлый запуск ещё работает
    job = json.loads(remote("claim", "debank") or "{}")
    if not job:
        return
    did = str(job["delivery_id"])
    log(f"задание #{job['post_id']} (delivery {did}), {len(job['text'])} симв., фото={'да' if job.get('photo_b64') else 'нет'}")
    photo = None
    if job.get("photo_b64"):
        fd, photo = tempfile.mkstemp(suffix=".jpg")
        with os.fdopen(fd, "wb") as f:
            f.write(base64.b64decode(job["photo_b64"]))
    try:
        url = publish(job["text"], photo, dry=dry)
        if dry:
            remote("fail", did, "dry-run агента, пост не нажат", "--retry")
        else:
            remote("done", did, url)
        log(f"готово: {url}")
    except NotLoggedIn as e:
        remote("fail", did, f"{e}. Перенеси сессию заново: python agent.py import-session")
        log(f"ошибка: {e}")
    except TimeoutError as e:
        remote("uncertain", did, str(e))
        log(f"неизвестно: {e}")
    except Exception as e:
        remote("fail", did, f"{type(e).__name__}: {e}"[:300], "--retry")
        log(f"ошибка: {type(e).__name__}: {e}")
    finally:
        if photo:
            os.remove(photo)


def main(argv) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    if argv[1] == "import-session":
        import_session(argv[2])
    elif argv[1] == "check":
        return 0 if check() else 1
    elif argv[1] == "run-once":
        run_once(dry="--dry" in argv)
    elif argv[1] == "try":  # проверка без очереди: python agent.py try "текст" [--photo путь] [--dry]
        photo = argv[argv.index("--photo") + 1] if "--photo" in argv else None
        log(f"try: {publish(argv[2], photo, dry='--dry' in argv)}")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
