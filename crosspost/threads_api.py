"""Meta Threads API: публикация, цепочка ответов, удаление, продление токена.

Docs: https://developers.facebook.com/docs/threads/posts
"""
from __future__ import annotations

import json
import os
import time
from typing import Callable, List, Optional

import requests

from .errors import PlatformError

BASE = "https://graph.threads.net/v1.0"
REFRESH_URL = "https://graph.threads.net/refresh_access_token"
TIMEOUT = 30
CONTAINER_POLL_SECONDS = 2
CONTAINER_POLL_MAX = 45  # ~90 c на обработку картинки


class ThreadsClient:
    def __init__(self, env_token: str, token_file: str, session: Optional[requests.Session] = None):
        self._env_token = env_token
        self._token_file = token_file
        self._http = session or requests.Session()
        self._user_id: Optional[str] = None

    # ---------- токен ----------
    def _load_file(self) -> dict:
        try:
            with open(self._token_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @property
    def token(self) -> str:
        data = self._load_file()
        # Файл свежее .env, только если продлевали токен, выданный из того же .env.
        if data.get("access_token") and data.get("source_token_tail") == self._env_token[-12:]:
            return data["access_token"]
        return self._env_token

    def _token_expires_at(self) -> int:
        try:
            r = self._http.get(f"{BASE}/debug_token", params={"input_token": self.token, "access_token": self.token},
                               timeout=TIMEOUT)
            return int(((r.json() or {}).get("data") or {}).get("expires_at") or 0)
        except (requests.RequestException, ValueError, TypeError):
            return 0

    def expires_at(self) -> int:
        data = self._load_file()
        if data.get("source_token_tail") == self._env_token[-12:] and data.get("expires_at"):
            return int(data["expires_at"])
        return self._token_expires_at()

    def refresh_token_if_needed(self, min_interval_sec: int = 20 * 3600,
                                renew_before_sec: int = 30 * 86400) -> Optional[str]:
        """Продлевает long-lived токен (ещё 60 дней). Возвращает текст ошибки или None."""
        data = self._load_file()
        same_source = data.get("source_token_tail") == self._env_token[-12:]
        last = int(data.get("refreshed_at") or 0) if same_source else 0
        if time.time() - last < min_interval_sec:
            return None
        expires_at = int(data.get("expires_at") or 0) if same_source else 0
        if not expires_at:
            expires_at = self._token_expires_at()
        if not expires_at:
            return None  # срок узнать не удалось (сеть, временный блок Meta): проверим в следующий раз
        if expires_at - time.time() > renew_before_sec:
            return None  # рано: Meta не продлевает токен моложе 24 ч, а продлевать заранее незачем
        try:
            r = self._http.get(
                REFRESH_URL,
                params={"grant_type": "th_refresh_token", "access_token": self.token},
                timeout=TIMEOUT,
            )
            body = r.json()
        except (requests.RequestException, ValueError) as e:
            return f"refresh network error: {e}"
        new_token = body.get("access_token")
        if r.status_code != 200 or not new_token:
            return f"refresh failed: HTTP {r.status_code} {str(body)[:300]}"
        payload = {
            "access_token": new_token,
            "expires_at": int(time.time()) + int(body.get("expires_in") or 0),
            "refreshed_at": int(time.time()),
            "source_token_tail": self._env_token[-12:],
        }
        tmp = self._token_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, self._token_file)
        try:
            os.chmod(self._token_file, 0o600)
        except OSError:
            pass
        return None

    # ---------- HTTP ----------
    def _call(self, method: str, path: str, params: dict, uncertain_on_timeout: bool = False) -> dict:
        params = dict(params)
        params["access_token"] = self.token
        try:
            r = self._http.request(method, f"{BASE}/{path}", data=params if method == "POST" else None,
                                   params=None if method == "POST" else params, timeout=TIMEOUT)
        except requests.Timeout as e:
            raise PlatformError(f"Threads timeout on {path}: {e}", retryable=not uncertain_on_timeout,
                                uncertain=uncertain_on_timeout)
        except requests.RequestException as e:
            raise PlatformError(f"Threads network error on {path}: {e}", retryable=True)
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text[:300]}
        if r.status_code >= 400 or (isinstance(body, dict) and body.get("error")):
            err = body.get("error") if isinstance(body, dict) else None
            msg = (err or {}).get("message") if isinstance(err, dict) else str(body)[:300]
            code = (err or {}).get("code") if isinstance(err, dict) else None
            # 4/17/32/613 = rate limit, 1/2 = временная ошибка, 5xx = сервер.
            retryable = r.status_code >= 500 or code in (1, 2, 4, 17, 32, 613)
            if uncertain_on_timeout and r.status_code >= 500:
                raise PlatformError(f"Threads {r.status_code} on {path}: {msg}", uncertain=True)
            raise PlatformError(f"Threads HTTP {r.status_code} code={code}: {msg}", retryable=retryable)
        return body

    def user_id(self) -> str:
        if not self._user_id:
            self._user_id = str(self._call("GET", "me", {"fields": "id,username"})["id"])
        return self._user_id

    def username(self) -> str:
        return str(self._call("GET", "me", {"fields": "username"}).get("username") or "")

    def _wait_container(self, container_id: str, sleep: Callable[[float], None]) -> None:
        for _ in range(CONTAINER_POLL_MAX):
            st = self._call("GET", container_id, {"fields": "status,error_message"})
            status = st.get("status")
            if status in ("FINISHED", "PUBLISHED"):
                return
            if status in ("ERROR", "EXPIRED"):
                raise PlatformError(f"Threads container {status}: {st.get('error_message')}", retryable=False)
            sleep(CONTAINER_POLL_SECONDS)
        raise PlatformError("Threads container not ready in time", retryable=True)

    def publish(self, text: str, image_url: Optional[str] = None, reply_to_id: Optional[str] = None,
                sleep: Callable[[float], None] = time.sleep) -> str:
        """Публикует один пост, возвращает media id."""
        uid = self.user_id()
        params = {"media_type": "IMAGE" if image_url else "TEXT", "text": text}
        if image_url:
            params["image_url"] = image_url
        if reply_to_id:
            params["reply_to_id"] = reply_to_id
        container = self._call("POST", f"{uid}/threads", params)
        cid = container.get("id")
        if not cid:
            raise PlatformError(f"Threads: no container id in {container}", retryable=True)
        self._wait_container(cid, sleep)
        res = self._call("POST", f"{uid}/threads_publish", {"creation_id": cid}, uncertain_on_timeout=True)
        mid = res.get("id")
        if not mid:
            raise PlatformError(f"Threads: publish returned no id: {res}", uncertain=True)
        return str(mid)

    def permalink(self, media_id: str) -> Optional[str]:
        try:
            return self._call("GET", media_id, {"fields": "permalink"}).get("permalink")
        except PlatformError:
            return None

    def delete(self, media_id: str) -> None:
        """Требует право threads_delete."""
        self._call("DELETE", media_id, {})

    def publish_chain(self, parts: List[str], image_url: Optional[str], done_ids: List[str],
                      on_progress: Callable[[List[str]], None],
                      sleep: Callable[[float], None] = time.sleep) -> List[str]:
        """Публикует цепочку. done_ids: уже опубликованные части (продолжаем с места сбоя)."""
        ids = list(done_ids)
        for idx in range(len(ids), len(parts)):
            reply_to = ids[-1] if ids else None
            mid = self.publish(parts[idx], image_url=image_url if idx == 0 else None,
                               reply_to_id=reply_to, sleep=sleep)
            ids.append(mid)
            on_progress(ids)
            if idx + 1 < len(parts):
                sleep(2)
        return ids
