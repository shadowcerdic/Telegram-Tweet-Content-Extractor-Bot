import unittest
from unittest import mock

import constants
import helpers
import main
from constants import MAX_MESSAGE_LENGTH
from main import Ctx
from models import MediaItem
from telegram_api import TelegramError

URL_PHOTO = MediaItem("photo", "https://pbs.twimg.com/a.jpg")
URL_VIDEO = MediaItem("video", "https://video.twimg.com/b.mp4")


_saved_layout = None


def setUpModule():
    """These modules guard the PLAIN layout byte-for-byte.

    Production .env switches the clean layout on, and feature flags must never
    be inherited by a test run -- pinning here (unittest calls it around this
    module's tests, not at import) keeps the assertion independent of both the
    machine and the order the modules happen to run in.
    """
    global _saved_layout
    _saved_layout = constants.RICH_LAYOUT
    constants.RICH_LAYOUT = False


def tearDownModule():
    constants.RICH_LAYOUT = _saved_layout


class Deliver(unittest.TestCase):
    def setUp(self):
        self.ctx = Ctx(chat_id=10, message_id=99, thread_id=None)
        patches = {
            "send_message": mock.patch.object(main, "send_message"),
            "send_media": mock.patch.object(main, "send_media"),
            "send_media_group": mock.patch.object(main, "send_media_group"),
            "send_chat_action": mock.patch.object(main, "send_chat_action"),
        }
        self.m = {k: p.start() for k, p in patches.items()}
        self.addCleanup(mock.patch.stopall)

    def test_short_text_becomes_caption(self):
        main.deliver(self.ctx, [URL_PHOTO], "hello")
        self.m["send_media"].assert_called_once_with(10, URL_PHOTO, caption="hello", reply_to_message_id=99)
        self.m["send_message"].assert_not_called()

    def test_long_text_sent_first_then_media_without_reply(self):
        main.deliver(self.ctx, [URL_PHOTO], "x" * 1500)
        self.m["send_message"].assert_called_once()
        self.assertEqual(self.m["send_message"].call_args.kwargs["reply_to_message_id"], 99)
        self.m["send_media"].assert_called_once_with(10, URL_PHOTO, caption=None)

    def test_text_only(self):
        main.deliver(self.ctx, [], "just text")
        self.m["send_message"].assert_called_once_with(10, "just text", reply_to_message_id=99)
        self.m["send_media"].assert_not_called()

    def test_album_caption_only_on_first_chunk(self):
        items = [MediaItem("photo", f"https://x/{i}.jpg") for i in range(11)]
        main.deliver(self.ctx, items, "cap")
        calls = self.m["send_media_group"].call_args_list
        self.assertEqual([len(c.args[1]) for c in calls], [6, 5])
        self.assertEqual(calls[0].kwargs["caption"], "cap")
        self.assertIsNone(calls[1].kwargs["caption"])
        self.assertIn("reply_to_message_id", calls[0].kwargs)
        self.assertNotIn("reply_to_message_id", calls[1].kwargs)

    def test_forum_topic_thread_kept_on_every_message(self):
        ctx = Ctx(chat_id=10, message_id=99, thread_id=5)
        main.deliver(ctx, [], "x" * 5000)  # several message parts
        for call in self.m["send_message"].call_args_list:
            self.assertEqual(call.kwargs["message_thread_id"], 5)
        first, second = self.m["send_message"].call_args_list[:2]
        self.assertIn("reply_to_message_id", first.kwargs)
        self.assertNotIn("reply_to_message_id", second.kwargs)

    def test_falls_back_to_upload_when_telegram_cannot_fetch_url(self):
        self.m["send_media"].side_effect = [
            TelegramError("sendVideo", "Bad Request: failed to get HTTP URL content", 400),
            None,
        ]
        local = MediaItem("video", "/tmp/local.mp4")
        with mock.patch.object(main, "download_media", return_value=local) as dl:
            main.deliver(self.ctx, [URL_VIDEO], "cap")
        dl.assert_called_once()
        self.assertEqual(self.m["send_media"].call_args_list[1].args[1], local)

    def test_other_telegram_errors_are_not_retried(self):
        self.m["send_media"].side_effect = TelegramError("sendVideo", "Forbidden: bot was kicked", 403)
        with mock.patch.object(main, "download_media") as dl:
            with self.assertRaises(TelegramError):
                main.deliver(self.ctx, [URL_VIDEO], None)
        dl.assert_not_called()

    def test_mixed_album_fallback_downloads_only_video_first(self):
        self.m["send_media_group"].side_effect = [
            TelegramError("sendMediaGroup", "Bad Request: failed to get HTTP URL content", 400),
            None,
        ]
        local_vid = MediaItem("video", "/tmp/local.mp4")
        with mock.patch.object(main, "download_media", return_value=local_vid) as dl:
            main.deliver(self.ctx, [URL_PHOTO, URL_VIDEO], "cap")
        dl.assert_called_once_with(URL_VIDEO, mock.ANY)
        retried_group = self.m["send_media_group"].call_args_list[1].args[1]
        self.assertEqual(retried_group, [URL_PHOTO, local_vid])

    def test_fallback_propagates_to_second_chunk(self):
        self.m["send_media"].side_effect = [
            TelegramError("sendVideo", "Bad Request: failed to get HTTP URL content", 400),
            None,
            None,
        ]
        local = MediaItem("photo", "/tmp/local.jpg")
        with mock.patch.object(main, "download_media", return_value=local) as dl:
            main.deliver(
                self.ctx,
                [URL_VIDEO],
                "cap",
                extra_items=[URL_PHOTO],
                extra_caption="quote",
            )
        # First chunk hit 400 -> downloaded URL_VIDEO; second chunk inherited force_local -> downloaded URL_PHOTO directly
        self.assertEqual(dl.call_count, 2)
        self.assertEqual(self.m["send_media"].call_count, 3)


