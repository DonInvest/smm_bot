"""Claude как X-редактор: RU пост из Telegram -> EN твит или тред в голосе Дона.

Один запрос на весь пост (Grok переводил тред кусками). Структурированный JSON,
локальная проверка лимитов и запретов, при нарушениях один круг исправления.
Если ключа нет или Claude не ответил, вызывающий код уходит в старый путь (Grok).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

_SCHEMA = {
    "type": "object",
    "properties": {
        "parts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "One element = single tweet; 2-6 elements = thread, in order.",
        },
        "note": {
            "type": "string",
            "description": "Empty, or one line in Russian if the angle is weak/off-core/risky.",
        },
    },
    "required": ["parts", "note"],
    "additionalProperties": False,
}

# Слова, которые X деранжирует (feedback_banned_words.md проекта) и маркеры слопа.
_BANNED = [
    r"\bcrypto\w*", r"\bprediction markets?\b", r"\bpolymarket\b", r"\bbet(s|ting)?\b", r"\bgambl\w*",
    r"\bDYOR\b", r"\bgame[- ]changer\b", r"\blet that sink in\b", r"\bhere'?s why\b", r"\bbuckle up\b",
    r"\bwagmi\b", r"\bLFG\b", r"\bnot financial advice\b", r"\bNFA\b",
]
_CTA = [r"\b(retweet|repost|share this|send this|copy the link|bookmark this|follow me|follow for)\b",
        r"\b(comment below|drop a comment|reply below|let me know in the comments|click the link)\b"]
_THREAD_NUM = re.compile(r"^\s*\(?\d{1,2}\s*/\s*\d{1,2}\)?")


@dataclass
class WriterResult:
    parts: List[str]
    note: str = ""
    issues: List[str] = field(default_factory=list)
    model: str = ""
    usage: dict = field(default_factory=dict)


def load_system_prompt(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def check_parts(parts: List[str], source: str, x_len: Callable[[str], int], max_chars: int = 280) -> List[str]:
    """Локальная проверка. Возвращает список нарушений (пустой = ок)."""
    issues: List[str] = []
    if not parts or not any(p.strip() for p in parts):
        return ["empty output"]
    if len(parts) > 6:
        issues.append(f"thread has {len(parts)} parts, max is 6")
    joined = "\n".join(parts)
    for i, p in enumerate(parts, 1):
        n = x_len(p)
        if n > max_chars:
            issues.append(f"part {i} is {n} chars by X count, max {max_chars}")
        if _THREAD_NUM.match(p):
            issues.append(f"part {i} starts with thread numbering")
        if len(re.findall(r"(?<!\w)\$[A-Za-z][A-Za-z0-9]{1,14}\b", p)) > 1:
            issues.append(f"part {i} has more than one $TICKER")
    if "—" in joined or "–" in joined:
        issues.append("contains em/en dash")
    if "🧵" in joined:
        issues.append("contains 🧵")
    for pat in _BANNED + _CTA:
        m = re.search(pat, joined, re.I)
        if m:
            issues.append(f"banned phrase: '{m.group(0)}'")
    for url in re.findall(r"https?://\S+", source or ""):
        url = url.rstrip(").,!?»\"'")
        if url not in joined:
            issues.append(f"missing source URL {url}")
    return issues


class ClaudeWriter:
    def __init__(self, api_key: str, system_prompt: str, model: str = DEFAULT_MODEL, effort: str = "high",
                 client=None, log: Callable[[str], None] = print):
        if client is None:
            import anthropic  # импорт здесь: без ключа бот работает и без пакета

            client = anthropic.Anthropic(api_key=api_key, timeout=120.0, max_retries=2)
        self._client = client
        self._system = system_prompt
        self.model = model
        self._effort = effort
        self._log = log

    @classmethod
    def from_env(cls, base_dir: str, log: Callable[[str], None] = print) -> Optional["ClaudeWriter"]:
        key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
        mode = (os.getenv("X_WRITER") or "claude").strip().lower()
        if not key or mode != "claude":
            return None
        path = os.path.join(base_dir, "prompts", "x_system.md")
        return cls(
            api_key=key,
            system_prompt=load_system_prompt(path),
            model=(os.getenv("CLAUDE_MODEL") or DEFAULT_MODEL).strip(),
            effort=(os.getenv("CLAUDE_EFFORT") or "high").strip(),
            log=log,
        )

    def _request(self, messages: list):
        return self._client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            system=[{"type": "text", "text": self._system, "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": self._effort, "format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=messages,
        )

    def _parse(self, response) -> Optional[dict]:
        if response.stop_reason == "refusal":
            self._log(f"⚠️ Claude refusal: {getattr(response, 'stop_details', None)}")
            return None
        if response.stop_reason == "max_tokens":
            self._log("⚠️ Claude: max_tokens, ответ обрезан")
            return None
        text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), "")
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            self._log(f"⚠️ Claude: невалидный JSON: {text[:200]}")
            return None
        parts = [str(p).strip() for p in (data.get("parts") or []) if str(p).strip()]
        return {"parts": parts, "note": str(data.get("note") or "").strip()}

    def write(self, source: str, hints: str, x_len: Callable[[str], int]) -> Optional[WriterResult]:
        user = "Telegram post (Russian):\n<source>\n" + source.strip() + "\n</source>"
        if hints:
            user += "\n\nProject handles and tickers (use when the project is mentioned):\n" + hints
        messages = [{"role": "user", "content": user}]
        response = self._request(messages)
        data = self._parse(response)
        if not data:
            return None
        issues = check_parts(data["parts"], source, x_len)
        for _round in range(2):  # до двух кругов правки; часть длиннее 280 иначе обрежется по слову
            if not issues:
                break
            self._log(f"⚠️ Claude X: нарушения {issues}, прошу исправить")
            counts = ", ".join(f"part {i}: {x_len(p)}" for i, p in enumerate(data["parts"], 1))
            messages += [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": "Fix these problems and return the full corrected JSON: "
                 + "; ".join(issues) + f". Current X lengths: {counts}. Aim for 260 or less per part; "
                 "split into one more part rather than cutting facts."},
            ]
            response2 = self._request(messages)
            data2 = self._parse(response2)
            if not data2:
                break
            issues2 = check_parts(data2["parts"], source, x_len)
            if len(issues2) > len(issues):
                break
            data, issues, response = data2, issues2, response2
        usage = getattr(response, "usage", None)
        return WriterResult(
            parts=data["parts"], note=data["note"], issues=issues, model=getattr(response, "model", self.model),
            usage={k: getattr(usage, k, None) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens")}
            if usage else {},
        )
