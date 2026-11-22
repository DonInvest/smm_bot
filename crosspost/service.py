"""Очередь кросспоста: постановка, фоновый воркер, правки, уведомления, продление токена."""
from __future__ import annotations

import asyncio
import base64
import os
import tempfile
import time
import traceback
from typing import Callable, Dict, List, Optional

import requests

from . import store as st
from .config import CrosspostConfig
from .errors import PlatformError
from .square_api import SquareClient
from .text import build_square_payload, build_threads_parts
from .threads_api import ThreadsClient

PLATFORM_TITLES = {"threads": "Threads", "square": "Binance Square"}
RETRY_CALLBACK_PREFIX = "xp_retry:"


def _backoff(attempt: int) -> int:
    return min(1800, 30 * (2 ** max(0, attempt - 1)))


def upload_imgbb(path: str, api_key: str) -> str:
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    try:
        r = requests.post("https://api.imgbb.com/1/upload", data={"key": api_key, "image": b64}, timeout=60)
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        raise PlatformError(f"imgbb upload error: {e}", retryable=True)
    url = ((data or {}).get("data") or {}).get("url")
    if not url:
        raise PlatformError(f"imgbb upload failed: {str(data)[:200]}", retryable=r.status_code >= 500)
    return url


class CrosspostService:
    def __init__(self, cfg: CrosspostConfig, store: Optional[st.Store] = None,
                 threads: Optional[ThreadsClient] = None, square: Optional[SquareClient] = None,
                 sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = print):
        self.cfg = cfg
        self.store = store or st.Store(cfg.db_path)
        self.threads = threads or (ThreadsClient(cfg.threads_token, cfg.token_file) if cfg.threads_token else None)
        self.square = square or (SquareClient(cfg.square_key) if cfg.square_key else None)
        self._sleep = sleep
        self._log = log
        self._media_dir = os.path.join(os.path.dirname(cfg.db_path), ".crosspost_media")
        os.makedirs(self._media_dir, exist_ok=True)
        self._notify_failures: Dict[int, int] = {}

    # ================= постановка =================
    def enqueue(self, source_key: str, x_parts: List[str], photo_file_id: Optional[str],
                platforms: List[str]) -> Optional[int]:
        targets = [p for p in platforms if p in self.cfg.platforms]
        parts = [p.strip() for p in x_parts if p and p.strip()]
        if not targets or not parts:
            return None
        pid = self.store.create_post(source_key, parts, photo_file_id, targets,
                                     int(time.time()) + self.cfg.delay_seconds)
        self._log(f"🔁 Crosspost #{pid} в очереди: {', '.join(targets)} через {self.cfg.delay_seconds}s"
                  f"{' [DRY RUN]' if self.cfg.dry_run else ''}")
        return pid

    def handle_edit(self, source_key: str, x_parts: List[str], photo_file_id: Optional[str],
                    platforms: List[str]) -> List[str]:
        """Правка поста в окне редактирования. Возвращает заметки для уведомления."""
        notes: List[str] = []
        old = self.store.latest_post(source_key)
        repost: List[str] = list(platforms)
        if old:
            for d in self.store.deliveries(old.id):
                title = PLATFORM_TITLES.get(d.platform, d.platform)
                if d.status == st.PENDING:
                    self.store.update_delivery(d.id, status=st.CANCELLED, error="заменён правкой")
                elif d.status == st.DONE and d.platform == "threads" and d.remote_ids and not self.cfg.dry_run:
                    err = self._delete_threads(d.remote_ids)
                    if err:
                        notes.append(f"⚠️ {title}: старый пост не удалён ({err}), новый не публикую, чтобы не было дубля")
                        if d.platform in repost:
                            repost.remove(d.platform)
                    else:
                        notes.append(f"♻️ {title}: старый пост удалён, публикую исправленный")
                elif d.status in (st.DONE, st.UNCERTAIN, st.SENDING):
                    notes.append(f"⚠️ {title}: уже опубликован, удалить через API нельзя, правка не применена")
                    if d.platform in repost:
                        repost.remove(d.platform)
            self.store.set_superseded(old.id)
        if repost:
            self.enqueue(source_key, x_parts, photo_file_id, repost)
        return notes

    def _delete_threads(self, ids: List[str]) -> Optional[str]:
        if not self.threads:
            return "нет клиента Threads"
        for mid in reversed(ids):
            try:
                self.threads.delete(mid)
            except PlatformError as e:
                return str(e)[:200]
        return None

    def retry(self, delivery_id: int) -> bool:
        d = self.store.get_delivery(delivery_id)
        if not d or d.status not in (st.FAILED, st.UNCERTAIN):
            return False
        self.store.update_delivery(d.id, status=st.PENDING, next_at=int(time.time()), error="",
                                   reset_attempts=True)
        self.store.set_notified(d.post_id, False)
        return True

    # ================= отправка =================
    def _photo_path(self, post_id: int) -> str:
        return os.path.join(self._media_dir, f"{post_id}.jpg")

    async def _ensure_photo(self, post: st.Post, bot) -> Optional[str]:
        if not post.photo_file_id or bot is None:
            return None
        path = self._photo_path(post.id)
        if not os.path.exists(path):
            tg_file = await bot.get_file(post.photo_file_id)
            await tg_file.download_to_drive(custom_path=path)
        return path

    def _send_threads(self, post: st.Post, d: st.Delivery, photo: Optional[str]) -> Dict:
        parts = build_threads_parts(post.x_parts)
        if self.cfg.dry_run:
            self._log(f"🧪 DRY Threads #{post.id}: {len(parts)} пост(ов), фото={'да' if photo else 'нет'}\n"
                      + "\n---\n".join(parts))
            return {"url": "dry-run", "ids": []}
        image_url = None
        if photo:
            image_url = post.image_url
            if not image_url and self.cfg.imgbb_key:
                image_url = upload_imgbb(photo, self.cfg.imgbb_key)
                self.store.set_image_url(post.id, image_url)
            elif not image_url:
                self._log(f"⚠️ Crosspost #{post.id} Threads: нет IMGBB_API_KEY, публикую без фото")
        ids = self.threads.publish_chain(
            parts, image_url, d.remote_ids,
            on_progress=lambda done: self.store.update_delivery(d.id, remote_ids=done),
            sleep=self._sleep)
        return {"ids": ids, "url": self.threads.permalink(ids[0]) if ids else None}

    def _send_square(self, post: st.Post, d: st.Delivery, photo: Optional[str]) -> Dict:
        body, title = build_square_payload(post.x_parts, self.cfg.square_strip_links, self.cfg.square_max_chars)
        if not body.strip():
            raise PlatformError("пустой текст после очистки ссылок")
        if self.cfg.dry_run:
            self._log(f"🧪 DRY Square #{post.id}: {'статья «' + title + '»' if title else 'пост'}, "
                      f"{len(body)} симв., фото={'да' if photo else 'нет'}\n{body}")
            return {"url": "dry-run", "ids": []}
        pid, link = self.square.publish(body, title=title, image_paths=[photo] if photo else None,
                                        sleep=self._sleep)
        return {"ids": [pid] if pid else [], "url": link or ("" if pid else "опубликовано, ссылку API не вернул")}

    def _send(self, post: st.Post, d: st.Delivery, photo: Optional[str]) -> Dict:
        if d.platform == "threads":
            return self._send_threads(post, d, photo)
        if d.platform == "square":
            return self._send_square(post, d, photo)
        raise PlatformError(f"unknown platform {d.platform}")

    async def process_due(self, bot=None) -> int:
        due = self.store.claim_due(int(time.time()))
        for d in due:
            post = self.store.get_post(d.post_id)
            if post is None:
                self.store.update_delivery(d.id, status=st.FAILED, error="post not found")
                continue
            title = PLATFORM_TITLES.get(d.platform, d.platform)
            try:
                photo = await self._ensure_photo(post, bot)
                res = await asyncio.to_thread(self._send, post, d, photo)
                self.store.update_delivery(d.id, status=st.DONE, url=res.get("url") or "",
                                           remote_ids=res.get("ids") or d.remote_ids, error="")
                self._log(f"✅ Crosspost #{post.id} {title}: {res.get('url')}")
            except PlatformError as e:
                if e.uncertain:
                    status, nxt = st.UNCERTAIN, None
                elif e.retryable and d.attempts < self.cfg.max_attempts:
                    status, nxt = st.PENDING, int(time.time()) + _backoff(d.attempts)
                else:
                    status, nxt = st.FAILED, None
                self.store.update_delivery(d.id, status=status, next_at=nxt, error=str(e))
                self._log(f"⚠️ Crosspost #{post.id} {title}: {status} (попытка {d.attempts}): {e}")
            except Exception as e:  # неожиданная ошибка в коде/сети Telegram
                retry = d.attempts < self.cfg.max_attempts
                self.store.update_delivery(d.id, status=st.PENDING if retry else st.FAILED,
                                           next_at=int(time.time()) + _backoff(d.attempts) if retry else None,
                                           error=f"{type(e).__name__}: {e}")
                self._log(f"❌ Crosspost #{post.id} {title}: {e}\n{traceback.format_exc()}")
        return len(due)

    # ================= уведомления =================
    def build_report(self, post: st.Post) -> tuple:
        lines = [f"🔁 Кросспост #{post.id}{' [DRY RUN]' if self.cfg.dry_run else ''}"]
        retry_ids = []
        for d in self.store.deliveries(post.id):
            title = PLATFORM_TITLES.get(d.platform, d.platform)
            if d.status == st.DONE:
                lines.append(f"✅ {title}: {d.url or 'опубликовано'}")
            elif d.status == st.CANCELLED:
                lines.append(f"⏭️ {title}: отменён ({d.error or 'правка'})")
            elif d.status == st.UNCERTAIN:
                lines.append(f"❓ {title}: статус неизвестен, проверь вручную. {d.error or ''}")
                retry_ids.append((d.id, title))
            else:
                lines.append(f"❌ {title}: {d.error or d.status}")
                retry_ids.append((d.id, title))
        return "\n".join(lines), retry_ids

    async def notify_finished(self, bot) -> None:
        if bot is None or not self.cfg.notify_chat_id:
            for p in self.store.posts_to_notify():
                self.store.set_notified(p.id)
                self._cleanup_media(p.id)
            return
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        for p in self.store.posts_to_notify():
            text, retry_ids = self.build_report(p)
            markup = None
            if retry_ids:
                markup = InlineKeyboardMarkup([[InlineKeyboardButton(
                    f"🔄 Повторить {t}", callback_data=f"{RETRY_CALLBACK_PREFIX}{i}")] for i, t in retry_ids])
            try:
                await bot.send_message(chat_id=self.cfg.notify_chat_id, text=text, reply_markup=markup,
                                       disable_web_page_preview=True)
                self.store.set_notified(p.id)
                if not retry_ids:
                    self._cleanup_media(p.id)
            except Exception as e:
                n = self._notify_failures.get(p.id, 0) + 1
                self._notify_failures[p.id] = n
                self._log(f"⚠️ Crosspost уведомление #{p.id} не отправлено ({n}/5): {e}")
                if n >= 5:  # не долбим Telegram вечно, если чат недоступен
                    self.store.set_notified(p.id)

    def _cleanup_media(self, post_id: int) -> None:
        try:
            os.remove(self._photo_path(post_id))
        except OSError:
            pass

    def cleanup_old_media(self, max_age_sec: int = 3 * 86400) -> None:
        now = time.time()
        for name in os.listdir(self._media_dir):
            path = os.path.join(self._media_dir, name)
            try:
                if now - os.path.getmtime(path) > max_age_sec:
                    os.remove(path)
            except OSError:
                pass

    # ================= фоновые задачи =================
    async def run_worker(self, bot) -> None:
        n = self.store.recover_interrupted()
        if n:
            self._log(f"⚠️ Crosspost: {n} отправок прервались при рестарте, помечены как «неизвестно»")
        self._log(f"🔁 Crosspost воркер запущен: {sorted(self.cfg.platforms)}, задержка {self.cfg.delay_seconds}s"
                  f"{', DRY RUN' if self.cfg.dry_run else ''}")
        last_cleanup = 0.0
        while True:
            try:
                await self.process_due(bot)
                await self.notify_finished(bot)
                if time.time() - last_cleanup > 3600:
                    self.cleanup_old_media()
                    last_cleanup = time.time()
            except Exception as e:
                self._log(f"❌ Crosspost воркер: {e}\n{traceback.format_exc()}")
            await asyncio.sleep(self.cfg.poll_seconds)

    async def run_token_refresh(self, bot) -> None:
        if not self.threads:
            return
        while True:
            err = await asyncio.to_thread(self.threads.refresh_token_if_needed)
            if err:
                self._log(f"⚠️ Threads token: {err}")
                if bot is not None and self.cfg.notify_chat_id:
                    try:
                        await bot.send_message(chat_id=self.cfg.notify_chat_id,
                                               text=f"⚠️ Threads: не удалось продлить токен.\n{err}")
                    except Exception:
                        pass
            await asyncio.sleep(6 * 3600)
