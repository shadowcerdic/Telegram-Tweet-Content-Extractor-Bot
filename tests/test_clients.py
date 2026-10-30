import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

import x_client
from instagram_client import collect_media


class InstagramCollect(unittest.TestCase):
    def test_carousel_order_is_numeric_not_lexicographic(self):
        with tempfile.TemporaryDirectory() as tmp:
            for n in (1, 2, 3, 10, 11):
                (Path(tmp) / f"CODE_{n}.jpg").write_bytes(b"x")
            (Path(tmp) / "CODE_4.mp4").write_bytes(b"x")
            (Path(tmp) / "CODE.json").write_bytes(b"x")  # ignored
            names = [Path(i.source).name for i in collect_media(Path(tmp))]
        self.assertEqual(
            names,
            ["CODE_1.jpg", "CODE_2.jpg", "CODE_3.jpg", "CODE_4.mp4", "CODE_10.jpg", "CODE_11.jpg"],
        )

    def test_only_this_directory_is_read(self):
        # Previously glob("<shortcode>*") over a shared dir could pick up another post's files.
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            (Path(a) / "ABC.jpg").write_bytes(b"x")
            (Path(b) / "ABCdef.jpg").write_bytes(b"x")
            self.assertEqual(len(collect_media(Path(a))), 1)

    def test_webp_image_is_converted_and_collected(self):
        with tempfile.TemporaryDirectory() as tmp:
            webp = Path(tmp) / "CODE_1.webp"
            # generate real minimal webp so ffmpeg can convert it
            import subprocess
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=10x10:d=1", "-vframes", "1", str(webp)],
                capture_output=True,
                check=True,
            )
            items = collect_media(Path(tmp))
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].kind, "photo")
            self.assertTrue(items[0].source.endswith(".jpg"))
            self.assertFalse(webp.exists())
            self.assertTrue(Path(items[0].source).exists())


def resp(status=200, payload=None):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload or {}
    r.raise_for_status.side_effect = requests.HTTPError("err") if status >= 400 else None
    return r


class XClient(unittest.TestCase):
    def test_success_returns_typed_payload(self):
        payload = {"code": 200, "tweet": {"text": "hi", "media": {
            "photos": [{"url": "https://p/1.jpg"}, {"nourl": 1}],
            "videos": [{"url": "https://v/1.mp4"}]}}}
        with mock.patch.object(x_client._session, "get", return_value=resp(200, payload)):
            result = x_client.get_tweet_payload("1", "fa")
        self.assertEqual(result.photos, ["https://p/1.jpg"])
        self.assertEqual([(i.kind, i.source) for i in result.media_items()],
                         [("photo", "https://p/1.jpg"), ("video", "https://v/1.mp4")])

    def test_missing_media_keys(self):
        with mock.patch.object(x_client._session, "get", return_value=resp(200, {"code": 200, "tweet": {"text": "t", "media": None}})):
            result = x_client.get_tweet_payload("1")
        self.assertEqual(result.media_items(), [])

    def test_deleted_tweet_is_none_not_an_error(self):
        with mock.patch.object(x_client._session, "get", return_value=resp(404)):
            self.assertIsNone(x_client.get_tweet_payload("1"))

    def test_server_error_raises(self):
        with mock.patch.object(x_client._session, "get", return_value=resp(500)):
            with self.assertRaises(requests.RequestException):
                x_client.get_tweet_payload("1")