class MessageParsing(unittest.TestCase):
    def test_hidden_link_in_text_link_entity_is_found(self):
        msg = {
            "text": "look at this",
            "entities": [{"type": "text_link", "url": "https://x.com/a/status/42", "offset": 0, "length": 4}],
        }
        self.assertIn("https://x.com/a/status/42", main.get_message_text(msg))

    def test_caption_used_when_no_text(self):
        self.assertEqual(main.get_message_text({"caption": " hi "}), "hi")

    def test_bot_messages_and_private_channels_ignored(self):
        with mock.patch.object(main, "handle_tweet") as h:
            main.process_message({"chat": {"id": 1, "type": "private"}, "from": {"is_bot": True},
                                  "text": "https://x.com/a/status/1"})
            main.process_message({"chat": {"id": 1, "type": "channel"}, "text": "https://x.com/a/status/1"})
        h.assert_not_called()

    def test_link_cap_per_message(self):
        text = " ".join(f"https://x.com/a/status/{i}" for i in range(1, 21))
        with mock.patch.object(main, "handle_tweet") as h:
            main.process_message({"chat": {"id": 1, "type": "private"}, "message_id": 2, "text": text})
        self.assertEqual(h.call_count, main.MAX_LINKS_PER_MESSAGE)

    def test_topic_thread_only_for_real_forum_topics(self):
        seen = []
        with mock.patch.object(
            main, "handle_tweet", side_effect=lambda ctx, _id, _s, **_kw: seen.append(ctx)
        ):
            base = {"chat": {"id": 1, "type": "supergroup"}, "message_id": 2,
                    "text": "https://x.com/a/status/1", "message_thread_id": 77}
            main.process_message({**base, "is_topic_message": True})
            main.process_message(base)  # plain reply-thread, not a forum topic
        self.assertEqual([c.thread_id for c in seen], [77, None])

    def test_edited_messages_ignored_by_default(self):
        with mock.patch.object(main, "PROCESS_EDITED_MESSAGES", False), \
             mock.patch.object(main, "process_message") as pm:
            main.process_update({"edited_message": {"text": "x"}})
        pm.assert_not_called()


