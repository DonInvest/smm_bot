import base64
import json
import os
import requests
import asyncio
import re
import tempfile
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

GEMINI_KEY = clean_token("GEMINI_API_KEY")
TELEGRAM_TOKEN = clean_token("TELEGRAM_BOT_TOKEN")

# OAuth 2.0 X: access + refresh (refresh нужен для автообновления при 401)
X_USER_ACCESS_TOKEN = clean_token("X_USER_ACCESS_TOKEN")
X_REFRESH_TOKEN = clean_token("X_REFRESH_TOKEN")
X_CLIENT_ID = clean_token("X_CLIENT_ID")
X_CLIENT_SECRET = clean_token("X_CLIENT_SECRET")

# Файл с актуальными токенами после refresh (чтобы не терять после перезапуска)
_X_TOKENS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".x_tokens.json")
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
AUTOPOST_CHANNEL_IDS = [
    int(cid.strip()) for cid in os.getenv("AUTOPOST_CHANNEL_IDS", "").split(",") if cid.strip().isdigit()
]
# ID чата для уведомлений об автопостинге (ваш личный чат с ботом или канал для логов)
AUTOPOST_NOTIFY_CHAT_ID = None
_notify_chat_id_str = os.getenv("AUTOPOST_NOTIFY_CHAT_ID", "").strip()
if _notify_chat_id_str and _notify_chat_id_str.isdigit():
    AUTOPOST_NOTIFY_CHAT_ID = int(_notify_chat_id_str)

# Лимиты
X_MAX_CHARS = 280
FARCASTER_MAX_BYTES = 320  # Farcaster лимит измеряется в байтах UTF-8
X_TCO_URL_LEN = 23  # приближение: X считает каждый URL как фиксированную длину

# Gemini: новый SDK (google-genai) + api_version v1 — квота у тебя на gemini-2.5-flash
client_ai = genai.Client(
    api_key=GEMINI_KEY,
    http_options=genai_types.HttpOptions(api_version="v1"),
)
MODEL_NAME = "gemini-2.5-flash"


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


def post_to_x(text: str, media_ids: Optional[List[str]] = None) -> dict:
    """
    Публикуем пост в X API v2.
    Всегда используем OAuth2 (Bearer) для твита — так не будет 401 при истёкших OAuth1.
    Фото загружаются отдельно через upload_media_to_x (OAuth1), сюда передаются уже media_ids.
    card_uri: "tombstone://card" убирает предпросмотр ссылок (link preview cards).
    """
    url = "https://api.x.com/2/tweets"
    clean = clamp_to_limits(text)
    payload: dict = {"text": clean}
    if media_ids:
        payload["media"] = {"media_ids": media_ids}
    # Убираем предпросмотр ссылок - добавляем card_uri для отключения карточек
    if _URL_RE.search(clean):
        payload["card_uri"] = "tombstone://card"

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


def post_to_farcaster(text: str, embeds: Optional[List[str]] = None):
    """
    Публикуем пост в Farcaster через Neynar API.
    embeds: список URL для эмбедов (только изображения, не ссылки - чтобы не было предпросмотров).
    Ссылки остаются в тексте, но без предпросмотра карточек.
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
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=20)
        data = resp.json()
        if resp.status_code == 200 and data.get("success"):
            cast_hash = (data.get("cast") or {}).get("hash")
            return {"ok": True, "hash": cast_hash}
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
{user_text}
""".strip()
    
    try:
        response = client_ai.models.generate_content(model=MODEL_NAME, contents=prompt_translate)
        raw_translated = (response.text or "").strip()
        translated = normalize_social_text(raw_translated)
        translated = _avoid_cutting_url(translated)

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

        # Если не влезает — делаем 3 варианта сокращения, сохраняя инструкционный формат
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
        await msg.edit_text(f"❌ Ошибка Gemini: {e}")

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


async def auto_post_to_socials(text: str, photo_file_id: Optional[str] = None, bot=None) -> dict:
    """
    Автоматически переводит текст и публикует в X + Farcaster без кнопок.
    Используется для автопостинга из каналов.
    """
    if not text:
        return {"ok": False, "error": "No text to post"}
    
    # Переводим текст с оптимизацией для продвижения
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
            return {"ok": False, "error": "Translation failed"}
        
        # Если не влезает — сокращаем автоматически с оптимизацией
        if not fits_limits(translated):
            prompt_shorten = f"""
You are an editor and social media growth expert. The post is instructional (list, links, tips). Shorten it to fit platform limits while keeping it useful AND adding engagement optimization.

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
        
        final_text = clamp_to_limits(translated)
        
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
        return
    
    # channel_post может быть в update.channel_post или update.edited_channel_post
    channel_post = update.channel_post or update.edited_channel_post
    if not channel_post:
        return
    
    chat_id = channel_post.chat.id
    # Если указаны конкретные каналы — проверяем
    if AUTOPOST_CHANNEL_IDS and chat_id not in AUTOPOST_CHANNEL_IDS:
        return
    
    # Игнорируем посты от самого бота (чтобы избежать циклов)
    if channel_post.from_user and channel_post.from_user.is_bot:
        return
    
    user_text, photos = extract_text_and_photos(channel_post)
    if not user_text:
        return
    
    photo_file_id = photos[-1].file_id if photos else None
    
    # Автоматически публикуем
    result = await auto_post_to_socials(user_text, photo_file_id, context.bot)
    
    # Формируем сообщение о результате
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
    
    result_msg = "\n".join(lines)
    
    # Логируем в консоль
    if result.get("ok"):
        print(f"✅ Автопост из канала {chat_id}: опубликовано в X/Farcaster")
    else:
        print(f"❌ Автопост из канала {chat_id}: ошибка - {result.get('error', result)}")
    
    # Отправляем уведомление в Telegram, если указан chat_id
    if AUTOPOST_NOTIFY_CHAT_ID:
        try:
            await context.bot.send_message(
                chat_id=AUTOPOST_NOTIFY_CHAT_ID,
                text=f"📤 Автопост из канала:\n\n{result_msg}",
                disable_notification=False,
            )
        except Exception as e:
            print(f"⚠️ Не удалось отправить уведомление: {e}")


if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    # Обработчик для постов из каналов (автопостинг)
    if AUTOPOST_ENABLED:
        from telegram.ext import filters as tg_filters
        app.add_handler(MessageHandler(tg_filters.UpdateType.CHANNEL_POSTS, handle_channel_post))
    
    oauth1_count = sum(1 for v in [X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET] if v)
    imgbb_status = "включена" if IMGBB_API_KEY else "отключена (добавь IMGBB_API_KEY в .env)"
    autopost_status = f"включен (каналы: {AUTOPOST_CHANNEL_IDS if AUTOPOST_CHANNEL_IDS else 'все'})" if AUTOPOST_ENABLED else "отключен"
    print(f"🚀 Бот @Don_Inv запущен на {MODEL_NAME}")
    print(f"📷 X media (OAuth1): {oauth1_count}/4 ключей — фото в посты X {'включены' if oauth1_count == 4 else 'отключены (добавь X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET в .env)'}")
    print(f"🖼️ Farcaster images (Imgbb): {imgbb_status}")
    print(f"🤖 Автопостинг из каналов: {autopost_status}")
    app.run_polling()