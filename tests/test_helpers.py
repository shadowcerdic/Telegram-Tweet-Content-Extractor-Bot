import os
import tempfile
import unittest
from unittest import mock

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

    def test_identical_translation_is_treated_as_no_translation(self):
        """An echo ("ok" -> "ok") is not a translation; say so instead of printing it twice.

        Behaviour change from 2026-10-28: this used to collapse silently to "ok",
        which looked like the bot had skipped the work.
        """
        out = build_reply_text(
            {"text": "ok", "lang": "en", "translation": {"text": "ok"}, "author": {}}
        )
        assert out is not None
        self.assertEqual(out, "متن اصلی (ترجمه در دسترس نبود):\n\nok")
        self.assertNotIn("Original:", out)

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


class QuoteSection(unittest.TestCase):
    """The quoted post rendered as a Twitter-style card under the outer tweet."""

    FA_QUOTE = {
        "id": "2",
        "text": "متن نقل‌قول",
        "lang": "fa",
        "url": "https://x.com/q/status/2",
        "author": {"name": "Q Name", "screen_name": "q"},
    }

    def test_no_quote_renders_nothing(self):
        self.assertEqual(helpers.build_quote_section(None), "")
        self.assertEqual(helpers.build_quote_section({}), "")
        self.assertEqual(helpers.build_quote_section({"id": "2", "text": ""}), "")

    def test_card_block_with_author_and_link(self):
        block = helpers.build_quote_section(self.FA_QUOTE)
        self.assertIn("↩️ نقل‌قول از Q Name (@q):", block)
        self.assertIn("متن نقل‌قول", block)
        self.assertIn("🔗 https://x.com/q/status/2", block)
        # The outer author signature must not leak into the card.
        self.assertNotIn("Profile:", block)

    def test_card_keeps_original_and_translation(self):
        quote = {
            "id": "3",
            "text": "hello there",
            "lang": "en",
            "translation": {"text": "سلام"},
            "author": {"name": "E", "screen_name": "e"},
        }
        block = helpers.build_quote_section(quote)
        self.assertIn("Original:\n\nhello there", block)
        self.assertIn("ترجمه فارسی:\n\nسلام", block)

    def test_unavailable_quote_is_announced_not_silent(self):
        block = helpers.build_quote_section(None, unavailable="deleted")
        self.assertIn("در دسترس نیست (deleted)", block)

    def test_author_label_fallbacks(self):
        self.assertEqual(helpers.quote_author_label(None), "کاربر دیگر")
        self.assertEqual(helpers.quote_author_label({"author": {"name": "Only Name"}}), "Only Name")
        self.assertEqual(helpers.quote_author_label({"author": {"screen_name": "only"}}), "only")

    def test_build_reply_text_unchanged_for_plain_tweets(self):
        """Regression: the fa/translation behaviour must match the pre-quote code."""
        fa = {"text": "سلام", "lang": "fa", "author": {"name": "N", "url": "https://x.com/n"}}
        self.assertEqual(
            build_reply_text(fa),
            "سلام\n\n— N\n\nProfile: https://x.com/n",
        )
        en = {
            "text": "hi",
            "lang": "en",
            "translation": {"text": "سلام"},
            "author": {"name": "N", "url": ""},
        }
        self.assertEqual(build_reply_text(en), "Original:\n\nhi\n\nترجمه فارسی:\n\nسلام\n\n— N")

    def test_failed_translation_is_announced_not_silent(self):
        """A tweet with no translation at all must say so, never look 'finished'."""
        en = {"text": "hello there", "lang": "en", "author": {"name": "N", "url": ""}}
        self.assertEqual(
            build_reply_text(en), "متن اصلی (ترجمه در دسترس نبود):\n\nhello there\n\n— N"
        )

    def test_disabled_translation_is_not_announced(self):
        """TRANSLATE_TO='' means nobody asked for a translation, so no warning."""
        with mock.patch.object(helpers, "TRANSLATE_TO", ""):
            self.assertEqual(build_reply_text({"text": "hello", "lang": "en"}), "hello")

    def test_untranslated_source_language_is_announced(self):
        en = {"text": "hello", "lang": "en", "translation": {"text": "hello"}}
        self.assertEqual(build_reply_text(en), "متن اصلی (ترجمه در دسترس نبود):\n\nhello")

    def test_card_marks_an_untranslatable_quote(self):
        quote = {"id": "4", "text": "long english note", "lang": "en",
                 "author": {"name": "Q", "screen_name": "q"}}
        block = helpers.build_quote_section(quote)
        self.assertIn("↩️ نقل‌قول از Q (@q):", block)
        self.assertIn("متن اصلی (ترجمه در دسترس نبود):", block)