class StatusIsBestEffort(unittest.TestCase):
    def test_failing_edits_do_not_raise(self):
        ctx = Ctx(1, 2)
        with mock.patch.object(main, "send_message", return_value={"message_id": 5}), \
             mock.patch.object(main, "edit_message_text", side_effect=TelegramError("edit", "boom", 400)), \
             mock.patch.object(main, "delete_message", side_effect=TelegramError("del", "boom", 400)):
            status = main.Status(ctx, "start")
            status.update("next")
            status.finish()  # delete fails -> falls back to edit, which also fails; still no raise


class TweetHandler(unittest.TestCase):
    def test_missing_tweet_reports_cleanly(self):
        ctx = Ctx(1, 2)
        with mock.patch.object(main, "send_message", return_value={"message_id": 5}), \
             mock.patch.object(main, "edit_message_text") as edit, \
             mock.patch.object(main, "send_chat_action"), \
             mock.patch.object(main, "get_thread_result") as thread, \
             mock.patch.object(main, "get_tweet_payload", return_value=None):
            main.handle_tweet(ctx, "1")
        self.assertIn("دریافت این توییت ممکن نشد", edit.call_args.args[2])
        # A tweet that does not exist cannot have a thread: no wasted probe.
        thread.assert_not_called()

    def test_unexpected_exception_is_contained(self):
        ctx = Ctx(1, 2)
        with mock.patch.object(main, "send_message", return_value={"message_id": 5}), \
             mock.patch.object(main, "edit_message_text") as edit, \
             mock.patch.object(main, "send_chat_action"), \
             mock.patch.object(main, "get_thread_result") as thread, \
             mock.patch.object(main, "get_tweet_payload", side_effect=ValueError("bad json")):
            main.handle_tweet(ctx, "1")  # must not raise
        self.assertEqual(edit.call_args.args[2], "❌ خطا در پردازش لینک X.")
        thread.assert_not_called()


class YoutubeHandlers(unittest.TestCase):
    def test_prompt_escapes_html_and_sets_parse_mode(self):
        from youtube_client import YoutubeInfo

        ctx = Ctx(10, 20)
        info = YoutubeInfo(video_id="dQw4w9WgXcQ", title="A < B & C > D", duration=10, thumbnail=None)
        with mock.patch.object(main, "get_video_info", return_value=info), \
             mock.patch.object(main, "send_chat_action"), \
             mock.patch.object(main, "send_message") as send:
            main.handle_youtube_prompt(ctx, "dQw4w9WgXcQ")
        self.assertEqual(send.call_args.kwargs.get("parse_mode"), "HTML")
        sent_text = send.call_args.args[1]
        self.assertIn("<b>A &lt; B &amp; C &gt; D</b>", sent_text)

    def test_callback_renders_clean_persian_youtube_error(self):
        from youtube_client import YoutubeError

        cq = {
            "id": "cq1",
            "data": "yt:dQw4w9WgXcQ:high",
            "message": {"message_id": 55, "chat": {"id": 10}},
        }
        with mock.patch.object(main, "answer_callback_query"), \
             mock.patch.object(main, "send_chat_action"), \
             mock.patch.object(main, "edit_message_text") as edit, \
             mock.patch.object(
                 main,
                 "download_youtube_media",
                 side_effect=YoutubeError("این ویدیوی یوتیوب محدودیت سنی (+۱۸) دارد و بدون لاگین اکانت قابل دانلود نیست."),
             ):
            main.handle_youtube_callback(cq)
        final_msg = edit.call_args_list[-1].args[2]
        self.assertIn("محدودیت سنی", final_msg)
        self.assertNotIn("Traceback", final_msg)


