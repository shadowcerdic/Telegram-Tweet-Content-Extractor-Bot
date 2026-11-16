"""Tests for the two clean-output features.

RICH_LAYOUT  — Persian-first rendering (HTML byline, no "Original:"/"Profile:",
               short thread separators, one handle link instead of raw URLs).
DELETE_SOURCE_LINK — delete the message that carried the link, but only after
               its content actually reached the chat.
"""

import time
import unittest
from unittest import mock

import constants
import helpers
import main
import telegram_api
from models import MediaItem
from x_client import ThreadItem, ThreadResult, TweetPayload


def author(name="Jane", screen="jane", url="https://x.com/jane"):
    return {"name": name, "screen_name": screen, "url": url}


def tweet(text="Hello", lang="en", translated=None, by=None, url=None):
    data = {"text": text, "lang": lang, "author": by if by is not None else author()}
    if translated:
        data["translation"] = {"text": translated}
    if url:
        data["url"] = url
    return data


class Knob(unittest.TestCase):
    """Both layouts are driven by knobs read at call time, not at import."""

    def setUp(self):
        self._rich = constants.RICH_LAYOUT
        self._orig = constants.SHOW_ORIGINAL

    def tearDown(self):
        constants.RICH_LAYOUT = self._rich
        constants.SHOW_ORIGINAL = self._orig


class RichRendering(Knob):
    def setUp(self):
        super().setUp()
        constants.RICH_LAYOUT = True
        constants.SHOW_ORIGINAL = True

    def test_byline_is_bold_name_plus_linked_handle(self):
        out = helpers.build_reply_text(tweet(text="Hello", translated="سلام"))
        self.assertTrue(out.startswith('<b>Jane</b> <a href="https://x.com/jane">@jane</a>'))
        self.assertNotIn("Profile:", out)
        self.assertNotIn("Original:", out)

    def test_translation_first_then_labeled_source(self):
        out = helpers.build_reply_text(tweet(text="Hello", translated="سلام"))
        self.assertLess(out.index("سلام"), out.index("Hello"))
        self.assertIn("<b>متن اصلی:</b>\n\nHello", out)

    def test_show_original_off_drops_the_source_text(self):
        constants.SHOW_ORIGINAL = False
        out = helpers.build_reply_text(tweet(text="Hello", translated="سلام"))
        self.assertIn("سلام", out)
        self.assertNotIn("Hello", out)
        self.assertNotIn("متن اصلی:", out)

    def test_upstream_titles_and_text_are_escaped(self):
        out = helpers.build_reply_text(
            tweet(text='a <b>bold</b> & "quote"', translated="سلام",
                  by=author(name='<script>x</script>', screen="safe_one"))
        )
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", out)
        self.assertNotIn("<script>x</script>", out)
        self.assertIn('a &lt;b&gt;bold&lt;/b&gt; &amp; "quote"', out)

    def test_html_tags_never_span_a_chunk_boundary(self):
        """chunk_text splits on lines; every tag here lives on one short line."""
        long_body = "سلام " * 3000
        out = helpers.build_reply_text(
            tweet(text="short source", translated=long_body.strip())
        )
        chunks = helpers.chunk_text(out)
        self.assertGreater(len(chunks), 1, "fixture should actually chunk")
        for chunk in chunks:
            # An unbalanced open tag would make Telegram answer 400 for that chunk.
            self.assertEqual(chunk.count("<b>"), chunk.count("</b>"))
            self.assertEqual(chunk.count("<a "), chunk.count("</a>"))

    def test_quote_card_uses_separator_and_linked_author(self):
        quoted = tweet(text="quoted text", translated="متن نقل‌قول",
                       by=author(name="Q", screen="q", url="https://x.com/q"),
                       url="https://x.com/q/status/2")
        block = helpers.build_quote_section(quoted)
        self.assertIn("↩️ نقل‌قول از <b>Q</b> <a href=\"https://x.com/q\">@q</a>", block)
        self.assertNotIn("🔗 https://", block)
        # A card is a summary: the quoted tweet's own source text is not doubled.
        self.assertNotIn("quoted text", block)

    def test_missing_translation_is_still_announced(self):
        out = helpers.build_reply_text(tweet(text="ok", translated="ok"))
        self.assertIn("متن اصلی (ترجمه در دسترس نبود):\n\nok", out)

    def test_no_author_leaves_no_empty_line(self):
        out = helpers.build_reply_text({"text": "hi", "lang": "fa"})
        self.assertEqual(out, "hi")


