"""Binance Square OpenAPI: публикация поста/статьи и загрузка картинок.

Источник контракта: github.com/binance/binance-skills-hub, skills/binance/square-post/scripts/lib.mjs
API умеет только создавать посты: нет удаления, правки, чтения.
"""
from __future__ import annotations

import os
import time
from typing import Callable, List, Optional, Tuple

import requests

from .errors import PlatformError

BASE_V1 = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi"
BASE_V2 = "https://www.binance.com/bapi/composite/v2/public/pgc/openApi"
TIMEOUT = 30

# Ошибки, при которых повтор бессмыслен.
_PERMANENT = {
    "220003": "API key not found",
    "220004": "API key expired",
    "220009": "дневной лимит постов OpenAPI",
    "220014": "дневной лимит загрузок",
    "20002": "запрещённые слова",
    "20022": "запрещённые слова",
    "20013": "превышена длина",
    "20020": "пустой текст",
    "220011": "пустой текст",
    "30008": "ограничение аккаунта",
    "2000001": "ограничение аккаунта/устройства",
    "2000002": "ограничение аккаунта/устройства",
}

_CT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}


class SquareClient:
    def __init__(self, api_key: str, session: Optional[requests.Session] = None):
        self._key = api_key
        self._http = session or requests.Session()

    def _headers(self) -> dict:
        return {"X-Square-OpenAPI-Key": self._key, "Content-Type": "application/json", "clienttype": "binanceSkill"}

    def _api(self, endpoint: str, body: dict, base: str = BASE_V2, is_publish: bool = False):
        try:
            r = self._http.post(f"{base}{endpoint}", json=body, headers=self._headers(), timeout=TIMEOUT)
        except requests.ConnectionError as e:
            # Соединение не установилось: пост точно не ушёл.
            raise PlatformError(f"Square connection error: {e}", retryable=True)
        except requests.RequestException as e:
            if is_publish:
                raise PlatformError(f"Square timeout on publish (пост мог выйти): {e}", uncertain=True)
            raise PlatformError(f"Square network error on {endpoint}: {e}", retryable=True)
        if is_publish and r.status_code == 504:
            # Официальный скрипт трактует 504 как «опубликовано, но без id».
            return {"id": None, "shareLink": None, "publishStatus": "success_without_post_id"}
        try:
            data = r.json()
        except ValueError:
            if is_publish:
                raise PlatformError(f"Square non-JSON on publish HTTP {r.status_code}", uncertain=True)
            raise PlatformError(f"Square non-JSON HTTP {r.status_code}", retryable=r.status_code >= 500)
        code = str(data.get("code"))
        if code != "000000":
            msg = data.get("message") or data.get("messageDetail") or ""
            hint = _PERMANENT.get(code)
            if hint:
                raise PlatformError(f"Square [{code}] {hint}: {msg}", retryable=False)
            if is_publish and r.status_code >= 500:
                raise PlatformError(f"Square [{code}] HTTP {r.status_code}: {msg}", uncertain=True)
            raise PlatformError(f"Square [{code}] HTTP {r.status_code}: {msg}", retryable=r.status_code >= 500)
        return data.get("data")

    def upload_image(self, path: str, sleep: Callable[[float], None] = time.sleep) -> str:
        name = os.path.basename(path)
        ctype = _CT.get(os.path.splitext(name)[1].lower(), "image/jpeg")
        pre = self._api("/image/presignedUrl", {"imageName": name}) or {}
        url, ticket = pre.get("presignedUrl"), pre.get("fileTicket")
        if not url or not ticket:
            raise PlatformError(f"Square presignedUrl: bad response {pre}", retryable=True)
        with open(path, "rb") as f:
            blob = f.read()
        try:
            put = self._http.put(url, data=blob, headers={"Content-Type": ctype}, timeout=60)
        except requests.RequestException as e:
            raise PlatformError(f"Square S3 upload error: {e}", retryable=True)
        if not put.ok:
            raise PlatformError(f"Square S3 upload HTTP {put.status_code}", retryable=True)
        for _ in range(10):
            st = self._api("/image/imageStatus", {"fileTicket": ticket}) or {}
            if st.get("status") == 1 and st.get("imageUrl"):
                return str(st["imageUrl"])
            if st.get("status") == 2:
                raise PlatformError(f"Square image processing failed: {st.get('failedReason')}")
            sleep(3)
        raise PlatformError("Square image processing timeout", retryable=True)

    def publish(self, text: str, title: Optional[str] = None, image_paths: Optional[List[str]] = None,
                sleep: Callable[[float], None] = time.sleep) -> Tuple[Optional[str], Optional[str]]:
        """Возвращает (post_id, share_link). Оба могут быть None при 504 (пост вышел без id)."""
        body: dict = {"bodyTextOnly": text}
        images = [self.upload_image(p, sleep) for p in (image_paths or [])[:4]]
        if title:
            body["contentType"] = 2
            body["title"] = title
            if images:
                body["cover"] = images[0]
        else:
            body["contentType"] = 1
            if images:
                body["imageList"] = images
        res = self._api("/content/add", body, base=BASE_V1, is_publish=True) or {}
        pid = res.get("id")
        return (str(pid) if pid else None), res.get("shareLink")