class ThreadRender(unittest.TestCase):
    """A thread renders as ONE continuous message with chain-position counters."""

    def setUp(self):
        self._old = helpers.TRANSLATE_TO
        helpers.TRANSLATE_TO = "fa"
        self.addCleanup(setattr, helpers, "TRANSLATE_TO", self._old)

    @staticmethod
    def _item(item_id, text="", **kw):
        import x_client

        payload_kwargs = {"tweet": {
            "id": item_id,
            "text": text,
            "lang": kw.pop("lang", "fa"),
            "url": f"https://x.com/a/status/{item_id}",
            "author": {"name": "A Name", "screen_name": kw.pop("author", "a"),
                        "url": f"https://x.com/a"},
            **kw,
        }}
        media = kw.pop("media", None)
        payload_kwargs["photos"] = media or []
        return x_client.ThreadItem(payload=x_client.TweetPayload(**payload_kwargs), item_id=item_id)

    def _result(self, items, requested, total=None, truncated=0, dropped=None):
        import x_client

        return x_client.ThreadResult(
            items=items,
            root=items[0] if items else x_client.ThreadItem(item_id=""),
            focal=items[0].payload if items and items[0].payload else None,
            requested_id=requested,
            total=total if total is not None else len(items),
            truncated=truncated,
            dropped=dropped if dropped is not None else [],
        )

    def test_counters_are_persian_and_ordered(self):
        thread = self._result([self._item("1", "اول"), self._item("2", "دوم")], "1")
        text = helpers.build_thread_text(thread)
        self.assertIn("۱/۲", text)
        self.assertIn("۲/۲", text)
        self.assertIn("اول", text)
        self.assertIn("دوم", text)
        self.assertIn(helpers.THREAD_ITEM_SEPARATOR, text)

    def test_the_link_the_user_sent_is_marked(self):
        thread = self._result([self._item("1", "اول"), self._item("2", "دوم")], "2")
        text = helpers.build_thread_text(thread)
        self.assertIn("👈 توییتی که فرستادید", text)

    def test_every_item_carries_its_own_link(self):
        thread = self._result([self._item("1", "اول"), self._item("2", "دوم")], "1")
        text = helpers.build_thread_text(thread)
        self.assertIn("🔗 https://x.com/a/status/1", text)
        self.assertIn("🔗 https://x.com/a/status/2", text)

    def test_byline_names_the_count_and_author(self):
        thread = self._result([self._item("1", "a"), self._item("2", "b")], "1")
        text = helpers.build_thread_text(thread)
        self.assertIn("🧵 ترد ۲ توییت — @a", text)

    def test_a_hole_keeps_its_position_and_is_labelled(self):
        import x_client

        items = [self._item("1", "اول"), x_client.ThreadItem(unavailable="deleted", item_id=""), self._item("3", "سوم")]
        text = helpers.build_thread_text(self._result(items, "1"))
        self.assertIn("❌ توییت حذف شده", text)
        self.assertIn("۳/۳", text)  # the deleted tweet still counts

    def test_truncation_is_announced_with_the_continuation_link(self):
        items = [self._item("1", "اول"), self._item("2", "دوم")]
        thread = self._result(
            items, "1", total=7, truncated=5, dropped=[self._item("3", "سوم")]
        )
        text = helpers.build_thread_text(thread)
        self.assertIn("⚠️ ۵ توییت دیگر در این ترد بود و ارسال نشد", text)
        # the link opens WHERE WE STOPPED, not back at the root the reader already got
        self.assertIn("ادامه: https://x.com/a/status/3", text)
        self.assertIn("🧵 ترد ۲ از ۷ توییت", text)

    def test_continuation_falls_back_to_the_root_without_a_dropped_item(self):
        items = [self._item("1", "اول"), self._item("2", "دوم")]
        thread = self._result(items, "1", total=7, truncated=5)
        text = helpers.build_thread_text(thread)
        assert text is not None
        self.assertIn("ادامه: https://x.com/a/status/1", text)

    def test_continuation_of_a_dropped_item_without_a_payload(self):
        """A dropped item only carries id + author, so the link is still buildable."""
        import x_client

        items = [self._item("1", "اول"), self._item("2", "دوم")]
        thread = self._result(
            items, "1", total=4, truncated=2,
            dropped=[x_client.ThreadItem(item_id="3", author_screen_name="b")],
        )
        text = helpers.build_thread_text(thread)
        assert text is not None
        self.assertIn("ادامه: https://x.com/b/status/3", text)

    def test_requested_tweet_outside_the_cap_says_so(self):
        items = [self._item("1", "اول"), self._item("2", "دوم")]
        thread = self._result(items, "99", total=40, truncated=38)
        text = helpers.build_thread_text(thread)
        self.assertIn("توییت درخواستی شما در ۲ توییت اول این ترد نبود", text)

    def test_media_only_item_is_not_an_empty_gap(self):
        item = self._item("1", "")
        text = helpers.build_thread_text(self._result([item], "1"))
        self.assertIn("(بدون متن", text)

    def test_translation_and_original_both_render(self):
        item = self._item("1", "hello", lang="en", translation={"text": "سلام"})
        text = helpers.build_thread_text(self._result([item], "1"))
        self.assertIn("Original:\n\nhello", text)
        self.assertIn("ترجمه فارسی:\n\nسلام", text)

    def test_untranslated_item_is_marked_not_silent(self):
        item = self._item("1", "hello", lang="en")
        text = helpers.build_thread_text(self._result([item], "1"))
        self.assertIn("متن اصلی (ترجمه در دسترس نبود):", text)

    def test_quote_card_inside_a_thread_item(self):
        item = self._item("1", "اول")
        import x_client

        item.payload.quote = x_client.TweetPayload(tweet={
            "id": "77", "text": "متن نقل‌قول", "lang": "fa",
            "url": "https://x.com/q/status/77",
            "author": {"name": "Q", "screen_name": "q"},
        })
        text = helpers.build_thread_text(self._result([item], "1"))
        self.assertIn("↩️ نقل‌قول از Q (@q):", text)
        self.assertIn("متن نقل‌قول", text)

    def test_empty_thread_renders_nothing(self):
        self.assertIsNone(helpers.build_thread_text(self._result([], "1")))

    def test_fa_number_covers_multi_digits(self):
        self.assertEqual(helpers.fa_number(20), "۲۰")
        self.assertEqual(helpers.fa_number("7/7"), "۷/۷")

    def test_unavailable_labels(self):
        self.assertEqual(helpers.unavailable_label("deleted"), "حذف شده")
        self.assertEqual(helpers.unavailable_label("suspended"), "حساب معلق است")
        self.assertEqual(helpers.unavailable_label("weird"), "در دسترس نیست (weird)")
        self.assertEqual(helpers.unavailable_label(None), "در دسترس نیست")


if __name__ == "__main__":
    unittest.main()
