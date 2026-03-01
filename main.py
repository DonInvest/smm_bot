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

load_dotenv()

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

# Лимиты
X_MAX_CHARS = 280
FARCASTER_MAX_BYTES = 320  # Farcaster лимит измеряется в байтах UTF-8
X_TCO_URL_LEN = 23  # приближение: X считает каждый URL как фиксированную длину

# Инициализация Gemini (Developer API / v1, под твой лимит Gemini 2.5 Flash)
client_ai = genai.Client(
    api_key=GEMINI_KEY,
    http_options=genai_types.HttpOptions(api_version="v1"),
)
# Используем модель с квотой из скриншота
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


def upload_media_to_x(photo_path: str) -> dict:
    """
    Загружаем фото в X через v1.1 media/upload (OAuth 1.0a).
    """
    if not (X_API_KEY and X_API_SECRET and X_ACCESS_TOKEN and X_ACCESS_TOKEN_SECRET):
        return {"ok": False, "error": "Missing X OAuth1 keys for media upload"}

    url = "https://upload.twitter.com/1.1/media/upload.json"
    auth = OAuth1(X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET)
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
    - Если есть OAuth1 ключи, используем OAuth1 (нужно для media upload)
    - Иначе используем OAuth2 user access token
    """
    url = "https://api.x.com/2/tweets"
    clean = clamp_to_limits(text)
    payload: dict = {"text": clean}
    if media_ids:
        payload["media"] = {"media_ids": media_ids}

    try:
        if X_API_KEY and X_API_SECRET and X_ACCESS_TOKEN and X_ACCESS_TOKEN_SECRET:
            auth = OAuth1(X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET)
            resp = requests.post(url, json=payload, auth=auth, timeout=20)
        else:
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


def post_to_farcaster(text: str):
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
You are a professional translator for instructional and list-style social posts.

Translate the user's post to English. CRITICAL rules:
- Preserve ALL URLs exactly: they appear as "link_text (https://...)". Keep every URL in the same form "translated_label (same_url)".
- Preserve structure: title, bullet lists (• or -), line breaks, paragraphs, numbered items. The post should look like the original, just in English.
- Keep **bold**, _italic_, `code` markers if present — they mark emphasis/structure.
- Do NOT add new facts, hype, or emojis. Minimal editing only.
- Hashtags: translate meaning if needed (e.g. #база_знаний → #knowledge_base) or keep.

Output ONLY the translated text, ready to be posted. Same formatting and line breaks as the original.

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
You are an editor. The post is instructional (list, links, tips). Shorten it to fit platform limits while keeping it useful.

Create 3 shortening variants. For EACH option:
- Preserve structure: keep bullet/list format and line breaks where possible.
- Keep as many "name (url)" links as fit; do NOT drop URLs or replace with "link" — keep real URLs.
- No markdown symbols in output (no ** or _ or `) — target is X/Farcaster plain text.
- Must fit: X effective length <= {X_MAX_CHARS} (each URL counts as {X_TCO_URL_LEN} chars), Farcaster <= {FARCASTER_MAX_BYTES} bytes UTF-8.

Options can differ by: how many list items included, short intro vs full intro, which links kept.

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

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    idx = parts[1] if len(parts) > 1 else "0"

    text_to_post = context.user_data.get(f"post_{idx}")
    photo_file_id = context.user_data.get("last_photo_file_id")

    await query.edit_message_text(text="📤 Отправка в X + Farcaster...")

    media_ids: Optional[List[str]] = None
    skipped_photo_reason = None

    if photo_file_id:
        if X_API_KEY and X_API_SECRET and X_ACCESS_TOKEN and X_ACCESS_TOKEN_SECRET:
            tmp_path = None
            try:
                tg_file = await context.bot.get_file(photo_file_id)
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                    tmp_path = tmp.name
                await tg_file.download_to_drive(custom_path=tmp_path)
                up = upload_media_to_x(tmp_path)
                if up.get("ok"):
                    media_ids = [up["media_id"]]
                else:
                    skipped_photo_reason = f"media upload failed: {up}"
            except Exception as e:
                skipped_photo_reason = f"telegram download/upload error: {e}"
            finally:
                if tmp_path:
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
        else:
            skipped_photo_reason = "no X OAuth1 keys for media upload (X_API_KEY/SECRET + X_ACCESS_TOKEN/SECRET)"

    result_x = post_to_x(text_to_post, media_ids=media_ids)
    result_fc = post_to_farcaster(text_to_post)

    lines = []
    if result_x.get("ok"):
        lines.append("✅ X: posted")
    else:
        lines.append(f"❌ X: {result_x}")

    if result_fc.get("ok"):
        lines.append("✅ Farcaster: posted")
    else:
        lines.append(f"❌ Farcaster: {result_fc}")

    if skipped_photo_reason:
        lines.append(f"📷 Photo: skipped ({skipped_photo_reason})")

    lines.append("\nТекст:\n" + text_to_post)
    await query.edit_message_text(text="\n".join(lines))

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    # Принимаем любые сообщения без команд:
    # - текст
    # - пересланные посты
    # - фото/видео/документы с подписью
    app.add_handler(MessageHandler(~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(button_handler))
    print(f"🚀 Бот @Don_Inv запущен на {MODEL_NAME}")
    app.run_polling()