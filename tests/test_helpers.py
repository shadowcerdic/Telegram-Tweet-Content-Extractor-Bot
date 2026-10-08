import os
import tempfile
import unittest

import helpers
from helpers import (
    build_reply_text,
    chunk_media_items,
    chunk_text,
    extract_instagram_shortcodes,
    extract_tweet_ids,
    load_last_update_id,
    save_last_update_id,
)


class LinkExtraction(unittest.TestCase):
    def test_tweet_ids(self):
        text = (
            "see https://x.com/foo/status/123?s=20 and https://twitter.com/bar_1/status/456 "
            "and https://mobile.twitter.com/baz/status/789 and again https://x.com/foo/status/123"
        )
        self.assertEqual(extract_tweet_ids(text), ["123", "456", "789"])  # de-duplicated, ordered

    def test_tweet_ids_empty(self):
        self.assertEqual(extract_tweet_ids(""), [])
        self.assertEqual(extract_tweet_ids("https://example.com/status/1"), [])

    def test_instagram_shortcodes(self):
        text = (
            "https://www.instagram.com/p/ABC_def-1/ https://instagram.com/reel/XYZ123 "
            "https://instagram.com/reels/QQQ https://www.instagram.com/tv/TTT"
        )
        self.assertEqual(extract_instagram_shortcodes(text), ["ABC_def-1", "XYZ123", "QQQ", "TTT"])

    def test_instagram_username_in_path(self):
        # The Instagram share sheet produces links like this one.
        text = "https://www.instagram.com/some.user/reel/CODE123/?igsh=abc"
        self.assertEqual(extract_instagram_shortcodes(text), ["CODE123"])

    def test_instagram_profile_link_not_matched(self):
        self.assertEqual(extract_instagram_shortcodes("https://instagram.com/some.user/"), [])


class ChunkText(unittest.TestCase):
    def test_short(self):
        self.assertEqual(chunk_text("  hello "), ["hello"])
        self.assertEqual(chunk_text("   "), [])

    def test_never_exceeds_limit_and_no_empty_chunks(self):
        text = "\n".join(["line " * 30] * 50) + "\n\n\n" + "x" * 500
        chunks = chunk_text(text, max_length=200)
        self.assertTrue(chunks)
        self.assertTrue(all(0 < len(c) <= 200 for c in chunks))

    def test_content_preserved(self):
        words = [f"w{i}" for i in range(2000)]
        text = " ".join(words)  # a single very long line
        chunks = chunk_text(text, max_length=100)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        self.assertEqual(" ".join(chunks).split(), words)

    def test_breaks_at_whitespace_not_mid_word(self):
        chunks = chunk_text("alpha beta gamma delta", max_length=11)
        self.assertEqual(chunks, ["alpha beta", "gamma delta"])

    def test_unbreakable_run(self):
        chunks = chunk_text("x" * 250, max_length=100)
        self.assertEqual([len(c) for c in chunks], [100, 100, 50])


class ChunkMedia(unittest.TestCase):
    def test_no_single_item_album(self):
        # 10 + 1 used to crash: media groups need at least 2 items.
        for n in range(2, 80):
            chunks = chunk_media_items(list(range(n)))
            self.assertEqual(sum(len(c) for c in chunks), n)
            self.assertTrue(all(2 <= len(c) <= 10 for c in chunks), (n, chunks))

    def test_order_preserved(self):
        flat = [x for c in chunk_media_items(list(range(23))) for x in c]
        self.assertEqual(flat, list(range(23)))

    def test_single_and_empty(self):
        self.assertEqual(chunk_media_items([1]), [[1]])
        self.assertEqual(chunk_media_items([]), [])

    def test_eleven(self):
        self.assertEqual([len(c) for c in chunk_media_items(list(range(11)))], [6, 5])


class ReplyText(unittest.TestCase):
    def setUp(self):
        self._old = helpers.TRANSLATE_TO
        helpers.TRANSLATE_TO = "fa"

    def tearDown(self):
        helpers.TRANSLATE_TO = self._old

    author = {"name": "Jane", "url": "https://x.com/jane"}

    def test_persian_tweet_is_not_translated(self):
        out = build_reply_text({"text": "سلام", "lang": "fa", "author": self.author})
        self.assertEqual(out, "سلام\n\n— Jane\n\nProfile: https://x.com/jane")

    def test_translation_included(self):
        out = build_reply_text(
            {"text": "Hello", "lang": "en", "translation": {"text": "سلام"}, "author": self.author}
        )
        self.assertIn("Original:\n\nHello", out)
        self.assertIn("ترجمه فارسی:\n\nسلام", out)

    def test_identical_translation_collapsed(self):
        out = build_reply_text(
            {"text": "ok", "lang": "en", "translation": {"text": "ok"}, "author": {}}
        )
        self.assertEqual(out, "ok")

    def test_missing_author_does_not_leave_dangling_dash(self):
        self.assertEqual(build_reply_text({"text": "hi", "lang": "fa"}), "hi")

    def test_empty_text(self):
        self.assertIsNone(build_reply_text({"text": "  "}))
        self.assertIsNone(build_reply_text({}))


class OffsetFile(unittest.TestCase):
    def test_roundtrip_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "offset.txt")
            self.assertIsNone(load_last_update_id(path))
            save_last_update_id(path, 42)
            self.assertEqual(load_last_update_id(path), 42)
            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_garbage_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "offset.txt")
            with open(path, "w") as f:
                f.write("not a number")
            self.assertIsNone(load_last_update_id(path))


if __name__ == "__main__":
    unittest.main()
