import base64
import json
import os
import requests
import asyncio
import re
import tempfile
import time
from typing import List, Optional, Tuple
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CallbackQueryHandler
from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
from requests_oauthlib import OAuth1

# .env рядом с main.py — так ключи видны и при запуске через systemd
# override=True: значения из .env перезаписывают env, иначе на сервере могли остаться пустые
_load_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_load_env_path, override=True)

# Функция автоматической очистки ключей
def clean_token(token_name):
    val = os.getenv(token_name, "").strip()
    # Убираем URL-кодировку, если она попала в .env
    return val.replace("%3D", "=").replace("%3d", "=")

# AI для перевода: Grok (xAI) - лучше для X, Gemini - альтернатива
XAI_API_KEY = clean_token("XAI_API_KEY")
GEMINI_KEY = clean_token("GEMINI_API_KEY")
TELEGRAM_TOKEN = clean_token("TELEGRAM_BOT_TOKEN")

# Выбор AI: если есть XAI_API_KEY - используем Grok для X постов, иначе Gemini
USE_GROK_FOR_X = bool(XAI_API_KEY)

# OAuth 2.0 X: access + refresh (refresh нужен для автообновления при 401)
X_USER_ACCESS_TOKEN = clean_token("X_USER_ACCESS_TOKEN")
X_REFRESH_TOKEN = clean_token("X_REFRESH_TOKEN")
X_CLIENT_ID = clean_token("X_CLIENT_ID")
X_CLIENT_SECRET = clean_token("X_CLIENT_SECRET")

# Файл с актуальными токенами после refresh (чтобы не терять после перезапуска)
_X_TOKENS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".x_tokens.json")
_AUTOPOST_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".autopost_state.json")
# Текущий access token в памяти (обновляется при refresh)
_x_access_token: Optional[str] = None
_x_refresh_token: Optional[str] = None


def _load_x_tokens() -> Tuple[str, Optional[str]]:
    """Загружаем access (и refresh) из файла или из .env."""
    global _x_access_token, _x_refresh_token
    if os.path.isfile(_X_TOKENS_FILE):
        try:
            with open(_X_TOKENS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            at = (data.get("access_token") or "").strip()
            rt = (data.get("refresh_token") or "").strip() or None
            if at:
                _x_access_token = at
                _x_refresh_token = rt or X_REFRESH_TOKEN or None
                return _x_access_token, _x_refresh_token
        except Exception:
            pass
    _x_access_token = X_USER_ACCESS_TOKEN
    _x_refresh_token = X_REFRESH_TOKEN or None
    return _x_access_token, _x_refresh_token


def _get_x_access_token() -> str:
    """Текущий access token (из файла или .env); при первом вызове подгружаем."""
    global _x_access_token
    if _x_access_token is None:
        _load_x_tokens()
    return _x_access_token or X_USER_ACCESS_TOKEN or ""


def _save_x_tokens(access_token: str, refresh_token: Optional[str]) -> None:
    """Сохраняем токены в файл после успешного refresh."""
    try:
        with open(_X_TOKENS_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"access_token": access_token, "refresh_token": refresh_token or ""},
                f,
                ensure_ascii=False,
            )
    except Exception:
        pass


def _load_autopost_state() -> dict:
    if os.path.isfile(_AUTOPOST_STATE_FILE):
        try:
            with open(_AUTOPOST_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_autopost_state(state: dict) -> None:
    try:
        with open(_AUTOPOST_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception:
        pass


def _refresh_x_token() -> Optional[str]:
    """
    Обновляем access token по refresh_token. Возвращает новый access_token или None.
    """
    global _x_access_token, _x_refresh_token
    refresh = _x_refresh_token or X_REFRESH_TOKEN
    if not refresh or not X_CLIENT_ID or not X_CLIENT_SECRET:
        return None
    url = "https://api.x.com/2/oauth2/token"
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": X_CLIENT_ID,
    }
    basic = base64.b64encode(
        f"{X_CLIENT_ID}:{X_CLIENT_SECRET}".encode("utf-8")
    ).decode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {basic}",
    }
    try:
        resp = requests.post(url, data=data, headers=headers, timeout=20)
        if resp.status_code != 200:
            return None
        payload = resp.json()
        new_access = (payload.get("access_token") or "").strip()
        new_refresh = (payload.get("refresh_token") or "").strip() or refresh
        if new_access:
            _x_access_token = new_access
            _x_refresh_token = new_refresh or _x_refresh_token
            _save_x_tokens(_x_access_token, _x_refresh_token)
            return _x_access_token
    except Exception:
        pass
    return None


# OAuth 1.0a ключи X (нужны для загрузки media; можно также постить ими)
X_API_KEY = clean_token("X_API_KEY")
X_API_SECRET = clean_token("X_API_SECRET")
X_ACCESS_TOKEN = clean_token("X_ACCESS_TOKEN")
X_ACCESS_TOKEN_SECRET = clean_token("X_ACCESS_TOKEN_SECRET")

# Farcaster (через Neynar)
NEYNAR_API_KEY = clean_token("NEYNAR_API_KEY")
NEYNAR_SIGNER_UUID = clean_token("NEYNAR_SIGNER_UUID")

# Imgbb для загрузки изображений в Farcaster (альтернатива Imgur)
IMGBB_API_KEY = clean_token("IMGBB_API_KEY")

