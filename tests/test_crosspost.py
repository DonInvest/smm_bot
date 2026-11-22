"""Тесты кросспоста. Запуск: python -m unittest discover -s tests -v"""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crosspost import store as st  # noqa: E402
from crosspost.config import CrosspostConfig  # noqa: E402
from crosspost.errors import PlatformError  # noqa: E402
from crosspost.service import CrosspostService  # noqa: E402
from crosspost.square_api import SquareClient  # noqa: E402
from crosspost.text import (  # noqa: E402
    build_long_text, build_square_payload, build_threads_parts, pop_crosspost_tags,
    strip_non_binance_links, strip_x_mentions)


def make_cfg(tmp, **kw):
    base = dict(enabled=True, dry_run=False, threads_token="tok", square_key="key", imgbb_key="",
                delay_seconds=0, poll_seconds=3, max_attempts=3, square_strip_links=True,
                square_max_chars=2000, notify_chat_id=None, db_path=os.path.join(tmp, "db.sqlite3"),
                token_file=os.path.join(tmp, "tok.json"), platforms={"threads", "square"})
    base.update(kw)
    return CrosspostConfig(**base)


class TextTests(unittest.TestCase):
    def test_mentions_stripped_but_emails_and_urls_kept(self):
        t = "gm @Backpack and @solana, mail a@b.com https://x.com/@foo"
        self.assertEqual(strip_x_mentions(t), "gm Backpack and solana, mail a@b.com https://x.com/@foo")

    def test_threads_parts_respect_500_and_links(self):
        long = "word " * 200
        parts = build_threads_parts(["short @Foo", long])
        self.assertEqual(parts[0], "short Foo")
        self.assertTrue(all(len(p) <= 500 for p in parts))
        many = " ".join(f"https://a{i}.io" for i in range(7))
        self.assertEqual(build_threads_parts([many])[0].count("https://"), 5)

    def test_long_text_removes_thread_numbering(self):
        txt = build_long_text(["1/3 Hook here 🧵", "2/3 Second", "3/3 Third"])
        self.assertEqual(txt, "Hook here\n\nSecond\n\nThird")

    def test_square_link_policy(self):
        t = "Read 👉 https://example.com/x\nand https://www.binance.com/en/square ok"
        out = strip_non_binance_links(t)
        self.assertNotIn("example.com", out)
        self.assertIn("https://www.binance.com/en/square", out)

    def test_square_article_when_long(self):
        body, title = build_square_payload(["Title line\n" + "x" * 3000], True, 2000)
        self.assertEqual(title, "Title line")
        self.assertTrue(body.startswith("xxx"))
        body2, title2 = build_square_payload(["short"], True, 2000)
        self.assertIsNone(title2)

    def test_pop_tags(self):
        text, ex = pop_crosspost_tags("#no_sq\n#no_x\nHello\n#no_th stays")
        self.assertEqual(ex, {"square"})
        self.assertEqual(text, "#no_x\nHello\n#no_th stays")


class FakeThreads:
    def __init__(self, fail=None):
        self.fail = list(fail or [])
        self.published = []
        self.deleted = []

    def publish_chain(self, parts, image_url, done_ids, on_progress, sleep):
        if self.fail:
            raise self.fail.pop(0)
        ids = list(done_ids) + [f"t{len(self.published) + i}" for i in range(len(parts) - len(done_ids))]
        self.published.append(parts)
        on_progress(ids)
        return ids

    def permalink(self, mid):
        return f"https://threads.net/p/{mid}"

    def delete(self, mid):
        self.deleted.append(mid)