def thread_result(items, requested="1", total=None, dropped=None):
    first = items[0]
    return ThreadResult(
        items=items,
        root=first,
        focal=first.payload,
        requested_id=requested,
        total=total if total is not None else len(items),
        truncated=len(dropped or []),
        dropped=list(dropped or []),
    )


def payload(text, translated=None, screen="a", name="A", url="https://x.com/a", tweet_url=None,
            photos=()):
    data = {"text": text, "lang": "en", "author": {"name": name, "screen_name": screen, "url": url}}
    if translated:
        data["translation"] = {"text": translated}
    if tweet_url:
        data["url"] = tweet_url
    return TweetPayload(tweet=data, photos=list(photos))


class RichThread(Knob):
    def setUp(self):
        super().setUp()
        constants.RICH_LAYOUT = True
        constants.SHOW_ORIGINAL = False

    def test_short_separators_and_no_per_item_urls(self):
        items = [
            ThreadItem(payload=payload("one", translated="یک", tweet_url="https://x.com/a/status/1"),
                       item_id="1"),
            ThreadItem(payload=payload("two", translated="دو", tweet_url="https://x.com/a/status/2"),
                       item_id="2"),
        ]
        out = helpers.build_thread_text(thread_result(items, requested="2"))
        self.assertIn("── ۱ ──", out)
        self.assertIn("── ۲ ──  پیام شما", out)
        self.assertNotIn("┄", out)
        self.assertNotIn("🔗 https://", out)
        # Same author for both items: the byline is printed once, not per item.
        self.assertEqual(out.count("@a"), 2)  # one in item 1, one in the thread byline

    def test_byline_repeats_when_the_author_changes(self):
        items = [
            ThreadItem(payload=payload("one", translated="یک"), item_id="1"),
            ThreadItem(payload=payload("two", translated="دو", screen="b", name="B",
                                       url="https://x.com/b"), item_id="2"),
        ]
        out = helpers.build_thread_text(thread_result(items, requested="2"))
        self.assertIn("@a", out)
        self.assertIn('@b</a>', out)

    def test_tombstone_is_labeled_not_dropped(self):
        items = [
            ThreadItem(payload=payload("one", translated="یک"), item_id="1"),
            ThreadItem(payload=None, unavailable="deleted", item_id="2", author_screen_name="a"),
        ]
        out = helpers.build_thread_text(thread_result(items, requested="1"))
        self.assertIn("❌ توییت حذف شده.", out)
        self.assertIn("── ۲ ──", out)

    def test_continuation_link_is_an_anchor_in_html_mode(self):
        kept = [ThreadItem(payload=payload("one", translated="یک"), item_id="1")]
        dropped = [ThreadItem(payload=payload("gone", translated="رفته",
                                             url="https://x.com/a",
                                             tweet_url="https://x.com/a/status/9"),
                              item_id="9", author_screen_name="a")]
        out = helpers.build_thread_text(thread_result(kept, total=5, dropped=dropped))
        self.assertIn('<a href="https://x.com/a/status/9">ادامه را باز کنید</a>', out)
        self.assertNotIn("ادامه: https://", out)