# Автопостинг из каналов
AUTOPOST_ENABLED = os.getenv("AUTOPOST_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
# Парсим ID каналов (могут быть отрицательными, например -1001423540718)
_autopost_channels_str = os.getenv("AUTOPOST_CHANNEL_IDS", "").strip()
AUTOPOST_CHANNEL_IDS = []
if _autopost_channels_str:
    for cid in _autopost_channels_str.split(","):
        cid = cid.strip()
        try:
            AUTOPOST_CHANNEL_IDS.append(int(cid))
        except ValueError:
            pass  # Пропускаем невалидные значения
# ID чата для уведомлений об автопостинге (ваш личный чат с ботом или канал для логов)
AUTOPOST_NOTIFY_CHAT_ID = None
_notify_chat_id_str = os.getenv("AUTOPOST_NOTIFY_CHAT_ID", "").strip()
if _notify_chat_id_str and _notify_chat_id_str.isdigit():
    AUTOPOST_NOTIFY_CHAT_ID = int(_notify_chat_id_str)

# Кастомные подсказки по проектам/тикерам для принудительной подсветки в X-постах.
# Формат: "backpack:@Backpack:$BKP,solana:@solana:$SOL"
PROJECT_ALIAS_HINTS = os.getenv("PROJECT_ALIAS_HINTS", "").strip()

# Лимиты
X_MAX_CHARS = 280
FARCASTER_MAX_BYTES = 320  # Farcaster лимит измеряется в байтах UTF-8
X_TCO_URL_LEN = 23  # приближение: X считает каждый URL как фиксированную длину
AUTOPOST_EDIT_WINDOW_SECONDS = 300  # 5 минут: удаляем старые и репостим после редактирования

# Grok (xAI) для X постов - лучше понимает алгоритм X
# Используем OpenAI-compatible API формат
if XAI_API_KEY:
    client_grok = XAI_API_KEY  # Сохраняем ключ для использования в запросах
    GROK_MODEL = "grok-4-latest"  # или "grok-4-1-fast-non-reasoning" для экономии ($0.28-0.50)
    USE_GROK_FOR_X = True
else:
    client_grok = None
    USE_GROK_FOR_X = False

# Gemini: новый SDK (google-genai) + api_version v1 — квота у тебя на gemini-2.5-flash
if GEMINI_KEY:
    client_ai = genai.Client(
        api_key=GEMINI_KEY,
        http_options=genai_types.HttpOptions(api_version="v1"),
    )
    MODEL_NAME = "gemini-2.5-flash"
else:
    client_ai = None
    MODEL_NAME = None


def normalize_social_text(text: str) -> str:
    """
    Нормализуем текст для X + Farcaster:
    - удаляем Markdown-символы (**, _, `) — X их не рендерит и выглядит грязно
    - нормализуем пробелы, сохраняем переносы строк
    """
    t = text.strip()
    t = re.sub(r"[*_`]+", "", t)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


_URL_RE = re.compile(r"https?://\S+")


def x_effective_length(text: str) -> int:
    """
    Приближённый подсчёт длины для X:
    - каждый URL считается как фиксированная длина (t.co)
    - остальное считаем как обычные символы
    """
    t = text or ""
    urls = _URL_RE.findall(t)
    t_wo = _URL_RE.sub("", t)
    return len(t_wo) + len(urls) * X_TCO_URL_LEN


def _avoid_cutting_url(text: str) -> str:
    """
    Если обрезали внутри URL, удаляем "хвост" до пробела/переноса.
    """
    m = re.search(r"https?://\S*$", text)
    if not m:
        return text
    return text[: m.start()].rstrip()


def fits_limits(text: str) -> bool:
    return (x_effective_length(text) <= X_MAX_CHARS) and (
        len(text.encode("utf-8")) <= FARCASTER_MAX_BYTES
    )


def clamp_to_limits(text: str) -> str:
    """
    Гарантированно подгоняем под лимиты X (символы) и Farcaster (байты).
    Стараемся резать по границе предложения/строки, добавляя многоточие.
    """
    t = normalize_social_text(text)
    if fits_limits(t):
        return t

    # Подгоняем под Farcaster (байты) и X (effective length)
    while len(t.encode("utf-8")) > FARCASTER_MAX_BYTES and len(t) > 0:
        t = t[:-1]
    while x_effective_length(t) > X_MAX_CHARS and len(t) > 0:
        t = t[:-1]

    # Попытка "красивого" обрезания
    candidates = []
    for sep in ("\n\n", "\n", ". ", "! ", "? ", "; ", ": ", ", "):
        idx = t.rfind(sep)
        if idx > 40:
            candidates.append(t[:idx].rstrip())
    if candidates:
        t = max(candidates, key=len)

    t = _avoid_cutting_url(t)

    # Добавим многоточие, если пришлось резать
    t = t.rstrip()
    if not t.endswith("…"):
        t = (t[:-1] if t.endswith(".") else t).rstrip()
        t = f"{t}…"

    # Финальная гарантия лимитов
    while len(t.encode("utf-8")) > FARCASTER_MAX_BYTES and len(t) > 0:
        t = t[:-1]
    while x_effective_length(t) > X_MAX_CHARS and len(t) > 0:
        t = t[:-1]
    return t.strip()


def x_tweet_url(tweet_id: Optional[str]) -> Optional[str]:
    if not tweet_id:
        return None
    return f"https://x.com/i/web/status/{tweet_id}"


def farcaster_cast_url(cast_hash: Optional[str]) -> Optional[str]:
    if not cast_hash:
        return None
    return f"https://warpcast.com/~/conversations/{cast_hash}"


def _build_project_entity_hints(source_text: str) -> str:
    """
    Собирает подсказки для модели по @/$ из PROJECT_ALIAS_HINTS на основе текста поста.
    Возвращает блок строк для промпта.
    """
    if not PROJECT_ALIAS_HINTS or not source_text:
        return ""
    src = source_text.lower()
    # Разрешаем тикер только если он уже явно присутствует в исходнике ($ABC)
    source_tickers = {m.group(0).upper() for m in re.finditer(r"\$[A-Za-z0-9_]{2,15}", source_text)}
    lines = []
    for item in PROJECT_ALIAS_HINTS.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        # key:@handle:$TICKER (ticker опционален)
        parts = [p.strip() for p in item.split(":") if p.strip()]
        if len(parts) < 2:
            continue
        key = parts[0]
        handle = parts[1] if parts[1].startswith("@") else f"@{parts[1]}"
        ticker = parts[2] if len(parts) > 2 else ""
        if key.lower() in src:
            if ticker and not ticker.startswith("$"):
                ticker = f"${ticker}"
            # Тикер в подсказку добавляем только если он есть в исходном тексте.
            # Это защищает от "выдуманных" тикеров вроде $BKP вместо фактического $BP.
            if ticker and ticker.upper() not in source_tickers:
                ticker = ""
            lines.append(f"- {key} -> {handle}" + (f" {ticker}" if ticker else ""))
    return "\n".join(lines)


def upload_media_to_x(
    photo_path: str,
    api_key: Optional[str] = None,
    api_secret: Optional[str] = None,
    access_token: Optional[str] = None,
    access_token_secret: Optional[str] = None,
) -> dict:
    """
    Загружаем фото в X через v1.1 media/upload (OAuth 1.0a).
    Ключи можно передать явно; иначе берутся из глобальных переменных.
    """
    key = api_key or X_API_KEY
    secret = api_secret or X_API_SECRET
    tok = access_token or X_ACCESS_TOKEN
    tok_secret = access_token_secret or X_ACCESS_TOKEN_SECRET
    if not (key and secret and tok and tok_secret):
        return {"ok": False, "error": "Missing X OAuth1 keys for media upload"}

    url = "https://upload.twitter.com/1.1/media/upload.json"
    auth = OAuth1(key, secret, tok, tok_secret)
    try:
        with open(photo_path, "rb") as f:
            resp = requests.post(url, files={"media": f}, auth=auth, timeout=30)
        data = resp.json()
        if resp.status_code in (200, 201) and data.get("media_id_string"):
            return {"ok": True, "media_id": data["media_id_string"]}
        return {"ok": False, "status": resp.status_code, "body": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def post_to_x(text: str, media_ids: Optional[List[str]] = None, reply_to_tweet_id: Optional[str] = None) -> dict:
    """
    Публикуем пост в X API v2.
    Всегда используем OAuth2 (Bearer) для твита — так не будет 401 при истёкших OAuth1.
    Фото загружаются отдельно через upload_media_to_x (OAuth1), сюда передаются уже media_ids.
    card_uri: "tombstone://card" убирает предпросмотр ссылок (link preview cards).
    reply_to_tweet_id: ID твита для ответа (создание треда).
    """
    url = "https://api.x.com/2/tweets"
    clean = clamp_to_limits(text)
    payload: dict = {"text": clean}
    if media_ids:
        payload["media"] = {"media_ids": media_ids}
    # Убираем предпросмотр ссылок - добавляем card_uri для отключения карточек
    # Важно: X API запрещает передавать одновременно media и card_uri (ошибка 400).
    # Поэтому card_uri ставим только когда НЕТ медиа.
    if (not media_ids) and _URL_RE.search(clean):
        payload["card_uri"] = "tombstone://card"
    # Если это ответ в треде - добавляем reply
    if reply_to_tweet_id:
        payload["reply"] = {"in_reply_to_tweet_id": reply_to_tweet_id}

    try:
        headers = {
            "Authorization": f"Bearer {_get_x_access_token()}",
            "Content-Type": "application/json",
        }
        resp = requests.post(url, json=payload, headers=headers, timeout=20)

        data = resp.json()
        if resp.status_code in (200, 201) and "data" in data:
            return {"ok": True, "id": data["data"].get("id")}
        # При 401 пробуем один раз обновить токен по refresh_token и повторить
        if resp.status_code == 401 and _refresh_x_token():
            headers = {
                "Authorization": f"Bearer {_get_x_access_token()}",
                "Content-Type": "application/json",
            }
            resp2 = requests.post(url, json=payload, headers=headers, timeout=20)
            data = resp2.json()
            if resp2.status_code in (200, 201) and "data" in data:
                return {"ok": True, "id": data["data"].get("id")}
            return {"ok": False, "status": resp2.status_code, "body": data}
        return {"ok": False, "status": resp.status_code, "body": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def upload_image_to_imgbb(photo_path: str, api_key: Optional[str] = None) -> dict:
    """
    Загружаем фото в Imgbb (альтернатива Imgur).
    Возвращает {"ok": True, "url": "https://..."} или {"ok": False, "error": "..."}.
    """
    key = api_key or IMGBB_API_KEY
    if not key:
        return {"ok": False, "error": "Missing IMGBB_API_KEY in .env"}
    
    url = "https://api.imgbb.com/1/upload"
    try:
        with open(photo_path, "rb") as f:
            files = {"image": f}
            data = {"key": key}
            resp = requests.post(url, files=files, data=data, timeout=30)
        result = resp.json()
        if resp.status_code == 200 and result.get("success"):
            image_url = result.get("data", {}).get("url")
            if image_url:
                return {"ok": True, "url": image_url}
        return {"ok": False, "status": resp.status_code, "body": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def post_to_farcaster(text: str, embeds: Optional[List[str]] = None, reply_to_hash: Optional[str] = None):
    """
    Публикуем пост в Farcaster через Neynar API.
    embeds: список URL для эмбедов (только изображения, не ссылки - чтобы не было предпросмотров).
    Ссылки остаются в тексте, но без предпросмотра карточек.
    reply_to_hash: hash родительского каста для ответа (создание треда).
    """
    if not NEYNAR_API_KEY or not NEYNAR_SIGNER_UUID:
        return {
            "ok": False,
            "error": "Missing NEYNAR_API_KEY or NEYNAR_SIGNER_UUID in .env",
        }

    url = "https://api.neynar.com/v2/farcaster/cast/"
    headers = {
        "x-api-key": NEYNAR_API_KEY,
        "Content-Type": "application/json",
    }
    clean = clamp_to_limits(text)
    payload = {"signer_uuid": NEYNAR_SIGNER_UUID, "text": clean}
    # Добавляем в embeds только изображения (не ссылки), чтобы ссылки были без предпросмотра
    if embeds:
        # Farcaster API ожидает массив объектов с полем "url", а не просто строки
        # Фильтруем только изображения (imgbb URLs), ссылки остаются в тексте без предпросмотра
        image_embeds = [{"url": url} for url in embeds if url.startswith(("https://i.", "https://i.imgbb.com", "http://i.", "http://i.imgbb.com"))]
        if image_embeds:
            payload["embeds"] = image_embeds
    # Если это ответ в треде - добавляем reply_to
    if reply_to_hash:
        payload["reply_to"] = reply_to_hash
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=20)
        data = resp.json()
        if resp.status_code == 200 and data.get("success"):
            cast_hash = (data.get("cast") or {}).get("hash")
            return {"ok": True, "hash": cast_hash, "cast": data.get("cast")}
        return {"ok": False, "status": resp.status_code, "body": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def delete_x_tweet(tweet_id: str) -> dict:
    if not tweet_id:
        return {"ok": False, "error": "Missing tweet_id"}
    url = f"https://api.x.com/2/tweets/{tweet_id}"
    try:
        headers = {"Authorization": f"Bearer {_get_x_access_token()}"}
        resp = requests.delete(url, headers=headers, timeout=20)
        data = resp.json() if resp.text else {}
        if resp.status_code in (200, 204):
            return {"ok": True}
        if resp.status_code == 401 and _refresh_x_token():
            headers = {"Authorization": f"Bearer {_get_x_access_token()}"}
            resp2 = requests.delete(url, headers=headers, timeout=20)
            data2 = resp2.json() if resp2.text else {}
            if resp2.status_code in (200, 204):
                return {"ok": True}
            return {"ok": False, "status": resp2.status_code, "body": data2}
        return {"ok": False, "status": resp.status_code, "body": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def delete_farcaster_cast(cast_hash: str) -> dict:
    if not cast_hash:
        return {"ok": False, "error": "Missing cast hash"}
    if not NEYNAR_API_KEY or not NEYNAR_SIGNER_UUID:
        return {"ok": False, "error": "Missing NEYNAR_API_KEY or NEYNAR_SIGNER_UUID"}
    url = "https://api.neynar.com/v2/farcaster/cast/"
    headers = {
        "x-api-key": NEYNAR_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {"signer_uuid": NEYNAR_SIGNER_UUID, "target_hash": cast_hash}
    try:
        resp = requests.delete(url, json=payload, headers=headers, timeout=20)
        data = resp.json() if resp.text else {}
        if resp.status_code in (200, 204):
            return {"ok": True}
        return {"ok": False, "status": resp.status_code, "body": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _utf16_units(c: str) -> int:
    """Один символ в Python = 1 или 2 единицы UTF-16 (суррогатная пара)."""
    o = ord(c)
    return 2 if 0x10000 <= o <= 0x10FFFF else 1


def _entity_span_to_char_indices(text: str, offset_utf16: int, length_utf16: int) -> Tuple[int, int]:
    """Переводит offset/length в UTF-16 в индексы символов (для среза text[start:end])."""
    if not text or offset_utf16 < 0 or length_utf16 <= 0:
        return 0, 0
    utf16_pos = 0
    start_char = None
    end_char = None
    for i, c in enumerate(text):
        if utf16_pos == offset_utf16:
            start_char = i
        if start_char is not None and utf16_pos == offset_utf16 + length_utf16:
            end_char = i
            break
        utf16_pos += _utf16_units(c)
    if start_char is None:
        start_char = 0
    if end_char is None:
        end_char = len(text)
    return start_char, end_char


def build_enriched_text(text: str, entities: Optional[List] = None) -> str:
    """
    Собирает текст с явными URL и сохранением структуры из сущностей Telegram.
    - TEXT_LINK -> "link_text (url)" чтобы ссылки не терялись при переводе
    - SPOILER -> просто текст (раскрытый)
    - BOLD/ITALIC/CODE -> ** / _ / ` для сохранения акцентов
    """
    if not text:
        return ""
    if not entities:
        return text
    # Сортируем по offset (и по -length чтобы внешние сущности шли раньше вложенных)
    sorted_entities = sorted(
        (e for e in entities if getattr(e, "offset", None) is not None and getattr(e, "length", None) is not None),
        key=lambda e: (e.offset, -e.length),
    )
    # Строим список отрезков (start, end, replacement или None = оставить как есть)
    segments = []
    for entity in sorted_entities:
        start, end = _entity_span_to_char_indices(text, entity.offset, entity.length)
        if start >= end:
            continue
        slice_text = text[start:end]
        etype = getattr(entity, "type", None) or ""
        replacement = None
        if etype == "text_link":
            url = getattr(entity, "url", None)
            if url:
                replacement = f"{slice_text} ({url})"
        elif etype == "spoiler":
            replacement = slice_text  # раскрытый спойлер — просто текст
        elif etype == "bold":
            replacement = f"**{slice_text}**"
        elif etype == "italic":
            replacement = f"_{slice_text}_"
        elif etype == "code":
            replacement = f"`{slice_text}`"
        if replacement is not None:
            segments.append((start, end, replacement))
    # Применяем с конца, чтобы индексы не сбивались
    result = text
    for start, end, repl in reversed(segments):
        result = result[:start] + repl + result[end:]
    return result


def extract_text_and_photos(message) -> Tuple[Optional[str], List]:
    """
    Возвращает (обогащённый текст с учётом ссылок/спойлеров/форматирования, photos).
    Работает для обычных и пересланных постов.
    """
    text = message.text or message.caption
    entities = getattr(message, "entities", None) or getattr(message, "caption_entities", None)
    if text and entities:
        text = build_enriched_text(text, list(entities))
    photos = list(message.photo) if getattr(message, "photo", None) else []
    return text, photos


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user_text, photos = extract_text_and_photos(message)

    if not user_text:
        await message.reply_text("Пришли текст поста (или фото/видео с подписью).")
        return

    # Сохраняем фото для постинга по кнопке (одно, самое большое)
    photo_file_id = photos[-1].file_id if photos else None
    context.user_data["last_photo_file_id"] = photo_file_id

    msg = await message.reply_text(
        "⏳ Перевожу на английский (сохраняю ссылки и структуру) и проверяю лимиты X + Farcaster..."
    )
    
    try:
        # Используем Grok для X постов если доступен, иначе Gemini
        translated = await _translate_section(user_text, for_x=True)

        if not translated:
            await msg.edit_text("❌ Не смог получить перевод. Попробуй ещё раз.")
            return

        # Если перевод уже влезает — показываем 1 вариант и одну кнопку
        if fits_limits(translated):
            final_text = clamp_to_limits(translated)
            context.user_data["post_0"] = final_text
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🚀 Запостить → X + Farcaster", callback_data="send_0")]]
            )
            await msg.edit_text(
                f"**EN (готово к постингу):**\n\n"
                f"({x_effective_length(final_text)}/{X_MAX_CHARS} X-chars, {len(final_text.encode('utf-8'))}/{FARCASTER_MAX_BYTES} bytes)\n\n"
                f"{final_text}",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
            return

        # Если не влезает — делаем 3 варианта сокращения через Gemini (Grok не поддерживает множественные варианты)
        options = []
        if client_ai and MODEL_NAME:
            prompt_shorten = f"""
You are an editor and social media growth expert. The post is instructional (list, links, tips). Shorten it to fit platform limits while keeping it useful AND adding engagement optimization.

Create 3 shortening variants. For EACH option:
- Preserve structure: keep bullet/list format and line breaks where possible.
- Keep as many "name (url)" links as fit; do NOT drop URLs or replace with "link" — keep real URLs.
- No markdown symbols in output (no ** or _ or `) — target is X/Farcaster plain text.
- Must fit: X effective length <= {X_MAX_CHARS} (each URL counts as {X_TCO_URL_LEN} chars), Farcaster <= {FARCASTER_MAX_BYTES} bytes UTF-8.
- Add 1-3 RELEVANT hashtags at the end (crypto/tech related: #Crypto #Web3 #AI #Tech #DeFi #NFT #Blockchain #Innovation #BuildInPublic etc.)
- Add @mentions if relevant (well-known projects: @ethereum, @solana, @OpenAI, etc.)

Options can differ by: how many list items included, short intro vs full intro, which links kept, which hashtags/mentions added.

Output EXACTLY:
Option 1: <text>
Option 2: <text>
Option 3: <text>

Full English translation to shorten:
{translated}
""".strip()

            response2 = client_ai.models.generate_content(model=MODEL_NAME, contents=prompt_shorten)
            options = re.findall(r"Option \d+: (.*?)(?=Option \d+:|$)", response2.text or "", re.DOTALL)

        if not options:
            final_text = clamp_to_limits(translated)
            context.user_data["post_0"] = final_text
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🚀 Запостить → X + Farcaster", callback_data="send_0")]]
            )
            await msg.edit_text(
                f"**EN (сократил по лимитам):**\n\n"
                f"({x_effective_length(final_text)}/{X_MAX_CHARS} X-chars, {len(final_text.encode('utf-8'))}/{FARCASTER_MAX_BYTES} bytes)\n\n"
                f"{final_text}",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
            return

        keyboard_rows = []
        pretty_blocks = []
        for i, opt in enumerate(options[:3]):
            clean_opt = clamp_to_limits(opt.strip().strip("[]"))
            context.user_data[f"post_{i}"] = clean_opt
            keyboard_rows.append(
                [InlineKeyboardButton(f"🚀 Вариант {i+1} → X + Farcaster", callback_data=f"send_{i}")]
            )
            pretty_blocks.append(
                f"Option {i+1} ({x_effective_length(clean_opt)}/{X_MAX_CHARS} X-chars, {len(clean_opt.encode('utf-8'))}/{FARCASTER_MAX_BYTES} bytes):\n{clean_opt}"
            )

        await msg.edit_text(
            "**Нужно сократить (выбери вариант):**\n\n" + "\n\n".join(pretty_blocks),
            reply_markup=InlineKeyboardMarkup(keyboard_rows),
            parse_mode="Markdown",
        )
    except Exception as e:
        ai_name = "Grok" if (USE_GROK_FOR_X and client_grok) else "Gemini"
        await msg.edit_text(f"❌ Ошибка {ai_name}: {e}")

async def process_and_upload_photo(photo_file_id, bot) -> Tuple[Optional[List[str]], Optional[List[str]], Optional[str]]:
    """
    Загружает фото из Telegram и загружает в X и Farcaster.
    Возвращает (media_ids для X, embeds для Farcaster, ошибка если есть).
    """
    if not photo_file_id:
        return None, None, None
    
    load_dotenv(_load_env_path, override=True)
    media_ids: Optional[List[str]] = None
    farcaster_embeds: Optional[List[str]] = None
    skipped_photo_reason = None
    tmp_path = None
    
    try:
        tg_file = await bot.get_file(photo_file_id)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(custom_path=tmp_path)
        
        # Загрузка для X (OAuth1)
        oauth1_key = (os.getenv("X_API_KEY") or "").strip()
        oauth1_secret = (os.getenv("X_API_SECRET") or "").strip()
        oauth1_token = (os.getenv("X_ACCESS_TOKEN") or "").strip()
        oauth1_token_secret = (os.getenv("X_ACCESS_TOKEN_SECRET") or "").strip()
        if oauth1_key and oauth1_secret and oauth1_token and oauth1_token_secret:
            up_x = upload_media_to_x(
                tmp_path,
                api_key=oauth1_key,
                api_secret=oauth1_secret,
                access_token=oauth1_token,
                access_token_secret=oauth1_token_secret,
            )
            if up_x.get("ok"):
                media_ids = [up_x["media_id"]]
            else:
                skipped_photo_reason = f"X media upload failed: {up_x}"
        
        # Загрузка для Farcaster (Imgbb)
        imgbb_key = (os.getenv("IMGBB_API_KEY") or "").strip()
        if imgbb_key:
            up_fc = upload_image_to_imgbb(tmp_path, api_key=imgbb_key)
            if up_fc.get("ok"):
                farcaster_embeds = [up_fc["url"]]
            elif not skipped_photo_reason:
                skipped_photo_reason = f"Farcaster image upload failed: {up_fc}"
    except Exception as e:
        skipped_photo_reason = f"telegram download/upload error: {e}"
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
    
    return media_ids, farcaster_embeds, skipped_photo_reason


def split_long_post(text: str) -> List[str]:
    """
    Разбивает длинный пост на несколько постов по пунктам/секциям.
    Используется для постов с множеством отдельных тем (например, список проектов).
    """
    # Ищем маркеры разделения: •, -, *, цифры с точкой, заголовки в **
    lines = text.split('\n')
    sections = []
    current_section = []
    
    for line in lines:
        line_stripped = line.strip()
        # Определяем начало новой секции
        is_section_start = (
            line_stripped.startswith(('•', '-', '*', '·')) or
            re.match(r'^\d+[\.\)]\s+', line_stripped) or
            (line_stripped.startswith('**') and line_stripped.endswith('**') and len(line_stripped) < 50)
        )
        
        if is_section_start and current_section:
            # Сохраняем предыдущую секцию
            section_text = '\n'.join(current_section).strip()
            if section_text and len(section_text) > 20:  # Минимальная длина секции
                sections.append(section_text)
            current_section = [line]
        else:
            current_section.append(line)
    
    # Добавляем последнюю секцию
    if current_section:
        section_text = '\n'.join(current_section).strip()
        if section_text and len(section_text) > 20:
            sections.append(section_text)
    
    # Если разбиение не удалось или получилось слишком много мелких частей - возвращаем исходный текст
    if len(sections) > 10 or len(sections) == 0:
        return [text]
    
    return sections


async def auto_post_to_socials(text: str, photo_file_id: Optional[str] = None, bot=None) -> dict:
    """
    Автоматически переводит текст и публикует в X + Farcaster без кнопок.
    Используется для автопостинга из каналов.
    Если пост очень длинный - разбивает на тред (thread) с ответами.
    """
    if not text:
        return {"ok": False, "error": "No text to post"}
    
    # Проверяем, нужно ли разбивать пост (если он явно длинный и структурированный)
    text_length = len(text.encode('utf-8'))
    should_split = text_length > 500 and ('•' in text or '\n-' in text or re.search(r'^\d+[\.\)]', text, re.MULTILINE))
    
    if should_split:
        # Разбиваем на секции
        sections = split_long_post(text)
        if len(sections) > 1:
            # Публикуем как тред (thread)
            return await _post_as_thread(sections, photo_file_id, bot)
    
    # Обычная публикация одного поста
    return await _post_single_section(text, photo_file_id, bot)


def _extract_social_ids(result: dict) -> Tuple[List[str], List[str]]:
    """
    Из результата автопостинга собирает списки ID X-постов и Farcaster hash.
    """
    x_ids: List[str] = []
    fc_hashes: List[str] = []
    if not result:
        return x_ids, fc_hashes

    if result.get("thread"):
        for r in ((result.get("x") or {}).get("replies") or []):
            if r.get("ok") and r.get("id"):
                x_ids.append(str(r.get("id")))
        for r in ((result.get("farcaster") or {}).get("replies") or []):
            if r.get("ok") and r.get("hash"):
                fc_hashes.append(str(r.get("hash")))
    else:
        rx = result.get("x") or {}
        rf = result.get("farcaster") or {}
        if rx.get("ok") and rx.get("id"):
            x_ids.append(str(rx.get("id")))
        if rf.get("ok") and rf.get("hash"):
            fc_hashes.append(str(rf.get("hash")))
    return x_ids, fc_hashes


def _delete_previous_social_posts(record: dict) -> Tuple[List[str], List[str]]:
    """
    Удаляет ранее опубликованные посты/касты по record.
    Возвращает (errors_x, errors_fc)
    """
    errors_x: List[str] = []
    errors_fc: List[str] = []
    for tweet_id in record.get("x_ids", []) or []:
        d = delete_x_tweet(str(tweet_id))
        if not d.get("ok"):
            errors_x.append(str(d))
    for cast_hash in record.get("fc_hashes", []) or []:
        d = delete_farcaster_cast(str(cast_hash))
        if not d.get("ok"):
            errors_fc.append(str(d))
    return errors_x, errors_fc


async def _post_as_thread(sections: List[str], photo_file_id: Optional[str] = None, bot=None) -> dict:
    """
    Публикует несколько секций как тред (thread) в X и Farcaster.
    Первый пост - основной, остальные - ответы с задержкой 2-3 секунды.
    """
    load_dotenv(_load_env_path, override=True)
    
    # Переводим все секции (для X используем Grok если доступен)
    translated_sections = []
    for section in sections:
        translated = await _translate_section(section, for_x=True)
        if translated:
            translated_sections.append(translated)
    
    if not translated_sections:
        return {"ok": False, "error": "Translation failed"}
    
    # Публикуем первый пост
    first_section = translated_sections[0]
    media_ids = None
    farcaster_embeds = None
    
    if photo_file_id and bot:
        media_ids, farcaster_embeds, _ = await process_and_upload_photo(photo_file_id, bot)
    
    result_x = post_to_x(first_section, media_ids=media_ids)
    result_fc = post_to_farcaster(first_section, embeds=farcaster_embeds)
    
    x_thread_id = result_x.get("id") if result_x.get("ok") else None
    fc_thread_hash = result_fc.get("hash") if result_fc.get("ok") else None
    
    # Публикуем остальные посты как ответы с задержкой
    x_replies = [result_x] if result_x.get("ok") else []
    fc_replies = [result_fc] if result_fc.get("ok") else []
    
    for i, section in enumerate(translated_sections[1:], start=1):
        # Задержка 2-3 секунды между постами (чтобы не триггерить rate limits)
        await asyncio.sleep(2.5)
        
        if x_thread_id:
            x_reply = post_to_x(section, reply_to_tweet_id=x_thread_id)
            x_replies.append(x_reply)
            if x_reply.get("ok"):
                x_thread_id = x_reply.get("id")  # Обновляем для следующего ответа
        
        if fc_thread_hash:
            fc_reply = post_to_farcaster(section, reply_to_hash=fc_thread_hash)
            fc_replies.append(fc_reply)
            if fc_reply.get("ok"):
                fc_thread_hash = fc_reply.get("hash")  # Обновляем для следующего ответа
    
    x_success = sum(1 for r in x_replies if r.get("ok"))
    fc_success = sum(1 for r in fc_replies if r.get("ok"))
    
    return {
        "ok": x_success > 0 or fc_success > 0,
        "thread": True,
        "posts_count": len(translated_sections),
        "x": {"success": x_success, "total": len(x_replies), "replies": x_replies},
        "farcaster": {"success": fc_success, "total": len(fc_replies), "replies": fc_replies},
    }


async def _translate_with_grok(text: str, for_x: bool = True) -> Optional[str]:
    """Переводит текст используя Grok (xAI) - оптимизировано для алгоритма X 2025."""
    if not client_grok:
        return None

    entity_hints = _build_project_entity_hints(text)
    
    prompt = f"""
You are Grok, an expert at creating viral X (Twitter) content optimized for MAXIMUM VIEWS and COMMENTS in 2025.

PRIMARY GOAL: Maximize views and comments, not hashtags. Hashtags are OPTIONAL - only add if they genuinely boost discoverability.

CRITICAL X ALGORITHM OPTIMIZATION (2025):
1. ENGAGEMENT VELOCITY: First 60 minutes are critical. Add hooks that prompt immediate replies/questions.
2. REPLIES > LIKES: End with a question, controversial statement, or call-to-action that provokes discussion.
3. SPECIFICITY: Use concrete details, numbers, specific examples instead of vague statements.
4. CONVERSATION STARTERS: Frame content to invite replies, not just passive consumption.
5. CONTROVERSY & DEBATE: If appropriate, add elements that spark discussion (but stay truthful).

TRANSLATION RULES:
- Translate from Russian to English naturally
- Preserve ALL URLs exactly: "link_text (https://...)" format
- Keep structure: bullets, line breaks, numbered lists
- Remove markdown (** _ `) - X doesn't render it well

VIRAL OPTIMIZATION (focus on views & comments):
- END WITH A QUESTION or controversial statement to drive replies (CRITICAL - this is more important than hashtags)
- Use specific numbers, metrics, concrete examples
- Make it conversational, engaging, and debate-worthy
- If the source mentions a project/token, HIGHLIGHT it for X discovery:
  - Use @mention if you know the correct official handle.
  - Use $TICKER ONLY if the ticker is explicitly present in the source text.
  - Do NOT invent handles/tickers. If you aren't sure, keep the plain name.
- HASHTAGS: Add ONLY if they genuinely help discoverability AND don't hurt readability. Skip hashtags if the tweet is already strong without them. If adding, use 1-2 max: #Crypto #Web3 #AI #Tech #DeFi #NFT #Blockchain #BuildInPublic #TechTwitter
- Prioritize engagement hooks over hashtags - a question at the end is worth more than 5 hashtags

OUTPUT: Only the optimized tweet text, ready to post. Max 280 chars for X. Focus on driving comments and views, not hashtag stuffing. No explanations.

PROJECT/TICKER HINTS (handles from config; ticker only if present in source):
{entity_hints if entity_hints else "- (none)"}

Original post:
{text}
""".strip()
    
    try:
        # Используем OpenAI-compatible API формат
        url = "https://api.x.ai/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {client_grok}",
        }
        payload = {
            "model": GROK_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": "You are Grok, expert at creating viral X (Twitter) content optimized for the 2025 algorithm."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "temperature": 0.7,
            "stream": False,
        }
        
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        data = resp.json()
        
        if resp.status_code == 200 and "choices" in data:
            raw_translated = (data["choices"][0]["message"]["content"] or "").strip()
            translated = normalize_social_text(raw_translated)
            translated = _avoid_cutting_url(translated)
            
            if not translated:
                return None
            
            if not fits_limits(translated):
                translated = clamp_to_limits(translated)
            
            final = clamp_to_limits(translated)
            print(f"✅ Grok: переведено и оптимизировано ({len(final)} chars)")
            return final
        else:
            print(f"❌ Grok API error: {data}")
            return None
    except Exception as e:
        print(f"Grok translation error: {e}")
        return None


async def _translate_with_gemini(text: str) -> Optional[str]:
    """Переводит текст используя Gemini (fallback)."""
    if not client_ai or not MODEL_NAME:
        return None
    
    prompt_translate = f"""
You are a professional translator and social media growth expert for crypto/tech content.

Translate the user's post to English and optimize it for maximum engagement on X (Twitter) and Farcaster. CRITICAL rules:

TRANSLATION:
- Preserve ALL URLs exactly: they appear as "link_text (https://...)". Keep every URL in the same form "translated_label (same_url)".
- Preserve structure: title, bullet lists (• or -), line breaks, paragraphs, numbered items.
- Keep **bold**, _italic_, `code` markers if present — they mark emphasis/structure.
- Do NOT add new facts, hype, or emojis. Minimal editing only.

ENGAGEMENT OPTIMIZATION (add at the end, 1-3 hashtags max):
- Analyze the content topic (crypto, AI, tech, web3, DeFi, NFT, blockchain, coding, startup, etc.)
- Add 1-3 RELEVANT hashtags from these categories (choose the most fitting):
  * Crypto/Web3: #Crypto #Web3 #DeFi #NFT #Blockchain #Bitcoin #Ethereum #Solana #CryptoNews
  * AI/Tech: #AI #MachineLearning #Tech #Innovation #Startup #TechNews #SoftwareDev #Coding
  * General: #BuildInPublic #TechTwitter #CryptoTwitter #Web3 #Innovation
- If the post mentions a specific project/token/platform, add @mention if it's a well-known account (e.g., @ethereum, @solana, @OpenAI, @VitalikButerin)
- Only add hashtags/mentions if they genuinely fit the content - don't force them
- Place hashtags at the end, separated by space
- Keep total length under 280 chars for X

OUTPUT FORMAT:
Output ONLY the translated and optimized text, ready to be posted. Same formatting and line breaks as the original, with hashtags/mentions added at the end if relevant.

User post:
{text}
""".strip()
    
    try:
        response = client_ai.models.generate_content(model=MODEL_NAME, contents=prompt_translate)
        raw_translated = (response.text or "").strip()
        translated = normalize_social_text(raw_translated)
        translated = _avoid_cutting_url(translated)
        
        if not translated:
            return None
        
        if not fits_limits(translated):
            prompt_shorten = f"""
You are an editor and social media growth expert. Shorten the post to fit platform limits while keeping it useful AND adding engagement optimization.

Shorten the post:
- Preserve structure: keep bullet/list format and line breaks where possible.
- Keep as many "name (url)" links as fit; do NOT drop URLs or replace with "link" — keep real URLs.
- No markdown symbols in output (no ** or _ or `) — target is X/Farcaster plain text.
- Must fit: X effective length <= {X_MAX_CHARS} (each URL counts as {X_TCO_URL_LEN} chars), Farcaster <= {FARCASTER_MAX_BYTES} bytes UTF-8.
- Add 1-3 RELEVANT hashtags at the end (crypto/tech related: #Crypto #Web3 #AI #Tech #DeFi #NFT #Blockchain #Innovation #BuildInPublic etc.)
- Add @mentions if relevant (well-known projects: @ethereum, @solana, @OpenAI, etc.)

Output ONLY the shortened and optimized text, ready to be posted.

Full English translation to shorten:
{translated}
""".strip()
            response2 = client_ai.models.generate_content(model=MODEL_NAME, contents=prompt_shorten)
            shortened = (response2.text or "").strip()
            if shortened:
                translated = clamp_to_limits(shortened)
            else:
                translated = clamp_to_limits(translated)
        
        final = clamp_to_limits(translated)
        print(f"✅ Gemini: переведено и оптимизировано ({len(final)} chars)")
        return final
    except Exception as e:
        print(f"❌ Gemini translation error: {e}")
        return None


async def _translate_section(text: str, for_x: bool = True) -> Optional[str]:
    """Переводит одну секцию текста. Использует Grok для X постов, Gemini для остального."""
    # Для X постов используем Grok если доступен
    if for_x and USE_GROK_FOR_X and client_grok:
        print(f"🤖 Используется Grok для перевода X поста...")
        result = await _translate_with_grok(text, for_x=True)
        if result:
            return result
        # Fallback на Gemini если Grok не сработал
        print("⚠️ Grok failed, falling back to Gemini")
        if client_ai and MODEL_NAME:
            return await _translate_with_gemini(text)
        return None
    
    # Используем Gemini для Farcaster или если Grok недоступен
    if client_ai and MODEL_NAME:
        print(f"🤖 Используется Gemini для перевода...")
        return await _translate_with_gemini(text)
    return None


async def _post_single_section(text: str, photo_file_id: Optional[str] = None, bot=None) -> dict:
    """
    Публикует одну секцию поста (вспомогательная функция).
    Использует Grok для X постов если доступен.
    """
    
    # Переводим текст (Grok для X, Gemini для остального)
    translated = await _translate_section(text, for_x=True)
    
    if not translated:
        return {"ok": False, "error": "Translation failed"}
    
    final_text = clamp_to_limits(translated)
    
    try:
        # Загружаем фото если есть
        media_ids = None
        farcaster_embeds = None
        if photo_file_id and bot:
            media_ids, farcaster_embeds, _ = await process_and_upload_photo(photo_file_id, bot)
        
        # Публикуем
        load_dotenv(_load_env_path, override=True)
        result_x = post_to_x(final_text, media_ids=media_ids)
        result_fc = post_to_farcaster(final_text, embeds=farcaster_embeds)
        
        return {
            "ok": result_x.get("ok") or result_fc.get("ok"),
            "x": result_x,
            "farcaster": result_fc,
            "text": final_text,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    idx = parts[1] if len(parts) > 1 else "0"

    text_to_post = context.user_data.get(f"post_{idx}")
    photo_file_id = context.user_data.get("last_photo_file_id")

    await query.edit_message_text(text="📤 Отправка в X + Farcaster...")

    # Используем общую функцию для загрузки фото
    media_ids, farcaster_embeds, skipped_photo_reason = await process_and_upload_photo(
        photo_file_id, context.bot
    )

    load_dotenv(_load_env_path, override=True)
    result_x = post_to_x(text_to_post, media_ids=media_ids)
    result_fc = post_to_farcaster(text_to_post, embeds=farcaster_embeds)

    lines = []
    if result_x.get("ok"):
        lines.append("✅ X: posted")
    else:
        err = result_x
        if err.get("status") == 401:
            lines.append(
                "❌ X: 401 Unauthorized — токен истёк.\n"
                "• Если в .env есть X_API_KEY и X_ACCESS_TOKEN (OAuth1) — постинг идёт по ним; при 401 автообновление только у OAuth2. Либо обнови OAuth1-токены в X Developer Portal, либо удали с сервера эти 4 переменные (X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET), тогда будет использоваться OAuth2 с автообновлением.\n"
                "• Для OAuth2 на сервере нужны X_REFRESH_TOKEN, X_CLIENT_ID, X_CLIENT_SECRET и актуальный .x_tokens.json или X_USER_ACCESS_TOKEN. После правок: systemctl restart smm_bot"
            )
        else:
            lines.append(f"❌ X: {err}")

    if result_fc.get("ok"):
        lines.append("✅ Farcaster: posted")
    else:
        lines.append(f"❌ Farcaster: {result_fc}")

    if skipped_photo_reason:
        lines.append(f"📷 Photo: skipped ({skipped_photo_reason})")

    lines.append("\nТекст:\n" + text_to_post)
    await query.edit_message_text(text="\n".join(lines))

async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик постов из каналов для автопостинга."""
    if not AUTOPOST_ENABLED:
        print(f"⚠️ Автопостинг отключен (AUTOPOST_ENABLED={AUTOPOST_ENABLED})")
        return
    
    # channel_post может быть в update.channel_post или update.edited_channel_post
    channel_post = update.channel_post or update.edited_channel_post
    if not channel_post:
        print("⚠️ Нет channel_post в update")
        return
    
    chat_id = channel_post.chat.id
    message_id = channel_post.message_id
    is_edited = bool(update.edited_channel_post)
    print(f"📢 Получен пост из канала {chat_id}")
    
    # Если указаны конкретные каналы — проверяем
    if AUTOPOST_CHANNEL_IDS and chat_id not in AUTOPOST_CHANNEL_IDS:
        print(f"⚠️ Канал {chat_id} не в списке разрешенных: {AUTOPOST_CHANNEL_IDS}")
        return
    
    # Игнорируем посты от самого бота (чтобы избежать циклов)
    if channel_post.from_user and channel_post.from_user.is_bot:
        print(f"⚠️ Игнорируем пост от бота")
        return
    
    user_text, photos = extract_text_and_photos(channel_post)
    if not user_text:
        print(f"⚠️ Нет текста в посте (только фото без подписи?)")
        return
    
    print(f"✅ Обрабатываем пост из канала {chat_id}, текст: {user_text[:50]}...")
    photo_file_id = photos[-1].file_id if photos else None

    # Если это редактирование в течение 5 минут — удаляем старые посты и публикуем заново
    state = _load_autopost_state()
    state_key = f"{chat_id}:{message_id}"
    prev_record = state.get(state_key)
    if is_edited and prev_record:
        age_sec = int(time.time() - int(prev_record.get("posted_at", 0)))
        if age_sec <= AUTOPOST_EDIT_WINDOW_SECONDS:
            print(f"♻️ Редактирование в окне {AUTOPOST_EDIT_WINDOW_SECONDS}s: удаляем старые посты и репостим")
            err_x, err_fc = _delete_previous_social_posts(prev_record)
            if err_x:
                print(f"⚠️ Ошибки удаления X: {err_x}")
            if err_fc:
                print(f"⚠️ Ошибки удаления Farcaster: {err_fc}")
        else:
            print(f"ℹ️ Редактирование позже окна {AUTOPOST_EDIT_WINDOW_SECONDS}s — удаление пропущено")
    
    # Автоматически публикуем
    result = await auto_post_to_socials(user_text, photo_file_id, context.bot)

    # Сохраняем связь channel message -> social ids для последующего delete/repost
    if result.get("ok"):
        x_ids, fc_hashes = _extract_social_ids(result)
        state[state_key] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "posted_at": int(time.time()),
            "x_ids": x_ids,
            "fc_hashes": fc_hashes,
        }
        _save_autopost_state(state)
    
    # Формируем сообщение о результате
    is_thread = result.get("thread", False)
    
    if is_thread:
        # Результат треда
        posts_count = result.get("posts_count", 0)
        x_info = result.get("x", {})
        fc_info = result.get("farcaster", {})
        
        lines = [f"🧵 Тред из {posts_count} постов:"]
        
        x_success = x_info.get("success", 0)
        x_total = x_info.get("total", 0)
        if x_success > 0:
            lines.append(f"✅ X: {x_success}/{x_total} постов опубликовано")
        else:
            lines.append(f"❌ X: не удалось опубликовать")
        
        fc_success = fc_info.get("success", 0)
        fc_total = fc_info.get("total", 0)
        if fc_success > 0:
            lines.append(f"✅ Farcaster: {fc_success}/{fc_total} постов опубликовано")
        else:
            lines.append(f"❌ Farcaster: не удалось опубликовать")
    else:
        # Обычный пост
        result_x = result.get("x", {})
        result_fc = result.get("farcaster", {})
        
        lines = []
        if result_x.get("ok"):
            lines.append("✅ X: опубликовано")
        else:
            err = result_x.get("error") or result_x.get("body") or result_x
            lines.append(f"❌ X: {err}")
        
        if result_fc.get("ok"):
            lines.append("✅ Farcaster: опубликовано")
        else:
            err = result_fc.get("error") or result_fc.get("body") or result_fc
            lines.append(f"❌ Farcaster: {err}")
    
    # Формируем уведомление с ссылками
    notify_lines = []
    if is_thread:
        x_replies = (result.get("x") or {}).get("replies") or []
        fc_replies = (result.get("farcaster") or {}).get("replies") or []
        first_x_id = next((r.get("id") for r in x_replies if r.get("ok") and r.get("id")), None)
        first_fc_hash = next((r.get("hash") for r in fc_replies if r.get("ok") and r.get("hash")), None)
        x_link = x_tweet_url(first_x_id)
        fc_link = farcaster_cast_url(first_fc_hash)

        notify_lines.append(f"🧵 Автопост треда из канала {chat_id}")
        notify_lines.extend(lines)
        if x_link:
            notify_lines.append(f"✅ [Опубликовано в X]({x_link})")
        if fc_link:
            notify_lines.append(f"✅ [Опубликовано в Farcaster]({fc_link})")
    else:
        result_x = result.get("x", {})
        result_fc = result.get("farcaster", {})
        x_link = x_tweet_url(result_x.get("id")) if result_x.get("ok") else None
        fc_link = farcaster_cast_url(result_fc.get("hash")) if result_fc.get("ok") else None

        notify_lines.append(f"📤 Автопост из канала {chat_id}")
        notify_lines.extend(lines)
        if x_link:
            notify_lines.append(f"✅ [Опубликовано в X]({x_link})")
        if fc_link:
            notify_lines.append(f"✅ [Опубликовано в Farcaster]({fc_link})")

    result_msg = "\n".join(notify_lines)
    
    # Логируем в консоль
    if result.get("ok"):
        print(f"✅ Автопост из канала {chat_id}: опубликовано в X/Farcaster")
    else:
        print(f"❌ Автопост из канала {chat_id}: ошибка - {result.get('error', result)}")
    
    # Отправляем уведомление в Telegram только после успешного автопостинга
    if AUTOPOST_NOTIFY_CHAT_ID and result.get("ok"):
        try:
            await context.bot.send_message(
                chat_id=AUTOPOST_NOTIFY_CHAT_ID,
                text=result_msg,
                parse_mode="Markdown",
                disable_web_page_preview=True,
                disable_notification=False,
            )
        except Exception as e:
            print(f"⚠️ Не удалось отправить уведомление: {e}")


async def debug_any_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Диагностический handler: логирует любой входящий message/channel_post.
    Нужен, чтобы понять, доходят ли апдейты из канала до бота.
    """
    try:
        if update.channel_post or update.edited_channel_post:
            m = update.channel_post or update.edited_channel_post
            chat_id = getattr(getattr(m, "chat", None), "id", None)
            kind = "channel_post" if update.channel_post else "edited_channel_post"
            text = (m.text or m.caption or "").replace("\n", " ")[:80]
            print(f"🧪 DEBUG update={kind} chat_id={chat_id} text='{text}'")
        elif update.message or update.edited_message:
            m = update.message or update.edited_message
            chat_id = getattr(getattr(m, "chat", None), "id", None)
            chat_type = getattr(getattr(m, "chat", None), "type", None)
            kind = "message" if update.message else "edited_message"
            text = (m.text or m.caption or "").replace("\n", " ")[:80]
            print(f"🧪 DEBUG update={kind} chat_id={chat_id} chat_type={chat_type} text='{text}'")
    except Exception as e:
        print(f"🧪 DEBUG error: {e}")


if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    # Важно: посты из каналов должны обрабатываться ТОЛЬКО автопостингом.
    # Поэтому обычный обработчик текста ограничиваем чат-типами (private/group/supergroup),
    # чтобы он НИКОГДА не перехватывал channel_post.
    non_channel_chats = filters.ChatType.PRIVATE | filters.ChatType.GROUPS | filters.ChatType.SUPERGROUP
    app.add_handler(MessageHandler(non_channel_chats & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(button_handler))
    # DEBUG: логируем любые апдейты отдельной группой, не мешает основной логике
    app.add_handler(MessageHandler(filters.ALL, debug_any_update), group=1)
    
    # Обработчик для постов из каналов (автопостинг)
    if AUTOPOST_ENABLED:
        from telegram.ext import filters as tg_filters
        app.add_handler(MessageHandler(tg_filters.UpdateType.CHANNEL_POSTS, handle_channel_post))
    
    oauth1_count = sum(1 for v in [X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET] if v)
    imgbb_status = "включена" if IMGBB_API_KEY else "отключена (добавь IMGBB_API_KEY в .env)"
    autopost_status = f"включен (каналы: {AUTOPOST_CHANNEL_IDS if AUTOPOST_CHANNEL_IDS else 'все'})" if AUTOPOST_ENABLED else "отключен"
    if USE_GROK_FOR_X and client_grok:
        ai_status = f"Grok ({GROK_MODEL}) для X, {'Gemini' if MODEL_NAME else 'только Grok'}"
    elif MODEL_NAME:
        ai_status = f"Gemini ({MODEL_NAME})"
    else:
        ai_status = "не настроен"
    print(f"🚀 Бот @Don_Inv запущен")
    print(f"🤖 AI для перевода: {ai_status}")
    print(f"📷 X media (OAuth1): {oauth1_count}/4 ключей — фото в посты X {'включены' if oauth1_count == 4 else 'отключены (добавь X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET в .env)'}")
    print(f"🖼️ Farcaster images (Imgbb): {imgbb_status}")
    print(f"🤖 Автопостинг из каналов: {autopost_status}")
    app.run_polling()