class FakeSquare:
    def __init__(self, fail=None):
        self.fail = list(fail or [])
        self.published = []

    def publish(self, text, title=None, image_paths=None, sleep=None):
        if self.fail:
            raise self.fail.pop(0)
        self.published.append((text, title))
        return "sq1", "https://binance.com/square/post/sq1"


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.logs = []

    def svc(self, threads=None, square=None, **kw):
        return CrosspostService(make_cfg(self.tmp, **kw), threads=threads or FakeThreads(),
                                square=square or FakeSquare(), sleep=lambda s: None, log=self.logs.append)

    def run_due(self, s):
        return asyncio.run(s.process_due(bot=None))

    def test_happy_path(self):
        s = self.svc()
        pid = s.enqueue("c:1", ["1/2 Hello @Foo", "2/2 Bye"], None, ["threads", "square"])
        self.assertEqual(self.run_due(s), 2)
        ds = {d.platform: d for d in s.store.deliveries(pid)}
        self.assertEqual(ds["threads"].status, st.DONE)
        self.assertEqual(ds["threads"].url, "https://threads.net/p/t0")
        self.assertEqual(ds["square"].status, st.DONE)
        self.assertEqual(s.square.published[0][0], "Hello Foo\n\nBye")

    def test_delay_respected(self):
        s = self.svc(delay_seconds=60)
        s.enqueue("c:1", ["Hi"], None, ["threads"])
        self.assertEqual(self.run_due(s), 0)

    def test_unknown_platform_filtered(self):
        s = self.svc(platforms={"threads"})
        pid = s.enqueue("c:1", ["Hi"], None, ["threads", "square"])
        self.assertEqual([d.platform for d in s.store.deliveries(pid)], ["threads"])

    def test_retryable_then_failed(self):
        errs = [PlatformError("boom", retryable=True) for _ in range(5)]
        s = self.svc(threads=FakeThreads(fail=errs), max_attempts=2)
        pid = s.enqueue("c:1", ["Hi"], None, ["threads"])
        self.run_due(s)
        d = s.store.deliveries(pid)[0]
        self.assertEqual(d.status, st.PENDING)
        s.store.update_delivery(d.id, next_at=0)
        self.run_due(s)
        self.assertEqual(s.store.deliveries(pid)[0].status, st.FAILED)

    def test_uncertain_not_retried_and_retry_button(self):
        s = self.svc(square=FakeSquare(fail=[PlatformError("timeout", uncertain=True)]))
        pid = s.enqueue("c:1", ["Hi"], None, ["square"])
        self.run_due(s)
        d = s.store.deliveries(pid)[0]
        self.assertEqual(d.status, st.UNCERTAIN)
        text, retry_ids = s.build_report(s.store.get_post(pid))
        self.assertEqual(retry_ids[0][0], d.id)
        self.assertTrue(s.retry(d.id))
        self.run_due(s)
        self.assertEqual(s.store.deliveries(pid)[0].status, st.DONE)

    def test_edit_before_send_cancels_and_requeues(self):
        s = self.svc(delay_seconds=60)
        old = s.enqueue("c:1", ["Old"], None, ["threads", "square"])
        notes = s.handle_edit("c:1", ["New"], None, ["threads", "square"])
        self.assertEqual(notes, [])
        self.assertTrue(all(d.status == st.CANCELLED for d in s.store.deliveries(old)))
        new = s.store.latest_post("c:1")
        self.assertEqual(new.x_parts, ["New"])
        self.assertEqual(len(s.store.deliveries(new.id)), 2)

    def test_edit_after_send(self):
        s = self.svc()
        old = s.enqueue("c:1", ["Old"], None, ["threads", "square"])
        self.run_due(s)
        notes = s.handle_edit("c:1", ["New"], None, ["threads", "square"])
        self.assertEqual(s.threads.deleted, ["t0"])
        new = s.store.latest_post("c:1")
        self.assertEqual([d.platform for d in s.store.deliveries(new.id)], ["threads"])
        self.assertTrue(any("Binance Square" in n for n in notes))
        self.assertTrue(s.store.get_post(old).superseded)

    def test_edit_threads_delete_fails_no_duplicate(self):
        th = FakeThreads()
        th.delete = mock.Mock(side_effect=PlatformError("no threads_delete permission"))
        s = self.svc(threads=th)
        s.enqueue("c:1", ["Old"], None, ["threads"])
        self.run_due(s)
        notes = s.handle_edit("c:1", ["New"], None, ["threads"])
        self.assertIsNone(s.store.latest_post("c:1"))
        self.assertIn("не удалён", notes[0])

    def test_restart_marks_sending_uncertain(self):
        s = self.svc()
        pid = s.enqueue("c:1", ["Hi"], None, ["threads"])
        s.store.claim_due(int(time.time()))
        self.assertEqual(s.store.recover_interrupted(), 1)
        self.assertEqual(s.store.deliveries(pid)[0].status, st.UNCERTAIN)

    def test_dry_run_publishes_nothing(self):
        th, sq = FakeThreads(), FakeSquare()
        s = self.svc(threads=th, square=sq, dry_run=True)
        pid = s.enqueue("c:1", ["Hi"], None, ["threads", "square"])
        self.run_due(s)
        self.assertEqual(th.published, [])
        self.assertEqual(sq.published, [])
        self.assertTrue(all(d.status == st.DONE for d in s.store.deliveries(pid)))

    def test_notify_without_bot_marks_notified(self):
        s = self.svc()
        pid = s.enqueue("c:1", ["Hi"], None, ["threads"])
        self.run_due(s)
        asyncio.run(s.notify_finished(None))
        self.assertTrue(s.store.get_post(pid).notified)


class SquareClientTests(unittest.TestCase):
    def _resp(self, status, json_body):
        r = mock.Mock()
        r.status_code = status
        r.json.return_value = json_body
        return r

    def test_504_on_publish_is_success_without_id(self):
        sess = mock.Mock()
        sess.post.return_value = self._resp(504, None)
        self.assertEqual(SquareClient("k", session=sess).publish("hi"), (None, None))

    def test_permanent_error_not_retryable(self):
        sess = mock.Mock()
        sess.post.return_value = self._resp(200, {"code": "20002", "message": "sensitive"})
        with self.assertRaises(PlatformError) as cm:
            SquareClient("k", session=sess).publish("hi")
        self.assertFalse(cm.exception.retryable)
        self.assertFalse(cm.exception.uncertain)

    def test_success_payload(self):
        sess = mock.Mock()
        sess.post.return_value = self._resp(200, {"code": "000000", "data": {"id": 5, "shareLink": "L"}})
        self.assertEqual(SquareClient("k", session=sess).publish("hi"), ("5", "L"))
        body = sess.post.call_args.kwargs["json"]
        self.assertEqual(body, {"bodyTextOnly": "hi", "contentType": 1})


if __name__ == "__main__":
    unittest.main()