class RunLoop(unittest.TestCase):
    def test_failing_update_does_not_block_the_queue(self):
        """Regression: a raising update used to be re-fetched forever."""
        batches = [[{"update_id": 1}, {"update_id": 2}]]

        def fake_get_updates(offset, allow_edited=False):
            if batches:
                return batches.pop()
            main._stop = True
            return []

        processed = []

        def fake_process(update):
            processed.append(update["update_id"])
            if update["update_id"] == 1:
                raise RuntimeError("boom")

        main._stop = False
        with mock.patch.object(main, "get_updates", side_effect=fake_get_updates), \
             mock.patch.object(main, "process_update", side_effect=fake_process), \
             mock.patch.object(main, "save_last_update_id") as save, \
             mock.patch.object(main, "load_last_update_id", return_value=None), \
             mock.patch.object(main.time, "sleep"):
            main.run_loop()
        main._stop = False

        self.assertCountEqual(processed, [1, 2])
        self.assertEqual([c.args[1] for c in save.call_args_list], [2, 3])

    def test_slow_update_does_not_block_concurrent_update(self):
        import threading

        gate = threading.Event()
        second_finished = threading.Event()
        batches = [[{"update_id": 10}, {"update_id": 11}]]

        def fake_get_updates(offset, allow_edited=False):
            if batches:
                return batches.pop()
            second_finished.wait(timeout=2)
            main._stop = True
            gate.set()
            return []

        order = []

        def fake_process(update):
            uid = update["update_id"]
            if uid == 10:
                gate.wait(timeout=2)
                order.append(10)
            else:
                order.append(11)
                second_finished.set()

        main._stop = False
        with mock.patch.object(main, "get_updates", side_effect=fake_get_updates), \
             mock.patch.object(main, "process_update", side_effect=fake_process), \
             mock.patch.object(main, "save_last_update_id"), \
             mock.patch.object(main, "load_last_update_id", return_value=None), \
             mock.patch.object(main.time, "sleep"):
            main.run_loop()
        main._stop = False

        # Update 11 finished while update 10 was still waiting on gate!
        self.assertEqual(order[0], 11)
        self.assertIn(10, order)


def _quote_payload(
    outer_photos=("https://o1.jpg",), quote_photos=("https://q1.jpg",), has_quote=True
):
    """TweetPayload for a quote post, shaped like x_client returns one."""
    from x_client import TweetPayload

    outer = TweetPayload(
        tweet={
            "id": "1",
            "text": "outer text",
            "lang": "fa",
            "author": {"name": "A", "screen_name": "a", "url": "https://x.com/a"},
        },
        photos=list(outer_photos),
    )
    if has_quote:
        outer.quote = TweetPayload(
            tweet={
                "id": "2",
                "text": "quoted text",
                "lang": "fa",
                "url": "https://x.com/q/status/2",
                "author": {"name": "Q", "screen_name": "q"},
            },
            photos=list(quote_photos),
        )
    return outer


