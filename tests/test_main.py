import unittest
from unittest import mock

import main
from main import Ctx
from models import MediaItem
from telegram_api import TelegramError

URL_PHOTO = MediaItem("photo", "https://pbs.twimg.com/a.jpg")
URL_VIDEO = MediaItem("video", "https://video.twimg.com/b.mp4")


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
        with mock.patch.object(main, "handle_tweet", side_effect=lambda ctx, _id: seen.append(ctx)):
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
             mock.patch.object(main, "get_tweet_payload", return_value=None):
            main.handle_tweet(ctx, "1")
        self.assertEqual(edit.call_args.args[2], "Could not fetch that tweet text.")

    def test_unexpected_exception_is_contained(self):
        ctx = Ctx(1, 2)
        with mock.patch.object(main, "send_message", return_value={"message_id": 5}), \
             mock.patch.object(main, "edit_message_text") as edit, \
             mock.patch.object(main, "send_chat_action"), \
             mock.patch.object(main, "get_tweet_payload", side_effect=ValueError("bad json")):
            main.handle_tweet(ctx, "1")  # must not raise
        self.assertEqual(edit.call_args.args[2], "Failed to process X link.")


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

        self.assertEqual(processed, [1, 2])
        self.assertEqual([c.args[1] for c in save.call_args_list], [2, 3])


if __name__ == "__main__":
    unittest.main()
