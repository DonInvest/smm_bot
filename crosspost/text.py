"""Подготовка текста под каждую площадку. Без ИИ: берём готовый EN-текст из X-пайплайна."""
from __future__ import annotations

import re
import unicodedata
from typing import List, Optional, Tuple

THREADS_MAX_CHARS = 500
THREADS_MAX_LINKS = 5

_URL_RE = re.compile(r"https?://\S+")
# @handle из X: на других площадках он отметит чужой аккаунт, поэтому снимаем "@".
_MENTION_RE = re.compile(r"(?<![\w./@])@([A-Za-z0-9_]{1,15})\b")
_BINANCE_HOST_RE = re.compile(r"^https?://([a-z0-9-]+\.)*binance\.(com|info|us)(/|$)", re.I)


def strip_x_mentions(text: str) -> str:
    return _MENTION_RE.sub(r"\1", text or "")


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def threads_len(text: str) -> int:
    """Длина по правилам Threads: эмодзи считаются байтами UTF-8, остальное символами."""
    n = 0
    for ch in text:
        cp = ord(ch)
        is_emoji = cp > 0xFFFF or unicodedata.category(ch) == "So" or 0xFE00 <= cp <= 0xFE0F or cp == 0x200D
        n += len(ch.encode("utf-8")) if is_emoji else 1
    return n


def _fit_prefix(text: str, limit: int) -> int:
    """Сколько символов с начала влезает в limit по счёту Threads."""
    n = 0
    for i, ch in enumerate(text):
        n += threads_len(ch)
        if n > limit:
            return i
    return len(text)


def _hard_split(text: str, limit: int) -> List[str]:
    """Режет текст на куски <= limit (счёт Threads) по словам. Страховка на случай длинных частей."""
    out: List[str] = []
    rest = text.strip()
    while threads_len(rest) > limit:
        fit = _fit_prefix(rest, limit)
        cut = rest.rfind(" ", 0, fit)
        if cut < fit // 2:
            cut = fit
        out.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        out.append(rest)
    return out


def build_threads_parts(x_parts: List[str]) -> List[str]:
    """Части треда X -> цепочка постов Threads (каждая <= 500 символов, ссылок <= 5 на пост)."""
    result: List[str] = []
    for part in x_parts:
        p = _tidy(strip_x_mentions(part))
        if not p:
            continue
        urls = _URL_RE.findall(p)
        if len(urls) > THREADS_MAX_LINKS:
            for extra in urls[THREADS_MAX_LINKS:]:
                p = p.replace(extra, "", 1)
            p = _tidy(p)
        result.extend(_hard_split(p, THREADS_MAX_CHARS))
    return result


_THREAD_NUM_RE = re.compile(r"^\s*\(?\d{1,2}\s*/\s*\d{1,2}\)?\s*", re.M)
_THREAD_EMOJI_RE = re.compile(r"\s*🧵\s*$")


def build_long_text(x_parts: List[str]) -> str:
    """Склеивает тред в один пост (Square, DeBank): убирает нумерацию 1/5 и 🧵."""
    chunks = []
    for part in x_parts:
        p = _THREAD_NUM_RE.sub("", part or "")
        p = _THREAD_EMOJI_RE.sub("", p)
        p = _tidy(strip_x_mentions(p))
        if p:
            chunks.append(p)
    return "\n\n".join(chunks)


def strip_non_binance_links(text: str) -> str:
    """Square разрешает только ссылки на Binance; внешние убираем, чтобы не словить ограничение."""

    def repl(m: "re.Match[str]") -> str:
        url = m.group(0)
        return url if _BINANCE_HOST_RE.match(url) else ""

    out = _URL_RE.sub(repl, text)
    out = re.sub(r"\(\s*\)", "", out)
    out = re.sub(r"[ \t]*(→|👉|🔗)[ \t]*(?=\n|$)", "", out)
    return _tidy(out)


def build_square_payload(x_parts: List[str], strip_links: bool, max_chars: int) -> Tuple[str, Optional[str]]:
    """Возвращает (body, title). title != None -> публикуем статьёй (contentType=2)."""
    body = build_long_text(x_parts)
    if strip_links:
        body = strip_non_binance_links(body)
    if len(body) <= max_chars:
        return body, None
    first, _, rest = body.partition("\n")
    title = first.strip()[:100] or "Update"
    return (rest.strip() or body), title


# Метки в начале поста, отключающие площадку кросспоста.
CROSSPOST_TAGS = {
    "no_th": "threads", "no_threads": "threads",
    "no_sq": "square", "no_square": "square", "no_binance": "square",
    "no_db": "debank", "no_debank": "debank",
}


def pop_crosspost_tags(text: str) -> Tuple[str, set]:
    """Убирает из ведущих строк-меток #no_th/#no_sq/#no_db. Остальные метки (#no_x и т.п.) не трогает."""
    if not text:
        return text, set()
    lines = text.split("\n")
    excluded: set = set()
    out: List[str] = []
    in_header = True
    for line in lines:
        s = line.strip()
        if in_header and s.startswith("#"):
            tag = s.split()[0].lstrip("#").lower()
            if tag in CROSSPOST_TAGS:
                excluded.add(CROSSPOST_TAGS[tag])
                continue
        elif in_header and s:
            in_header = False
        out.append(line)
    return "\n".join(out), excluded