class PlainPathUnchanged(Knob):
    """The no-feature path must stay byte-identical, or the rollback is a lie."""

    def test_old_bytes_when_knobs_off(self):
        constants.RICH_LAYOUT = False
        constants.SHOW_ORIGINAL = True
        out = helpers.build_reply_text(tweet(text="Hello", translated="سلام"))
        self.assertIn("Original:\n\nHello", out)
        self.assertIn("ترجمه فارسی:\n\nسلام", out)
        self.assertTrue(out.endswith("— Jane\n\nProfile: https://x.com/jane"))

    def test_thread_old_bytes_when_knob_off(self):
        constants.RICH_LAYOUT = False
        items = [ThreadItem(payload=payload("one"), item_id="1"),
                 ThreadItem(payload=payload("two"), item_id="2")]
        out = helpers.build_thread_text(thread_result(items, requested="2"))
        self.assertIn("┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄", out)
        self.assertIn("۱/۲", out)
        self.assertIn("🧵 ترد ۲ توییت — @a", out)


class DeleteSender(unittest.TestCase):
    """The delivery-side plumbing: caption parse mode and no reply when deleting."""

    def setUp(self):
        self._rich = constants.RICH_LAYOUT
        self._del = constants.DELETE_SOURCE_LINK
        patches = {
            "send_message": mock.patch.object(main, "send_message", return_value={"message_id": 5}),
            "send_media": mock.patch.object(main, "send_media"),
            "send_media_group": mock.patch.object(main, "send_media_group"),
            "send_chat_action": mock.patch.object(main, "send_chat_action"),
            "delete_message": mock.patch.object(main, "delete_message"),
            "edit_message_text": mock.patch.object(main, "edit_message_text"),
        }
        self.m = {k: p.start() for k, p in patches.items()}
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        constants.RICH_LAYOUT = self._rich
        constants.DELETE_SOURCE_LINK = self._del

    def _run_tweet(self, ctx, payload_obj):
        with mock.patch.object(main, "get_tweet_payload", return_value=payload_obj), \
             mock.patch.object(main, "get_thread_result", return_value=None):
            return main.handle_tweet(ctx, "1", frozenset())

    def test_rich_caption_carries_parse_mode(self):
        constants.RICH_LAYOUT = True
        ctx = main.Ctx(chat_id=10, message_id=99)
        self.assertTrue(self._run_tweet(ctx, payload("hi", translated="سلام",
                                                     photos=["https://e/p.jpg"])))
        call = self.m["send_media"].call_args_list[0]
        self.assertEqual(call.kwargs["parse_mode"], "HTML")
        self.assertIn("<b>A</b>", call.kwargs["caption"])

    def test_plain_caption_has_no_parse_mode(self):
        constants.RICH_LAYOUT = False
        ctx = main.Ctx(chat_id=10, message_id=99)
        self._run_tweet(ctx, payload("hi", photos=["https://e/p.jpg"]))
        self.assertNotIn("parse_mode", self.m["send_media"].call_args_list[0].kwargs)

    def test_media_group_parse_mode_lands_on_the_first_entry(self):
        items = [
            MediaItem("photo", "https://o1.jpg"),
            MediaItem("photo", "https://o2.jpg"),
        ]
        captured = {}

        def fake_call(method, data, files=None, timeout=None):
            captured["data"] = data
            return [{"message_id": 1}]

        with mock.patch.object(telegram_api, "_call", side_effect=fake_call):
            telegram_api.send_media_group(1, items, caption="<b>x</b>", parse_mode="HTML")
        entry = captured["data"]["media"][0]
        self.assertEqual(entry["parse_mode"], "HTML")
        self.assertNotIn("parse_mode", captured["data"]["media"][1])


