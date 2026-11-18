"""The source-link tail: the linked tweet's own URL at the very end of the
delivery, because DELETE_SOURCE_LINK removes the message that carried it.

The handler appends the tail AFTER all composition (body, quote card, oversized
notes). It is deduped against what the reader can already SEE: the plain
layout's per-item links suppress a second copy, a hidden continuation href does
not. Payloads without a url keep the pre-feature bytes exactly.
"""

import unittest
from unittest import mock

import constants
import helpers
import main
from main import Ctx
from x_client import ThreadItem, ThreadResult, TweetPayload

URL = "https://x.com/a/status/1"
RICH_TAIL = f'🔗 <a href="{URL}">{URL}</a>'


def author():
    return {"name": "A", "screen_name": "a", "url": "https://x.com/a"}


def tweet(text: str = "hello", translated: str | None = "سلام",
          url: str | None = URL, lang: str = "en"):
    data = {"id": "1", "text": text, "lang": lang, "author": author()}
    if translated:
        data["translation"] = {"text": translated}
    if url:
        data["url"] = url
    return data


class HelperTail(unittest.TestCase):
    def setUp(self):
        self._rich = constants.RICH_LAYOUT

    def tearDown(self):
        constants.RICH_LAYOUT = self._rich

    def test_rich_tail_is_visible_url_on_anchor(self):
        constants.RICH_LAYOUT = True
        self.assertEqual(helpers.source_link_tail(tweet(), "body"), RICH_TAIL)

    def test_plain_tail_is_bare_url(self):
        constants.RICH_LAYOUT = False
        self.assertEqual(helpers.source_link_tail(tweet(), "body"), f"🔗 {URL}")

    def test_missing_url_means_no_tail(self):
        for rich in (True, False):
            constants.RICH_LAYOUT = rich
            self.assertEqual(helpers.source_link_tail(tweet(url=None), "body"), "")
            self.assertEqual(helpers.source_link_tail(tweet(url="   "), "body"), "")
            self.assertEqual(helpers.source_link_tail({}, "body"), "")
            self.assertEqual(helpers.source_link_tail(None, "body"), "")  # type: ignore[arg-type]

    def test_visible_url_dedupes_in_both_layouts(self):
        for rich in (True, False):
            constants.RICH_LAYOUT = rich
            self.assertEqual(helpers.source_link_tail(tweet(), f"🔗 {URL}"), "")

    def test_hidden_href_does_not_dedupe(self):
        # A rich continuation anchor is tappable but its url is not copyable:
        # the visible tail must still be appended.
        constants.RICH_LAYOUT = True
        text = f'⚠️ note <a href="{URL}">ادامه را باز کنید</a>'
        self.assertEqual(helpers.source_link_tail(tweet(), text), RICH_TAIL)

    def test_url_entities_are_escaped(self):
        constants.RICH_LAYOUT = True
        u = "https://x.com/a/status/1?s=20&t=x"
        tail = helpers.source_link_tail(tweet(url=u), "body")
        self.assertEqual(tail.count("&amp;"), 2)  # href + visible text
        self.assertNotIn("?s=20&t=x", tail)


class HandlerBase(unittest.TestCase):
    def setUp(self):
        self._rich = constants.RICH_LAYOUT
        self._orig = constants.SHOW_ORIGINAL
        self.ctx = Ctx(chat_id=10, message_id=99, thread_id=None)
        patches = {
            "send_message": mock.patch.object(
                main, "send_message", return_value={"message_id": 5}
            ),
            "send_media": mock.patch.object(main, "send_media"),
            "send_media_group": mock.patch.object(main, "send_media_group"),
            "send_chat_action": mock.patch.object(main, "send_chat_action"),
            "delete_message": mock.patch.object(main, "delete_message"),
            "edit_message_text": mock.patch.object(main, "edit_message_text"),
            "translate_to": mock.patch.object(helpers, "TRANSLATE_TO", "fa"),
        }
        self.m = {k: p.start() for k, p in patches.items()}
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        constants.RICH_LAYOUT = self._rich
        constants.SHOW_ORIGINAL = self._orig

    def run_single(self, payload):
        with mock.patch.object(main, "get_tweet_payload", return_value=payload), \
             mock.patch.object(main, "get_thread_result", return_value=None):
            return main.handle_tweet(self.ctx, "1", frozenset())

    def sent_texts(self):
        """Only real chat messages, not the ⏳/📥/📤 status bubble."""
        return [
            c.args[1] for c in self.m["send_message"].call_args_list
            if not c.args[1].startswith(("⏳", "📥", "📤"))
        ]

    def first_caption(self):
        calls = self.m["send_media"].call_args_list
        self.assertTrue(calls, "expected a media send")
        return calls[0].kwargs.get("caption")