class QuoteCard(unittest.TestCase):
    """handle_tweet must deliver both tweets: text as one card, media per tweet."""

    def setUp(self):
        self.ctx = main.Ctx(chat_id=10, message_id=99, thread_id=None)
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

    def _run(self, payload, siblings=frozenset()):
        with mock.patch.object(main, "get_tweet_payload", return_value=payload), \
             mock.patch.object(main, "get_thread_result", return_value=None):
            main.handle_tweet(self.ctx, "1", siblings)
        return self.m["send_media"].call_args_list

    def test_outer_and_quote_media_both_sent(self):
        calls = self._run(_quote_payload())
        self.assertEqual([c.args[1].source for c in calls], ["https://o1.jpg", "https://q1.jpg"])

    def test_card_text_rides_on_the_outer_media(self):
        caption = self._run(_quote_payload())[0].kwargs["caption"]
        self.assertIn("outer text", caption)
        self.assertIn("↩️ نقل‌قول از Q (@q):", caption)
        self.assertIn("quoted text", caption)

    def test_quote_media_is_labeled_with_its_author(self):
        quote_call = self._run(_quote_payload())[1]
        self.assertEqual(quote_call.kwargs["caption"], "📎 پیوست توییت نقل‌قول‌شده — Q (@q)")
        # Only the first message replies to the user's link.
        self.assertIn("reply_to_message_id", self.m["send_media"].call_args_list[0].kwargs)
        self.assertNotIn("reply_to_message_id", quote_call.kwargs)

    def test_quote_media_is_albummed_after_a_big_outer_album(self):
        payload = _quote_payload(
            outer_photos=[f"https://o{i}.jpg" for i in range(4)],
            quote_photos=[f"https://q{i}.jpg" for i in range(11)],
        )
        with mock.patch.object(main, "get_tweet_payload", return_value=payload), \
             mock.patch.object(main, "get_thread_result", return_value=None):
            main.handle_tweet(self.ctx, "1", frozenset())
        groups = self.m["send_media_group"].call_args_list
        self.assertEqual([len(g.args[1]) for g in groups], [4, 6, 5])
        # First chunk of each set: the outer album wears the card, the quote album
        # wears its own label, the overflow chunk wears nothing.
        self.assertIn("↩️ نقل‌قول از Q (@q):", groups[0].kwargs["caption"])
        self.assertEqual(groups[1].kwargs["caption"], "📎 پیوست توییت نقل‌قول‌شده — Q (@q)")
        self.assertIsNone(groups[2].kwargs["caption"])

    def test_outer_without_media_gives_the_card_to_the_quote(self):
        payload = _quote_payload(outer_photos=())
        calls = self._run(payload)
        self.assertEqual(len(calls), 1)
        self.assertIn("outer text", calls[0].kwargs["caption"])
        self.assertIn("quoted text", calls[0].kwargs["caption"])

    def test_too_long_for_a_caption_sends_text_first(self):
        payload = _quote_payload()
        payload.tweet["text"] = "ل" * 1500
        calls = self._run(payload)
        self.m["send_message"].assert_called()
        self.assertIsNone(calls[0].kwargs["caption"])
        self.assertEqual(calls[1].kwargs["caption"], "📎 پیوست توییت نقل‌قول‌شده — Q (@q)")

    def test_deleted_quote_is_reported_instead_of_dropped(self):
        payload = _quote_payload(quote_photos=())
        payload.quote = None
        payload.quote_unavailable = "deleted"
        calls = self._run(payload)
        self.assertIn("در دسترس نیست (deleted)", calls[0].kwargs["caption"])

    def test_quoted_post_linked_in_the_same_message_is_not_sent_twice(self):
        payload = _quote_payload()
        calls = self._run(payload, siblings=frozenset({"1", "2"}))
        self.assertEqual([c.args[1].source for c in calls], ["https://o1.jpg"])
        self.assertNotIn("نقل‌قول", calls[0].kwargs["caption"])

    def test_oversized_quote_video_is_noted(self):
        payload = _quote_payload(quote_photos=())
        quote = payload.quote
        assert quote is not None
        quote.oversized_videos = [{"url": "https://v/big.mp4", "duration": 900, "bitrate": 8}]
        caption = self._run(payload)[0].kwargs["caption"]
        self.assertIn("ویدیوی توییت نقل‌قول‌شده", caption)
        self.assertIn("https://v/big.mp4", caption)

    def test_plain_tweet_delivery_is_unchanged(self):
        """No quote -> the reply is exactly what the bot sent before this feature."""
        payload = _quote_payload(has_quote=False)
        calls = self._run(payload)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].kwargs["caption"].startswith("outer text"))
        self.assertNotIn("نقل‌قول", calls[0].kwargs["caption"])
        self.m["send_media_group"].assert_not_called()


class SiblingIds(unittest.TestCase):
    def test_process_message_passes_every_tweet_id_in_the_message(self):
        text = "https://x.com/a/status/1 and https://x.com/b/status/2"
        seen = []
        with mock.patch.object(
            main,
            "handle_tweet",
            side_effect=lambda ctx, link_id, sibs, **kw: seen.append((link_id, sibs, kw)),
        ):
            main.process_message({"chat": {"id": 1, "type": "private"}, "message_id": 2, "text": text})
        self.assertEqual([pair[0] for pair in seen], ["1", "2"])
        self.assertTrue(all(pair[1] == frozenset({"1", "2"}) for pair in seen))
        # Both links share ONE thread-root set, so the second link can tell that
        # the thread was already posted by the first.
        roots = [pair[2]["delivered_roots"] for pair in seen]
        self.assertEqual(len(roots), 2)
        self.assertIs(roots[0], roots[1])
        self.assertEqual(roots[0], set())

    def test_process_message_passes_no_thread_state_to_other_handlers(self):
        text = "https://x.com/a/status/1 https://www.instagram.com/p/ABC123/"
        with mock.patch.object(main, "handle_tweet") as tw, \
             mock.patch.object(main, "handle_instagram") as ig:
            main.process_message({"chat": {"id": 1, "type": "private"}, "message_id": 2, "text": text})
        self.assertEqual(tw.call_args.kwargs.keys(), {"delivered_roots"})
        self.assertEqual(ig.call_args.kwargs, {})