class VideoVariantPicker(unittest.TestCase):
    """These two ran zero times before: they lived inside the __main__ block."""

    def test_pick_best_fitting_video_prefers_highest_quality_under_cap(self):
        from x_client import _pick_best_fitting_video
        video = {
            "duration": 100.0,
            "formats": [
                {"url": "https://v/480.mp4", "bitrate": 200_000, "container": "mp4"},
                {"url": "https://v/720.mp4", "bitrate": 2_000_000, "container": "mp4"},
                {"url": "https://v/4k.mp4", "bitrate": 25_000_000, "container": "mp4"},
            ],
        }
        # 4k is ~350MB, 720 is ~28MB, 480 is ~2.8MB -> should choose 720
        url, over = _pick_best_fitting_video(video, max_bytes=50 * 1024 * 1024)
        self.assertEqual(url, "https://v/720.mp4")
        self.assertIsNone(over)

    def test_pick_best_fitting_video_marks_oversized_when_all_exceed_cap(self):
        from x_client import _pick_best_fitting_video
        video = {
            "duration": 1000.0,
            "url": "https://v/huge.mp4",
            "formats": [
                {"url": "https://v/huge.mp4", "bitrate": 25_000_000, "container": "mp4"},
            ],
        }
        url, over = _pick_best_fitting_video(video, max_bytes=50 * 1024 * 1024)
        self.assertIsNone(url)
        self.assertIsNotNone(over)
        self.assertEqual(over["url"], "https://v/huge.mp4")


def _tweet(text="t", lang="fa", author=None, media=None, **extra):
    """A minimal FxTwitter tweet object with the keys the bot reads."""
    return {
        "id": extra.pop("id", "1"),
        "text": text,
        "lang": lang,
        "author": author if author is not None else {"name": "A", "screen_name": "a"},
        "media": media or {},
        **extra,
    }


def _envelope(tweet):
    return {"code": 200, "tweet": tweet}


def _fetched(*args, **kwargs) -> x_client.TweetPayload:
    """get_tweet_payload, narrowed so the assertions below are honest."""
    result = x_client.get_tweet_payload(*args, **kwargs)
    assert result is not None
    return result


class XClientQuote(unittest.TestCase):
    """A quote post must come back as a second, fully parsed payload."""

    def test_quote_media_uses_the_same_50mb_picker(self):
        quoted_video = {
            "duration": 100.0,
            "url": "https://v/hq.mp4",
            "formats": [
                {"url": "https://v/lo.mp4", "bitrate": 200_000, "container": "mp4"},
                {"url": "https://v/hq.mp4", "bitrate": 25_000_000, "container": "mp4"},
            ],
        }
        payload = _envelope(_tweet(
            text="outer",
            quote=_tweet(id="2", text="quoted", media={"videos": [quoted_video]}),
        ))
        with mock.patch.object(x_client._session, "get", return_value=resp(200, payload)) as get:
            result = _fetched("1")
        quote = result.quote
        assert quote is not None
        # 25 Mbps over 100 s is ~350 MB, 0.2 Mbps fits -> the low variant is chosen.
        self.assertEqual(quote.videos, ["https://v/lo.mp4"])
        self.assertEqual(get.call_count, 1)  # already Persian: no extra translate fetch

    def test_foreign_quote_is_refetched_with_translation(self):
        payload = _envelope(_tweet(text="outer", quote=_tweet(id="2", text="hello", lang="en")))
        refreshed = _envelope(_tweet(id="2", text="hello", lang="en",
                                     translation={"text": "سلام"}))
        with mock.patch.object(x_client._session, "get",
                               side_effect=[resp(200, payload), resp(200, refreshed)]) as get, \
             mock.patch.object(x_client.translator, "translate") as fb:
            result = _fetched("1", translate_to="fa")
        quote = result.quote
        assert quote is not None
        urls = [c.args[0] for c in get.call_args_list]
        self.assertEqual(urls[1], "https://api.fxtwitter.com/i/status/2/fa")
        self.assertEqual(quote.tweet["translation"]["text"], "سلام")
        fb.assert_not_called()  # fxtwitter answered on the retry: no third-party call

    def test_translation_refetch_failure_keeps_the_quote(self):
        """A dead translate call must not lose the quoted tweet or crash the handler."""
        payload = _envelope(_tweet(text="outer", quote=_tweet(id="2", text="hello", lang="en")))
        with mock.patch.object(x_client._session, "get",
                               side_effect=[resp(200, payload), requests.RequestException("down")]), \
             mock.patch.object(x_client.translator, "translate", return_value="سلام") as fb:
            result = _fetched("1", translate_to="fa")
        quote = result.quote
        assert quote is not None
        self.assertEqual(quote.tweet["translation"]["text"], "سلام")
        self.assertEqual(quote.tweet["translation"]["provider"], "google-fallback")
        fb.assert_called_once_with("hello", "fa")

    def test_deleted_quote_becomes_a_reason_not_a_crash(self):
        payload = _envelope(_tweet(quote={"type": "tombstone", "reason": "deleted"}))
        with mock.patch.object(x_client._session, "get", return_value=resp(200, payload)):
            result = _fetched("1")
        self.assertIsNone(result.quote)
        self.assertEqual(result.quote_unavailable, "deleted")

    def test_quote_depth_is_capped_at_one(self):
        deep = _tweet(id="2", text="q2", quote=_tweet(id="3", text="q3"))
        payload = _envelope(_tweet(text="outer", quote=deep))
        with mock.patch.object(x_client._session, "get", return_value=resp(200, payload)):
            result = _fetched("1")
        quote = result.quote
        assert quote is not None
        self.assertIsNone(quote.quote)

    def test_self_quote_is_ignored(self):
        payload = _envelope(_tweet(id="1", text="outer", quote=_tweet(id="1", text="outer")))
        with mock.patch.object(x_client._session, "get", return_value=resp(200, payload)):
            result = _fetched("1")
        self.assertIsNone(result.quote)

    def test_plain_tweet_has_no_quote_fields(self):
        payload = _envelope(_tweet(text="just a tweet"))
        with mock.patch.object(x_client._session, "get", return_value=resp(200, payload)):
            result = _fetched("1")
        self.assertIsNone(result.quote)
        self.assertIsNone(result.quote_unavailable)


