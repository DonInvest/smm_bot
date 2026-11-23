"""Регрессии подготовки X-текста. Запуск: python -m unittest discover -s tests -v"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402


class XTextTests(unittest.TestCase):
    def test_trailing_url_kept_when_text_fits(self):
        t = "nvidia hired headhunters\n\nhttps://x.com/cnn/status/2124867549582033091"
        self.assertEqual(main.prepare_x_post_text(t), t)

    def test_underscore_in_url_kept(self):
        self.assertEqual(main.normalize_social_text("_hi_ https://x.com/don_inv/status/1"),
                         "hi https://x.com/don_inv/status/1")

    def test_long_text_still_clamped(self):
        out = main.prepare_x_post_text("word " * 80 + "\nhttps://x.com/a/status/1")
        self.assertLessEqual(main.x_effective_length(out), 280)


if __name__ == "__main__":
    unittest.main()
