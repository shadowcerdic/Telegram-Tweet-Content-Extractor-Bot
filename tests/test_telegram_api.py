import json
import os
import tempfile
import unittest
from unittest import mock

import requests

import constants
import telegram_api
from models import MediaItem
from telegram_api import TelegramError

TOKEN = "123456:SECRET-TOKEN"


def fake_response(payload, status=200):
    resp = mock.Mock()
    resp.status_code = status
    resp.json.return_value = payload
    return resp


class CallTests(unittest.TestCase):
    def setUp(self):
        self._old = constants.BOT_TOKEN
        constants.BOT_TOKEN = TOKEN

    def tearDown(self):
        constants.BOT_TOKEN = self._old

    def test_returns_result(self):
        with mock.patch.object(telegram_api._session, "post", return_value=fake_response({"ok": True, "result": {"x": 1}})):
            self.assertEqual(telegram_api._call("getMe"), {"x": 1})

    def test_error_carries_telegram_description(self):
        resp = fake_response({"ok": False, "error_code": 400, "description": "Bad Request: message is not modified"}, 400)
        with mock.patch.object(telegram_api._session, "post", return_value=resp):
            with self.assertRaises(TelegramError) as cm:
                telegram_api._call("editMessageText", {"chat_id": 1})
        self.assertEqual(cm.exception.error_code, 400)
        self.assertIn("message is not modified", str(cm.exception))

    def test_network_error_never_leaks_token(self):
        # requests' own exception text contains the full URL, token included.
        boom = requests.ConnectionError(f"HTTPSConnectionPool: url: /bot{TOKEN}/getUpdates")
        with mock.patch.object(telegram_api._session, "post", side_effect=boom):
            with self.assertRaises(TelegramError) as cm:
                telegram_api._call("getUpdates")
        self.assertNotIn("SECRET", str(cm.exception))
        self.assertNotIn("SECRET", repr(cm.exception.__cause__))
        self.assertTrue(cm.exception.__suppress_context__)

    def test_flood_control_retried_once(self):
        limited = fake_response({"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 2}}, 429)
        good = fake_response({"ok": True, "result": True})
        with mock.patch.object(telegram_api._session, "post", side_effect=[limited, good]) as post, \
             mock.patch.object(telegram_api.time, "sleep") as sleep:
            self.assertTrue(telegram_api._call("sendMessage", {"chat_id": 1}))
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(3)

    def test_json_body_for_plain_calls(self):
        with mock.patch.object(telegram_api._session, "post", return_value=fake_response({"ok": True, "result": 1})) as post:
            telegram_api.send_message(5, "hi", reply_to_message_id=9, message_thread_id=7)
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["reply_parameters"], {"message_id": 9, "allow_sending_without_reply": True})
        self.assertEqual(body["message_thread_id"], 7)
        self.assertEqual(body["link_preview_options"], {"is_disabled": True})

    def test_none_values_dropped(self):
        with mock.patch.object(telegram_api._session, "post", return_value=fake_response({"ok": True, "result": []})) as post:
            telegram_api.get_updates(None)
        self.assertNotIn("offset", post.call_args.kwargs["json"])
        self.assertEqual(post.call_args.kwargs["json"]["allowed_updates"], ["message"])


class MediaTests(unittest.TestCase):
    def setUp(self):
        self._old = constants.BOT_TOKEN
        constants.BOT_TOKEN = TOKEN
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "a.jpg")
        with open(self.path, "wb") as f:
            f.write(b"jpegdata")

    def tearDown(self):
        constants.BOT_TOKEN = self._old
        self.tmp.cleanup()

    def test_single_url_photo(self):
        with mock.patch.object(telegram_api._session, "post", return_value=fake_response({"ok": True, "result": {}})) as post:
            telegram_api.send_media(1, MediaItem("photo", "https://x/y.jpg"), caption="c" * 2000)
        self.assertTrue(post.call_args[0][0].endswith("/sendPhoto"))
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["photo"], "https://x/y.jpg")
        self.assertEqual(len(body["caption"]), 1024)

    def test_single_local_video_is_uploaded(self):
        vid = os.path.join(self.tmp.name, "v.mp4")
        with open(vid, "wb") as f:
            f.write(b"mp4")
        with mock.patch.object(telegram_api._session, "post", return_value=fake_response({"ok": True, "result": {}})) as post:
            telegram_api.send_media(1, MediaItem("video", vid))
        self.assertTrue(post.call_args[0][0].endswith("/sendVideo"))
        self.assertIn("video", post.call_args.kwargs["files"])
        self.assertEqual(post.call_args.kwargs["data"]["supports_streaming"], "true")

    def test_group_mixes_urls_and_files(self):
        items = [MediaItem("photo", "https://x/1.jpg"), MediaItem("photo", self.path)]
        with mock.patch.object(telegram_api._session, "post", return_value=fake_response({"ok": True, "result": []})) as post:
            telegram_api.send_media_group(1, items, caption="cap", reply_to_message_id=3)
        media = json.loads(post.call_args.kwargs["data"]["media"])
        self.assertEqual(media[0]["media"], "https://x/1.jpg")
        self.assertEqual(media[0]["caption"], "cap")
        self.assertEqual(media[1]["media"], "attach://file1")
        self.assertEqual(list(post.call_args.kwargs["files"]), ["file1"])

    def test_group_size_validated(self):
        with self.assertRaises(ValueError):
            telegram_api.send_media_group(1, [MediaItem("photo", "https://x/1.jpg")])
        with self.assertRaises(ValueError):
            telegram_api.send_media_group(1, [MediaItem("photo", "https://x/1.jpg")] * 11)


if __name__ == "__main__":
    unittest.main()