class TranslationFallback(unittest.TestCase):
    """The measured gap: fxtwitter returns nothing for certain tweet/language pairs."""

    def test_note_tweet_goes_straight_to_the_fallback(self):
        """Long-form tweets fail deterministically, so the re-fetch would be wasted."""
        note = _tweet(id="9", text="x" * 1000, lang="en", is_note_tweet=True)
        with mock.patch.object(x_client._session, "get",
                               return_value=resp(200, _envelope(note))) as get, \
             mock.patch.object(x_client.translator, "translate", return_value="متن فارسی") as fb:
            result = _fetched("9", translate_to="fa")
        self.assertEqual(get.call_count, 1)
        fb.assert_called_once()
        self.assertEqual(result.tweet["translation"]["text"], "متن فارسی")
        self.assertEqual(result.tweet["translation"]["provider"], "google-fallback")

    def test_fallback_also_covers_the_outer_tweet(self):
        outer = _tweet(id="5", text="hello", lang="en")
        with mock.patch.object(x_client._session, "get",
                               side_effect=[resp(200, _envelope(outer)),
                                            resp(200, _envelope(outer))]), \
             mock.patch.object(x_client.translator, "translate", return_value="سلام") as fb:
            result = _fetched("5", translate_to="fa")
        fb.assert_called_once_with("hello", "fa")
        self.assertEqual(result.tweet["translation"]["text"], "سلام")

    def test_when_both_layers_fail_the_tweet_is_still_delivered(self):
        outer = _tweet(id="6", text="hello", lang="en", is_note_tweet=True)
        with mock.patch.object(x_client._session, "get",
                               return_value=resp(200, _envelope(outer))), \
             mock.patch.object(x_client.translator, "translate", return_value=None):
            result = _fetched("6", translate_to="fa")
        self.assertEqual(result.tweet["text"], "hello")
        self.assertFalse((result.tweet.get("translation") or {}).get("text"))

    def test_no_extra_call_when_a_translation_already_exists(self):
        outer = _tweet(id="7", text="hello", lang="en", translation={"text": "سلام"})
        with mock.patch.object(x_client._session, "get",
                               return_value=resp(200, _envelope(outer))) as get, \
             mock.patch.object(x_client.translator, "translate") as fb:
            _fetched("7", translate_to="fa")
        self.assertEqual(get.call_count, 1)
        fb.assert_not_called()

    def test_echo_translation_is_treated_as_missing(self):
        """fx returning the source text verbatim is a failure, not a translation."""
        tweet = _tweet(id="8", text="hello", lang="en")
        tweet["translation"] = {"text": "hello", "source_lang": "en",
                                "target_lang": "fa", "provider": "grok"}
        x_client._scrub_echo(tweet, "fa")
        self.assertIsNone(tweet["translation"])

    def test_echo_is_left_alone_when_already_in_target_language(self):
        tweet = _tweet(id="9", text="سلام", lang="fa")
        tweet["translation"] = {"text": "سلام", "provider": "grok"}
        x_client._scrub_echo(tweet, "fa")
        self.assertEqual(tweet["translation"]["text"], "سلام")

    def test_outer_echo_falls_through_to_the_fallback(self):
        outer = _tweet(id="10", text="hello", lang="en",
                       translation={"text": "hello", "provider": "grok"})
        with mock.patch.object(x_client._session, "get",
                               return_value=resp(200, _envelope(outer))) as get, \
             mock.patch.object(x_client.translator, "translate", return_value="سلام") as fb:
            result = _fetched("10", translate_to="fa")
        self.assertEqual(get.call_count, 2)          # original + one re-fetch
        fb.assert_called_once_with("hello", "fa")
        self.assertEqual(result.tweet["translation"]["provider"], "google-fallback")

    def test_quoted_echo_is_recovered_by_the_fallback(self):
        """Live case: a Japanese quote came back as provider=grok repeating Japanese."""
        ja = "君のチンチンは大きい"
        quote = _tweet(id="2", text=ja, lang="ja",
                       translation={"text": ja, "provider": "grok"})
        outer = _tweet(id="1", text="", lang="fa", quote=quote)
        refetch = _tweet(id="2", text=ja, lang="ja",
                         translation={"text": ja, "provider": "grok"})
        with mock.patch.object(x_client._session, "get",
                               side_effect=[resp(200, _envelope(outer)),
                                            resp(200, _envelope(refetch))]) as get, \
             mock.patch.object(x_client.translator, "translate",
                               return_value="آلت تناسلی تو بزرگ است") as fb:
            result = _fetched("1", translate_to="fa")
        # The nested object already carried an (echo) translation, so the quote
        # refetch was skipped; one re-fetch + the fallback recovered it.
        self.assertEqual(get.call_count, 2)
        fb.assert_called_once_with(ja, "fa")
        assert result.quote is not None
        self.assertEqual(result.quote.tweet["translation"]["provider"], "google-fallback")

    def test_persian_needs_no_translation_call_at_all(self):
        with mock.patch.object(x_client._session, "get",
                               return_value=resp(200, _envelope(_tweet(text="سلام", lang="fa")))) as get, \
             mock.patch.object(x_client.translator, "translate") as fb:
            _fetched("8", translate_to="fa")
        self.assertEqual(get.call_count, 1)
        fb.assert_not_called()

    def test_switch_off_makes_no_extra_requests(self):
        outer = _tweet(id="10", text="hello", lang="en")
        with mock.patch.object(x_client, "TRANSLATE_FALLBACK", "off"), \
             mock.patch.object(x_client._session, "get",
                               return_value=resp(200, _envelope(outer))) as get, \
             mock.patch.object(x_client.translator, "translate") as fb:
            result = _fetched("10", translate_to="fa")
        self.assertEqual(get.call_count, 1)
        fb.assert_not_called()
        self.assertIsNone(result.tweet.get("translation"))


if __name__ == "__main__":
    unittest.main()