class DeleteSourceMessage(unittest.TestCase):
    def setUp(self):
        self._del = constants.DELETE_SOURCE_LINK
        self._age = constants.DELETE_SOURCE_LINK_MAX_AGE_S
        self._rich = constants.RICH_LAYOUT
        constants.RICH_LAYOUT = False
        patches = {
            "send_message": mock.patch.object(main, "send_message", return_value={"message_id": 5}),
            "send_media": mock.patch.object(main, "send_media"),
            "send_media_group": mock.patch.object(main, "send_media_group"),
            "send_chat_action": mock.patch.object(main, "send_chat_action"),
            "delete_message": mock.patch.object(main, "delete_message"),
            "edit_message_text": mock.patch.object(main, "edit_message_text"),
        }
        self.m = {k: p.start() for k, p in patches.items()}
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        constants.DELETE_SOURCE_LINK = self._del
        constants.DELETE_SOURCE_LINK_MAX_AGE_S = self._age
        constants.RICH_LAYOUT = self._rich

    def _message(self, text="https://x.com/a/status/1"):
        return {
            "message_id": 99,
            "date": int(time.time()),
            "chat": {"id": 10, "type": "private"},
            "from": {"id": 7, "is_bot": False},
            "text": text,
        }

    def _run(self, text="https://x.com/a/status/1", payload_obj=None):
        constants.DELETE_SOURCE_LINK = True
        with mock.patch.object(main, "get_tweet_payload", return_value=payload_obj), \
             mock.patch.object(main, "get_thread_result", return_value=None):
            main.process_message(self._message(text))

    def _source_deletes(self):
        """Deletes aimed at message 99, the user's link (5 is the status bubble)."""
        return [c for c in self.m["delete_message"].call_args_list if c.args == (10, 99)]

    def test_deleted_after_a_successful_delivery(self):
        self._run(payload_obj=payload("hi", translated="سلام", photos=["https://e/p.jpg"]))
        self.assertEqual(self._source_deletes(), [mock.call(10, 99)])

    def test_kept_when_the_link_could_not_be_served(self):
        self._run(payload_obj=None)
        self.assertEqual(self._source_deletes(), [])

    def test_kept_when_the_knob_is_off(self):
        constants.DELETE_SOURCE_LINK = False
        with mock.patch.object(main, "get_tweet_payload",
                               return_value=payload("hi", translated="سلام")), \
             mock.patch.object(main, "get_thread_result", return_value=None):
            main.process_message(self._message())
        self.assertEqual(self._source_deletes(), [])

    def test_replies_are_dropped_so_nothing_quotes_a_deleted_message(self):
        self._run(payload_obj=payload("hi", translated="سلام"))
        for call in self.m["send_message"].call_args_list:
            self.assertNotIn("reply_to_message_id", call.kwargs)
        for call in self.m["send_media"].call_args_list:
            self.assertNotIn("reply_to_message_id", call.kwargs)

    def test_replies_survive_when_the_knob_is_off(self):
        constants.DELETE_SOURCE_LINK = False
        with mock.patch.object(main, "get_tweet_payload",
                               return_value=payload("hi", translated="سلام")), \
             mock.patch.object(main, "get_thread_result", return_value=None):
            main.process_message(self._message())
        self.assertIn("reply_to_message_id", self.m["send_message"].call_args_list[0].kwargs)

    def test_old_message_is_kept_and_reason_logged(self):
        constants.DELETE_SOURCE_LINK_MAX_AGE_S = 60
        message = self._message()
        message["date"] = int(time.time()) - 3600
        with mock.patch.object(main, "get_tweet_payload",
                               return_value=payload("hi", translated="سلام")), \
             mock.patch.object(main, "get_thread_result", return_value=None):
            main.process_message(message)
        self.assertEqual(self._source_deletes(), [])

    def test_refusal_by_telegram_does_not_break_the_delivery(self):
        self.m["delete_message"].side_effect = telegram_api.TelegramError(
            "deleteMessage", "Bad Request: chat not found", 400
        )
        self._run(payload_obj=payload("hi", translated="سلام", photos=["https://e/p.jpg"]))
        self.assertTrue(self.m["send_media"].call_args_list)
        self.assertEqual(len(self._source_deletes()), 1, "the refusal was swallowed")

    def test_youtube_prompt_alone_keeps_the_link(self):
        """The file arrives later from the callback, so the link is not spent yet."""
        constants.DELETE_SOURCE_LINK = True
        with mock.patch.object(main, "get_video_info",
                               return_value=mock.Mock(title="t")), \
             mock.patch.object(main, "get_tweet_payload") as gt:
            main.process_message(self._message("https://youtu.be/abcdefghijklmnop"))
        gt.assert_not_called()
        self.assertEqual(self._source_deletes(), [])


if __name__ == "__main__":
    unittest.main()