class RichSingle(HandlerBase):
    def setUp(self):
        super().setUp()
        constants.RICH_LAYOUT = True
        constants.SHOW_ORIGINAL = True

    def test_caption_ends_with_source_tail(self):
        p = TweetPayload(tweet=tweet(), photos=["https://e/p.jpg"])
        self.assertTrue(self.run_single(p))
        cap = self.first_caption()
        self.assertTrue(cap.endswith(RICH_TAIL))
        self.assertEqual(self.m["send_media"].call_args_list[0].kwargs["parse_mode"], "HTML")

    def test_text_only_message_ends_with_tail(self):
        self.assertTrue(self.run_single(TweetPayload(tweet=tweet())))
        texts = self.sent_texts()
        self.assertEqual(len(texts), 1)
        self.assertTrue(texts[0].endswith(RICH_TAIL))

    def test_tail_lands_after_quote_card(self):
        q = TweetPayload(tweet={
            "id": "2", "text": "quoted", "lang": "en",
            "translation": {"text": "نقل"},
            "url": "https://x.com/q/status/2",
            "author": {"name": "Q", "screen_name": "q", "url": "https://x.com/q"},
        })
        self.assertTrue(self.run_single(TweetPayload(tweet=tweet(), quote=q)))
        t = self.sent_texts()[0]
        self.assertLess(t.index("نقل"), t.index("🔗 <a"))
        self.assertTrue(t.endswith(RICH_TAIL))
        self.assertEqual(t.count(RICH_TAIL), 1)

    def test_media_only_tweet_gets_link_caption(self):
        p = TweetPayload(tweet=tweet(text="", translated=None), photos=["https://e/p.jpg"])
        self.assertTrue(self.run_single(p))
        self.assertEqual(self.first_caption(), RICH_TAIL)

    def test_empty_content_error_ships_no_tail(self):
        p = TweetPayload(tweet=tweet(text="", translated=None))
        self.assertFalse(self.run_single(p))
        for c in self.m["send_message"].call_args_list:
            self.assertNotIn("🔗", c.args[1])
        self.assertTrue(any(
            "خالی" in c.args[2] for c in self.m["edit_message_text"].call_args_list
        ))

    def test_no_url_keeps_pre_feature_bytes(self):
        p = TweetPayload(tweet=tweet(url=None), photos=["https://e/p.jpg"])
        self.assertTrue(self.run_single(p))
        cap = self.first_caption()
        self.assertEqual(cap, helpers.build_reply_text(p.tweet))
        self.assertNotIn("🔗", cap)

    def test_tail_lands_after_oversized_note(self):
        p = TweetPayload(tweet=tweet(), oversized_videos=[{"url": "https://v/big.mp4"}])
        self.assertTrue(self.run_single(p))
        t = self.sent_texts()[0]
        self.assertLess(t.index("⚠️"), t.index("🔗 <a"))
        self.assertTrue(t.endswith(RICH_TAIL))

    def test_long_text_chunks_keep_tags_balanced_and_tail_last(self):
        long_fa = ("سلام " * 900).strip()  # > MAX_MESSAGE_LENGTH
        p = TweetPayload(
            tweet=tweet(text="source", translated=long_fa), photos=["https://e/p.jpg"]
        )
        self.assertTrue(self.run_single(p))
        texts = self.sent_texts()
        self.assertGreater(len(texts), 1)
        for t in texts:
            self.assertEqual(t.count("<a "), t.count("</a>"))
            self.assertEqual(t.count("<b>"), t.count("</b>"))
        self.assertTrue(texts[-1].endswith(RICH_TAIL))
        self.m["send_media"].assert_called_once()
        self.assertIsNone(self.m["send_media"].call_args.kwargs["caption"])


