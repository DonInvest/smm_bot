"""Тесты Claude-писателя без сети. Запуск: python -m unittest discover -s tests -v"""
import json
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crosspost.text import build_threads_parts, threads_len  # noqa: E402
from writer.claude_writer import ClaudeWriter, check_parts  # noqa: E402

xlen = len


def resp(payload, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, model="claude-opus-5-5",
                           content=[SimpleNamespace(type="text", text=json.dumps(payload))],
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0))


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


class CheckTests(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(check_parts(["$ARB unlock Friday, 92M tokens"], "src", xlen), [])

    def test_violations(self):
        issues = check_parts(["1/3 crypto is back — retweet this", "x" * 300], "see https://a.io/x", xlen)
        text = " ".join(issues)
        for needle in ("numbering", "crypto", "dash", "retweet", "part 2", "missing source URL"):
            self.assertIn(needle, text)

    def test_between_not_flagged_as_bet(self):
        self.assertEqual(check_parts(["Spread between CEX and DEX is 4%"], "", xlen), [])


class WriterTests(unittest.TestCase):
    def test_single_call_when_clean(self):
        c = FakeClient([resp({"parts": ["$ARB: 92M unlock Friday"], "note": ""})])
        w = ClaudeWriter("k", "SYS", client=c, log=lambda m: None)
        r = w.write("разлок ARB", "", xlen)
        self.assertEqual(r.parts, ["$ARB: 92M unlock Friday"])
        self.assertEqual(len(c.calls), 1)
        call = c.calls[0]
        self.assertEqual(call["fallbacks"], "default")
        self.assertEqual(call["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("temperature", call)

    def test_repair_round(self):
        c = FakeClient([resp({"parts": ["crypto pump — LFG"], "note": ""}),
                        resp({"parts": ["BTC pumped 6% in an hour"], "note": "слабый угол"})])
        w = ClaudeWriter("k", "SYS", client=c, log=lambda m: None)
        r = w.write("биток +6%", "", xlen)
        self.assertEqual(r.parts, ["BTC pumped 6% in an hour"])
        self.assertEqual(r.note, "слабый угол")
        self.assertEqual(len(c.calls), 2)
        self.assertEqual(c.calls[1]["messages"][1]["role"], "assistant")

    def test_refusal_returns_none(self):
        c = FakeClient([resp({}, stop="refusal")])
        self.assertIsNone(ClaudeWriter("k", "SYS", client=c, log=lambda m: None).write("x", "", xlen))

    def test_system_prompt_file_loads(self):
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "prompts", "x_system.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Анти-слоп", text)


class ThreadsLenTests(unittest.TestCase):
    def test_emoji_counts_as_bytes(self):
        self.assertEqual(threads_len("ab"), 2)
        self.assertEqual(threads_len("🚀"), 4)
        self.assertEqual(threads_len("привет"), 6)

    def test_split_respects_emoji_len(self):
        parts = build_threads_parts(["🚀" * 200])
        self.assertTrue(all(threads_len(p) <= 500 for p in parts))
        self.assertGreater(len(parts), 1)


if __name__ == "__main__":
    unittest.main()