class ThreadDelivery(unittest.TestCase):
    """A thread link must deliver the whole chain as one continuous message."""

    def setUp(self):
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
            # Pin the target language so rendered text does not depend on the
            # environment's TRANSLATE_TO (the asserts below are about structure).
            "translate_to": mock.patch.object(helpers, "TRANSLATE_TO", "fa"),
        }
        self.m = {k: p.start() for k, p in patches.items()}
        self.addCleanup(mock.patch.stopall)

    @staticmethod
    def _payload(item_id: str, text: str, photos=(), videos=(), quote=None):
        import x_client

        return x_client.TweetPayload(
            tweet={
                "id": item_id,
                "text": text,
                "lang": "fa",
                "url": f"https://x.com/a/status/{item_id}",
                "author": {"name": "A", "screen_name": "a", "url": "https://x.com/a"},
            },
            photos=list(photos),
            videos=list(videos),
            quote=quote,
        )

    def _item(self, item_id: str, text: str, photos=(), **kw):
        import x_client

        payload = self._payload(item_id, text, photos, **kw)
        return x_client.ThreadItem(payload=payload, item_id=item_id)

    def _thread(self, items, requested, total=None, truncated=0, dropped=None):
        import x_client

        return x_client.ThreadResult(
            items=items,
            root=items[0],
            focal=items[0].payload,
            requested_id=requested,
            total=total if total is not None else len(items),
            truncated=truncated,
            dropped=dropped or [],
        )

    def _run(self, thread, payload, **kw):
        import x_client

        payload = payload or x_client.TweetPayload(tweet={"id": "1", "text": "", "lang": "fa"})
        with mock.patch.object(main, "get_tweet_payload", return_value=payload), \
             mock.patch.object(main, "get_thread_result", return_value=thread):
            main.handle_tweet(self.ctx, thread.requested_id, **kw)

    def _sent_texts(self):
        """Only real chat messages, not the ⏳/📥/📤 status bubble."""
        out = []
        for call in self.m["send_message"].call_args_list:
            text = call.args[1]
            if text.startswith(("⏳", "📥", "📤")):
                continue
            out.append(text)
        return out

    def _all_text(self):
        """Everything the user would read: message bodies and media captions alike."""
        pieces = self._sent_texts()
        for mock_name in ("send_media", "send_media_group"):
            for call in self.m[mock_name].call_args_list:
                caption = call.kwargs.get("caption")
                if caption:
                    pieces.append(caption)
        return "\n".join(pieces)

    def test_quote_media_of_an_inner_item_never_mixes_with_thread_media(self):
        """A quoted post inside the chain keeps its own 📎 album, like a lone tweet."""
        import x_client

        quote = self._payload("9", "quoted", photos=["https://q/1.jpg"])
        items = [
            self._item("1", "اول", photos=["https://p/1.jpg"]),
            self._item("2", "دوم", quote=quote),
        ]
        dropped = [x_client.ThreadItem(item_id="3", author_screen_name="a")]
        thread = self._thread(items, "1", total=3, truncated=1, dropped=dropped)

        self._run(thread, None)
        first = self.m["send_media"].call_args_list[0]
        self.assertEqual(first.args[1].source, "https://p/1.jpg")  # the thread's own file
        self.assertIn("۱/۲", first.kwargs["caption"])
        self.assertNotIn("📎", first.kwargs["caption"])
        self.assertIn("ادامه: https://x.com/a/status/3", first.kwargs["caption"])
        second = self.m["send_media"].call_args_list[1]
        self.assertEqual(second.args[1].source, "https://q/1.jpg")  # the quoted post's
        self.assertIn("📎", second.kwargs["caption"])

    def test_focal_oversized_video_is_announced_once(self):
        """The focal tweet is present twice in the inputs; its note must not be."""
        import x_client

        big = {"url": "https://v/big.mp4", "size": 90_000_000}
        items = [self._item("1", "اول"), self._item("2", "دوم")]
        assert items[0].payload is not None
        items[0].payload.videos = []
        items[0].payload.oversized_videos = [big]
        thread = self._thread(items, "1")
        focal = self._payload("1", "")
        focal.oversized_videos = [big]
        self._run(thread, focal)
        assert self._all_text()
        self.assertEqual(self._all_text().count("https://v/big.mp4"), 1)

    def test_whole_thread_renders_as_one_message_with_counters(self):
        thread = self._thread([self._item("1", "اول"), self._item("2", "دوم")], "1")
        self._run(thread, None)
        sent = self._all_text()
        self.assertEqual(len(self._sent_texts()), 1)
        self.assertIn("۱/۲", sent)
        self.assertIn("۲/۲", sent)
        self.assertIn("اول", sent)
        self.assertIn("دوم", sent)
        self.assertIn("🧵 ترد ۲ توییت — @a", sent)
        # Media-less thread: the body is a real message, not a caption.
        self.m["send_media"].assert_not_called()
        self.m["send_media_group"].assert_not_called()

    def test_media_of_every_item_follows_the_text(self):
        long_text = "ل" * 1400
        thread = self._thread(
            [
                self._item("1", long_text, photos=["https://p/1.jpg", "https://p/2.jpg"]),
                self._item("2", "دوم", photos=["https://p/3.jpg"]),
            ],
            "1",
        )
        self._run(thread, None)
        group = self.m["send_media_group"].call_args
        self.assertEqual([i.source for i in group.args[1]],
                         ["https://p/1.jpg", "https://p/2.jpg", "https://p/3.jpg"])
        # The thread body is over Telegram's 1024-char caption limit, so it goes out
        # as a message first and the album wears the short byline instead.
        body = self._sent_texts()[0]
        self.assertGreater(len(body), main.CAPTION_LIMIT)
        self.assertIn("۱/۲", body)
        self.assertEqual(group.kwargs["caption"], "🧵 ترد ۲ توییت — @a")
        self.m["send_chat_action"].assert_any_call(10, "upload_photo", None)

    def test_lone_tweet_is_untouched_when_probe_returns_none(self):
        """Unchanged behaviour: no thread -> the single-tweet path exactly as before."""
        import x_client

        payload = _quote_payload()
        single = x_client.ThreadResult(
            items=[], root=x_client.ThreadItem(item_id="1"), focal=payload,
            requested_id="1", total=1, truncated=0,
        )
        self._run(single, payload)
        media_calls = self.m["send_media"].call_args_list
        self.assertEqual([c.args[1].source for c in media_calls],
                         ["https://o1.jpg", "https://q1.jpg"])
        caption = media_calls[0].kwargs["caption"]
        self.assertIn("outer text", caption)
        self.assertIn("↩️ نقل‌قول از Q (@q):", caption)
        self.m["send_media_group"].assert_not_called()
        self.assertEqual(self._sent_texts(), [])

    def test_second_link_of_the_same_thread_is_not_posted_twice(self):
        thread = self._thread([self._item("1", "اول"), self._item("2", "دوم")], "1")
        roots: set[str] = set()
        self._run(thread, None, delivered_roots=roots)
        self.assertEqual(roots, {"1"})

        second = self._thread([self._item("1", "اول"), self._item("2", "دوم")], "2")
        outer = _quote_payload()
        self._run(second, outer, delivered_roots=roots)
        # The thread body was sent once; the second link still delivers its own tweet
        # (unchanged behaviour), and it does not repeat the thread text.
        thread_messages = [t for t in self._sent_texts() if "🧵 ترد" in t]
        self.assertEqual(len(thread_messages), 1)
        self.assertEqual(
            [c.args[1].source for c in self.m["send_media"].call_args_list],
            ["https://o1.jpg", "https://q1.jpg"],
        )

    def test_long_thread_chunks_on_item_boundaries(self):
        items = [self._item(str(i), "ل" * 900) for i in range(1, 7)]
        thread = self._thread(items, "1")
        self._run(thread, None)
        texts = self._sent_texts()
        self.assertGreater(len(texts), 1)
        self.assertTrue(all(len(t) <= MAX_MESSAGE_LENGTH for t in texts))
        # No chunk may end mid-sentence: every chunk starts at a chain counter
        # except the tail (byline/notes).
        counters = [t.startswith(helpers.THREAD_ITEM_SEPARATOR) for t in texts]
        self.assertEqual(counters[:-1], [True] * (len(counters) - 1))
        self.assertIn("۶/۶", self._all_text())
        # Every item survived the split exactly once.
        for i in range(1, 7):
            self.assertEqual(self._all_text().count(f"🔗 https://x.com/a/status/{i}"), 1)

    def test_empty_thread_falls_back_to_the_single_tweet(self):
        payload = _quote_payload()
        thread = self._thread([self._item("1", "")], "1")
        thread.items[0].payload.tweet["text"] = ""
        with mock.patch.object(main, "build_thread_text", return_value=None), \
             mock.patch.object(main, "get_tweet_payload", return_value=payload), \
             mock.patch.object(main, "get_thread_result", return_value=thread):
            main.handle_tweet(self.ctx, "1")
        self.assertEqual(
            [c.args[1].source for c in self.m["send_media"].call_args_list],
            ["https://o1.jpg", "https://q1.jpg"],
        )

    def test_thread_caption_never_exceeds_the_caption_limit(self):
        items = [self._item(str(i), "ل" * 2000) for i in range(1, 21)]
        thread = self._thread(items, "1", total=25, truncated=5)
        self._run(thread, None)
        if self.m["send_media_group"].call_args_list:
            for call in self.m["send_media_group"].call_args_list:
                caption = call.kwargs["caption"]
                if caption and "🧵" in caption:
                    self.assertLessEqual(len(caption), main.CAPTION_LIMIT)
        else:
            self.m["send_media"].assert_not_called()

    def test_short_thread_rides_as_the_caption_like_a_single_tweet(self):
        """Unchanged delivery feel: a thread that fits is ONE media message."""
        thread = self._thread(
            [self._item("1", "کوتاه", photos=["https://p/1.jpg"]), self._item("2", "دوم")], "1"
        )
        self._run(thread, None)
        call = self.m["send_media"].call_args_list[0]
        self.assertEqual(call.args[1].source, "https://p/1.jpg")
        self.assertIn("۱/۲", call.kwargs["caption"])
        self.assertIn("🧵 ترد ۲ توییت — @a", call.kwargs["caption"])
        self.assertEqual(call.kwargs["caption"], self._all_text())
        self.assertEqual(call.kwargs["reply_to_message_id"], 99)
        self.assertEqual(self._sent_texts(), [])

    def test_byline_labels_the_last_album_when_the_body_is_a_message(self):
        photos = [f"https://p/{i}.jpg" for i in range(1, 13)]
        thread = self._thread(
            [self._item("1", "ل" * 1400, photos=photos), self._item("2", "دوم")], "1"
        )
        self._run(thread, None)
        groups = self.m["send_media_group"].call_args_list
        self.assertEqual([len(g.args[1]) for g in groups], [6, 6])
        self.assertIsNone(groups[0].kwargs["caption"])
        self.assertEqual(groups[-1].kwargs["caption"], "🧵 ترد ۲ توییت — @a")

    def test_deliver_trailing_caption_is_inert_by_default(self):
        """Regression for every non-thread caller: no trailing label, no behaviour shift."""
        main.deliver(self.ctx, [URL_PHOTO], "x" * 1500)
        self.assertIsNone(self.m["send_media"].call_args.kwargs["caption"])
        main.deliver(self.ctx, [URL_PHOTO], "x" * 1500, trailing_caption="🧵")
        self.assertEqual(self.m["send_media"].call_args_list[-1].kwargs["caption"], "🧵")


if __name__ == "__main__":
    unittest.main()