class PlainSingle(HandlerBase):
    def setUp(self):
        super().setUp()
        constants.RICH_LAYOUT = False

    def test_plain_caption_ends_with_bare_tail(self):
        p = TweetPayload(tweet=tweet(), photos=["https://e/p.jpg"])
        self.assertTrue(self.run_single(p))
        cap = self.first_caption()
        self.assertTrue(cap.endswith(f"🔗 {URL}"))
        self.assertNotIn("parse_mode", self.m["send_media"].call_args_list[0].kwargs)

    def test_quote_url_does_not_dedupe_outer_tail(self):
        q = TweetPayload(tweet={
            "id": "2", "text": "quoted", "lang": "fa",
            "url": "https://x.com/q/status/2",
            "author": {"name": "Q", "screen_name": "q"},
        })
        self.assertTrue(self.run_single(TweetPayload(tweet=tweet(), quote=q)))
        t = self.sent_texts()[0]
        self.assertIn("🔗 https://x.com/q/status/2", t)
        self.assertTrue(t.endswith(f"🔗 {URL}"))


def item(i, text, photos=()):
    data = {
        "id": str(i), "text": text, "lang": "fa",
        "url": f"https://x.com/a/status/{i}", "author": author(),
    }
    return ThreadItem(payload=TweetPayload(tweet=data, photos=list(photos)), item_id=str(i))


def thread_of(items, requested="1", total=None, dropped=None):
    return ThreadResult(
        items=items, root=items[0], focal=items[0].payload,
        requested_id=requested, total=total if total is not None else len(items),
        truncated=len(dropped or []), dropped=list(dropped or []),
    )


class RichThread(HandlerBase):
    def setUp(self):
        super().setUp()
        constants.RICH_LAYOUT = True
        constants.SHOW_ORIGINAL = False

    def run_thread(self, thread, payload):
        with mock.patch.object(main, "get_tweet_payload", return_value=payload), \
             mock.patch.object(main, "get_thread_result", return_value=thread):
            return main.handle_tweet(self.ctx, thread.requested_id, frozenset())

    def test_thread_text_ends_with_focal_tail(self):
        th = thread_of([item(1, "اول"), item(2, "دوم")], requested="2")
        self.assertTrue(self.run_thread(th, th.focal))
        t = self.sent_texts()[0]
        u = "https://x.com/a/status/2"
        self.assertTrue(t.endswith(f'🔗 <a href="{u}">{u}</a>'))
        self.assertEqual(t.count(f'🔗 <a href="{u}">{u}</a>'), 1)

    def test_focal_outside_slice_still_gets_visible_tail(self):
        # The rich continuation anchor hides the focal url behind a label;
        # the visible tail must still land at the end.
        th = thread_of(
            [item(1, "اول")], requested="2", total=2,
            dropped=[ThreadItem(item_id="2", author_screen_name="a")],
        )
        focal = TweetPayload(tweet={
            "id": "2", "text": "دوم", "lang": "fa",
            "url": "https://x.com/a/status/2", "author": author(),
        })
        self.assertTrue(self.run_thread(th, focal))
        t = self.sent_texts()[0]
        u = "https://x.com/a/status/2"
        self.assertTrue(t.endswith(f'🔗 <a href="{u}">{u}</a>'))

    def test_short_thread_caption_carries_tail(self):
        th = thread_of([item(1, "کوتاه", photos=["https://p/1.jpg"]), item(2, "دوم")])
        self.assertTrue(self.run_thread(th, th.focal))
        cap = self.first_caption()
        self.assertTrue(cap.endswith(RICH_TAIL))


class PlainThread(HandlerBase):
    def setUp(self):
        super().setUp()
        constants.RICH_LAYOUT = False

    def test_focal_url_not_duplicated(self):
        th = thread_of([item(1, "اول"), item(2, "دوم")], requested="1")
        with mock.patch.object(main, "get_tweet_payload", return_value=th.focal), \
             mock.patch.object(main, "get_thread_result", return_value=th):
            self.assertTrue(main.handle_tweet(self.ctx, "1", frozenset()))
        t = self.sent_texts()[0]
        self.assertEqual(t.count("🔗 https://x.com/a/status/1"), 1)
        self.assertEqual(t.count("🔗 https://x.com/a/status/2"), 1)


if __name__ == "__main__":
    unittest.main()
