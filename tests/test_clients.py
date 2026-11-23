import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

import constants
import helpers
import x_client
from instagram_client import collect_media


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


class InstagramCollect(unittest.TestCase):
    def test_carousel_order_is_numeric_not_lexicographic(self):
        with tempfile.TemporaryDirectory() as tmp:
            for n in (1, 2, 3, 10, 11):
                (Path(tmp) / f"CODE_{n}.jpg").write_bytes(b"x")
            (Path(tmp) / "CODE_4.mp4").write_bytes(b"x")
            (Path(tmp) / "CODE.json").write_bytes(b"x")  # ignored
            items, oversized = collect_media(Path(tmp))
            names = [Path(i.source).name for i in items]
        self.assertEqual(oversized, 0)
        self.assertEqual(
            names,
            ["CODE_1.jpg", "CODE_2.jpg", "CODE_3.jpg", "CODE_4.mp4", "CODE_10.jpg", "CODE_11.jpg"],
        )

    def test_only_this_directory_is_read(self):
        # Previously glob("<shortcode>*") over a shared dir could pick up another post's files.
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            (Path(a) / "ABC.jpg").write_bytes(b"x")
            (Path(b) / "ABCdef.jpg").write_bytes(b"x")
            items, oversized = collect_media(Path(a))
            self.assertEqual(len(items), 1)
            self.assertEqual(oversized, 0)

    def test_webp_image_is_converted_and_collected(self):
        with tempfile.TemporaryDirectory() as tmp:
            webp = Path(tmp) / "CODE_1.webp"
            # generate a real webp with Pillow (no external binary needed)
            from PIL import Image
            Image.new("RGB", (10, 10), "blue").save(webp, "WEBP")
            items, oversized = collect_media(Path(tmp))
            self.assertEqual(oversized, 0)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].kind, "photo")
            self.assertTrue(items[0].source.endswith(".jpg"))
            self.assertFalse(webp.exists())
            self.assertTrue(Path(items[0].source).exists())

    def test_transparent_webp_converts_without_ffmpeg_on_path(self):
        import os
        from unittest import mock
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            webp = Path(tmp) / "CODE_1.webp"
            Image.new("RGBA", (10, 10), (255, 0, 0, 0)).save(webp, "WEBP")
            with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
                items, _ = collect_media(Path(tmp))
            self.assertEqual(len(items), 1)
            self.assertTrue(items[0].source.endswith(".jpg"))
            with Image.open(items[0].source) as im:
                self.assertEqual(im.format, "JPEG")
                self.assertEqual(im.getpixel((0, 0)), (255, 255, 255))

    def test_oversized_instagram_file_is_filtered_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "CODE_1.jpg").write_bytes(b"123")
            (Path(tmp) / "CODE_2.mp4").write_bytes(b"x" * 200)
            items, oversized = collect_media(Path(tmp), max_bytes=100)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].kind, "photo")
            self.assertEqual(oversized, 1)

    def test_downloaded_instagram_media_raises_when_all_oversized(self):
        import instagram_client

        def fake_dl(_code, workdir):
            workdir.mkdir(parents=True, exist_ok=True)
            (workdir / "BIG.mp4").write_bytes(b"x" * 200)
            return "cap"

        with mock.patch.object(instagram_client, "_download", side_effect=fake_dl), \
             mock.patch.object(
                 instagram_client,
                 "collect_media",
                 side_effect=lambda d: collect_media(d, max_bytes=100),
             ):
            with self.assertRaises(instagram_client.MediaFetchError) as cm:
                with instagram_client.downloaded_instagram_media("CODE"):
                    pass
        self.assertIn("۵۰ مگابایت", str(cm.exception))


class InstagramFailureDiagnosis(unittest.TestCase):
    def test_age_restricted_ruling_returns_18_plus_message(self):
        from instagram_client import _diagnose_metadata_failure

        ctx = mock.Mock(is_logged_in=False)
        ctx.doc_id_graphql_query.return_value = {
            "data": {
                "xig_polaris_media": {
                    "gating_ruling": {
                        "gating_type": 3,
                        "title": "Age-restricted content",
                        "description": "This content is age-restricted based on your age or account settings.",
                    }
                }
            }
        }
        msg = _diagnose_metadata_failure(ctx, "DfHKkl3h_7h")
        assert msg is not None
        self.assertIn("+۱۸", msg)
        self.assertIn("محدودیت سنی", msg)

    def test_missing_or_private_polaris_null_returns_not_found(self):
        from instagram_client import _diagnose_metadata_failure

        ctx = mock.Mock(is_logged_in=False)
        ctx.doc_id_graphql_query.return_value = {"data": {"xig_polaris_media": None}}
        msg = _diagnose_metadata_failure(ctx, "AAAAAAAAAAA")
        assert msg is not None
        self.assertIn("یافت نشد", msg)

    def test_long_private_shortcode_is_identified_without_network(self):
        from instagram_client import _diagnose_metadata_failure

        ctx = mock.Mock(is_logged_in=False)
        msg = _diagnose_metadata_failure(ctx, "A" * 39)
        assert msg is not None
        self.assertIn("خصوصی", msg)
        ctx.doc_id_graphql_query.assert_not_called()

    def test_other_gating_ruling_includes_title(self):
        from instagram_client import _diagnose_metadata_failure

        ctx = mock.Mock(is_logged_in=False)
        ctx.doc_id_graphql_query.return_value = {
            "data": {
                "xig_polaris_media": {
                    "gating_ruling": {
                        "gating_type": 2,
                        "title": "Restricted in your region",
                        "description": "Unavailable.",
                    }
                }
            }
        }
        msg = _diagnose_metadata_failure(ctx, "DfHKkl3h_7h")
        assert msg is not None
        self.assertIn("Restricted in your region", msg)

    def test_download_surfaces_age_restricted_error(self):
        import instaloader
        import instagram_client

        fake_loader = mock.Mock()
        fake_loader.context.is_logged_in = False
        fake_loader.context.doc_id_graphql_query.return_value = {
            "data": {
                "xig_polaris_media": {
                    "gating_ruling": {"title": "Age-restricted content", "description": "age"}
                }
            }
        }
        with mock.patch.object(instagram_client, "_get_loader", return_value=fake_loader), \
             mock.patch.object(
                 instaloader.Post,
                 "from_shortcode",
                 side_effect=instaloader.exceptions.BadResponseException("Fetching Post metadata failed."),
             ):
            with self.assertRaises(instagram_client.MediaFetchError) as cm:
                instagram_client._download("DfHKkl3h_7h", Path("/tmp/unused"))
        self.assertIn("محدودیت سنی", str(cm.exception))


class InstagramRulingFallback(unittest.TestCase):
    """Primary metadata query rate-limited (1675004) -> ruling-payload fallback."""

    def _fake_loader(self, ung):
        loader = mock.Mock()
        loader.context.is_logged_in = False
        loader.context.doc_id_graphql_query.return_value = {
            "data": {"xig_polaris_media": {
                "gating_ruling": None,
                "if_not_gated_logged_out": ung,
            }}
        }
        return loader

    @staticmethod
    def _photo_ung(caption="hello"):
        return {
            "media_type": 1,
            "caption": {"text": caption},
            "image_versions2": {"candidates": [
                {"url": "https://cdn/x.jpg"},
                {"url": "https://cdn/x_small.jpg"},
            ]},
        }

    @staticmethod
    def _write_jpg(ctx, url, base, cap):
        p = base.with_suffix(".jpg")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"jpegbytes")
        return p, False

    def _run_download(self, loader, shortcode, workdir):
        import instaloader
        import instagram_client
        with mock.patch.object(instagram_client, "_get_loader", return_value=loader), \
             mock.patch.object(
                 instaloader.Post, "from_shortcode",
                 side_effect=instaloader.exceptions.BadResponseException(
                     "Fetching Post metadata failed.")):
            return instagram_client._download(shortcode, workdir)

    def test_photo_fallback_delivers_caption_and_file(self):
        import instagram_client
        loader = self._fake_loader(self._photo_ung("caption text"))
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "w"
            with mock.patch.object(instagram_client, "_download_url",
                                   side_effect=self._write_jpg):
                caption = self._run_download(loader, "DfhzAHdomFE", workdir)
            self.assertEqual(caption, "caption text")
            self.assertTrue((workdir / "DfhzAHdomFE.jpg").exists())

    def test_reel_fallback_picks_largest_variant_under_cap(self):
        import instagram_client
        ung = {
            "media_type": 2,
            "caption": {"text": "reel"},
            "video_versions": [
                {"url": "https://cdn/v1.mp4"},
                {"url": "https://cdn/v2.mp4"},
            ],
        }
        loader = self._fake_loader(ung)
        sizes = {"https://cdn/v1.mp4": 100, "https://cdn/v2.mp4": 500}

        def fake_mp4(ctx, url, base, cap):
            p = base.with_suffix(".mp4")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"mp4bytes")
            return p, False

        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "w"
            with mock.patch.object(instagram_client, "_head_length",
                                   side_effect=lambda ctx, url: sizes.get(url)), \
                 mock.patch.object(instagram_client, "_download_url",
                                   side_effect=fake_mp4) as dl:
                caption = self._run_download(loader, "De-MryvTVWC", workdir)
            self.assertEqual(caption, "reel")
            self.assertEqual(dl.call_args[0][1], "https://cdn/v2.mp4")
            self.assertTrue((workdir / "De-MryvTVWC.mp4").exists())

    def test_carousel_fallback_writes_every_slide_in_order(self):
        import instagram_client
        slide = self._photo_ung()
        ung = {"media_type": 8, "caption": {"text": "album"},
               "carousel_media": [slide, dict(slide)]}
        loader = self._fake_loader(ung)
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "w"
            with mock.patch.object(instagram_client, "_download_url",
                                   side_effect=self._write_jpg):
                caption = self._run_download(loader, "Dfa_KJyl2Uw", workdir)
            self.assertEqual(caption, "album")
            self.assertTrue((workdir / "Dfa_KJyl2Uw_1.jpg").exists())
            self.assertTrue((workdir / "Dfa_KJyl2Uw_2.jpg").exists())

    def test_all_oversized_raises_50mb_message(self):
        import instagram_client
        loader = self._fake_loader(self._photo_ung())
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "w"
            with mock.patch.object(instagram_client, "_download_url",
                                   return_value=(None, True)):
                with self.assertRaises(instagram_client.MediaFetchError) as cm:
                    self._run_download(loader, "DfhzAHdomFE", workdir)
        self.assertIn("۵۰ مگابایت", str(cm.exception))

    def test_partial_oversized_appends_note_to_caption(self):
        import instagram_client
        photo = self._photo_ung()
        big_video = {"media_type": 2,
                     "video_versions": [{"url": "https://cdn/big.mp4"}]}
        ung = {"media_type": 8, "caption": {"text": "mixed"},
               "carousel_media": [photo, big_video]}
        loader = self._fake_loader(ung)
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "w"
            with mock.patch.object(instagram_client, "_head_length",
                                   return_value=10 ** 9), \
                 mock.patch.object(instagram_client, "_download_url",
                                   side_effect=self._write_jpg):
                caption = self._run_download(loader, "Dfa_KJyl2Uw", workdir)
        self.assertIn("mixed", caption)
        self.assertIn("1 فایل", caption)
        self.assertIn("۵۰ مگابایت", caption)

    def test_public_payload_without_media_gets_rate_limit_message(self):
        import instagram_client
        loader = self._fake_loader({"media_type": 1})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(instagram_client.MediaFetchError) as cm:
                self._run_download(loader, "DfhzAHdomFE", Path(tmp) / "w")
        self.assertIn("موقتاً", str(cm.exception))

    def test_primary_success_never_calls_ruling_query(self):
        import instaloader
        import instagram_client
        loader = mock.Mock()
        loader.context.is_logged_in = False
        post = mock.Mock()
        post.caption = "ok"
        with mock.patch.object(instagram_client, "_get_loader", return_value=loader), \
             mock.patch.object(instaloader.Post, "from_shortcode", return_value=post):
            caption = instagram_client._download("DfzVRIHoHS4", Path("/tmp/unused"))
        self.assertEqual(caption, "ok")
        loader.context.doc_id_graphql_query.assert_not_called()

    def test_download_url_uses_content_type_and_enforces_cap(self):
        import instagram_client
        ctx = mock.MagicMock()
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "image/jpeg"}
        resp.iter_content.return_value = [b"a" * 60, b"b" * 60]
        ctx._session.get.return_value.__enter__.return_value = resp
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "CODE"
            path, over = instagram_client._download_url(ctx, "https://cdn/x", base, 100)
            self.assertIsNone(path)
            self.assertTrue(over)
            self.assertFalse(base.with_suffix(".jpg").exists())
            resp.iter_content.return_value = [b"a" * 60]
            path, over = instagram_client._download_url(ctx, "https://cdn/x", base, 100)
            assert path is not None
            self.assertFalse(over)
            self.assertEqual(path.suffix, ".jpg")
            self.assertEqual(path.read_bytes(), b"a" * 60)

    def test_download_url_webp_kept_for_later_conversion(self):
        import instagram_client
        ctx = mock.MagicMock()
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "image/webp"}
        resp.iter_content.return_value = [b"riff"]
        ctx._session.get.return_value.__enter__.return_value = resp
        with tempfile.TemporaryDirectory() as tmp:
            path, over = instagram_client._download_url(
                ctx, "https://cdn/x", Path(tmp) / "CODE", 100)
            assert path is not None
            self.assertEqual(path.suffix, ".webp")
            self.assertFalse(over)

    def test_pick_video_url_all_over_cap_returns_none(self):
        import instagram_client
        ctx = mock.Mock()
        with mock.patch.object(instagram_client, "_head_length", return_value=10 ** 9):
            url = instagram_client._pick_video_url(
                ctx, [{"url": "https://cdn/a.mp4"}, {"url": "https://cdn/b.mp4"}], 100)
        self.assertIsNone(url)

    def test_pick_video_url_unknown_size_falls_back_to_first(self):
        import instagram_client
        ctx = mock.Mock()
        with mock.patch.object(instagram_client, "_head_length", return_value=None):
            url = instagram_client._pick_video_url(
                ctx, [{"url": "https://cdn/a.mp4"}, {"url": "https://cdn/b.mp4"}], 100)
        self.assertEqual(url, "https://cdn/a.mp4")


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


def _thread_item(item_id: str, text: str, *, lang: str = "fa", author: str = "a", **extra) -> dict:
    """One item of a /2/thread response: same shape as a v1 tweet plus ``type``."""
    return {
        "type": "status",
        "id": item_id,
        "text": text,
        "lang": lang,
        "url": f"https://x.com/{author}/status/{item_id}",
        "author": {"name": author.upper(), "screen_name": author, "url": f"https://x.com/{author}"},
        "media": extra.pop("media", {}),
        **extra,
    }


def _thread_envelope(items, focal="auto"):
    # focal: dict | None  ("auto" takes the first item, what the API really returns)
    """A /2/thread envelope: the focal status is the tweet that was asked for."""
    if focal == "auto":
        focal = items[0] if items else None
    return {"code": 200, "thread": items, "status": focal}


class ThreadFetch(unittest.TestCase):
    """GET /2/thread/{id} resolves any link inside a self-reply chain, root first.

    Measured live on 2026-11-09 with id 2119728555940843648: 7 items, root first,
    also from a mid-thread link; a plain tweet returns a one-item thread, which is
    what makes the probe safe to run on every tweet.
    """

    def setUp(self):
        # The thread knobs are read at call time from `constants`, so an operator
        # running the suite with THREAD_FETCH=off or a small THREAD_MAX_TWEETS in
        # the environment must not turn these tests red.
        for name, value in (
            ("THREAD_FETCH", "on"),
            ("THREAD_MAX_TWEETS", 20),  # the shipped default, whatever the env says
            ("THREAD_TRANSLATE_REFETCH_TRIES", 1),
        ):
            patcher = mock.patch.object(constants, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _thread(self, tweet_id, translate_to=None, **kw):
        return x_client.get_thread_result(tweet_id, translate_to=translate_to, **kw)

    def test_chain_returns_every_item_root_first_with_media(self):
        video = {
            "duration": 100.0,
            "url": "https://v/hq.mp4",
            "formats": [
                {"url": "https://v/lo.mp4", "bitrate": 200_000, "container": "mp4"},
                {"url": "https://v/hq.mp4", "bitrate": 25_000_000, "container": "mp4"},
            ],
        }
        items = [
            _thread_item("10", "اول", media={"photos": [{"url": "https://p/1.jpg"}]}),
            _thread_item("11", "دوم", replying_to="a", media={"videos": [video]}),
            _thread_item("12", "سوم", replying_to="a"),
        ]
        with mock.patch.object(
            x_client._session, "get",
            return_value=resp(200, _thread_envelope(items, focal=items[1])),
        ) as get, mock.patch.object(x_client.translator, "translate") as fb:
            thread = self._thread("11")
        assert thread is not None
        self.assertEqual(get.call_args.args[0], "https://api.fxtwitter.com/2/thread/11")
        # A mid-thread link: items still come back from the root, and the tweet the
        # user actually sent is known separately, so the renderer can mark it.
        self.assertEqual([i.item_id for i in thread.items], ["10", "11", "12"])
        self.assertEqual(thread.requested_id, "11")
        self.assertEqual(thread.root.item_id, "10")
        self.assertEqual(thread.total, 3)
        self.assertEqual(thread.truncated, 0)
        # The 50 MB picker runs on thread items exactly as on a quoted post.
        self.assertEqual(thread.items[0].payload.photos, ["https://p/1.jpg"])
        self.assertEqual(thread.items[1].payload.videos, ["https://v/lo.mp4"])
        self.assertEqual(
            [(i.kind, i.source) for i in thread.media_items()],
            [("photo", "https://p/1.jpg"), ("video", "https://v/lo.mp4")],
        )
        fb.assert_not_called()  # source is already the target language

    def test_plain_tweet_is_no_thread(self):
        items = [_thread_item("10", "تک توییت")]
        with mock.patch.object(
            x_client._session, "get", return_value=resp(200, _thread_envelope(items))
        ):
            self.assertIsNone(self._thread("10"))

    def test_deleted_item_stays_a_labeled_hole(self):
        items = [
            _thread_item("10", "اول"),
            {"type": "tombstone", "provider": "twitter", "reason": "deleted"},
            _thread_item("12", "سوم"),
        ]
        with mock.patch.object(
            x_client._session, "get", return_value=resp(200, _thread_envelope(items))
        ):
            thread = self._thread("10")
        assert thread is not None
        self.assertEqual([i.is_unavailable for i in thread.items], [False, True, False])
        self.assertEqual(thread.items[1].unavailable, "deleted")
        self.assertEqual(thread.total, 3)

    def test_cap_keeps_the_first_dropped_item_for_the_continuation_link(self):
        items = [_thread_item(str(i), f"توییت {i}") for i in range(1, 6)]
        with mock.patch.object(
            x_client._session, "get", return_value=resp(200, _thread_envelope(items))
        ):
            thread = self._thread("1", max_items=3)
        assert thread is not None
        self.assertEqual([i.item_id for i in thread.items], ["1", "2", "3"])
        self.assertEqual((thread.total, thread.truncated), (5, 2))
        # only the FIRST dropped item is kept: that is where "ادامه" must open
        self.assertEqual([i.item_id for i in thread.dropped], ["4"])
        self.assertEqual(thread.dropped[0].author_screen_name, "a")

    def test_time_budget_cuts_the_thread_and_still_announces_the_rest(self):
        """A slow upstream must not turn a thread into an open-ended hang."""
        import time as time_module

        items = [_thread_item(str(i), f"توییت {i}") for i in range(1, 6)]
        # A clock that spends 10s per item: with a 25s budget two items fit and the
        # third is exactly where the delivery gets cut.
        readings = {"n": 0}

        def fake_monotonic():
            readings["n"] += 1
            return 10.0 * readings["n"]

        with mock.patch.object(
            x_client._session, "get", return_value=resp(200, _thread_envelope(items))
        ), mock.patch.object(constants, "THREAD_TIME_BUDGET_S", 25.0), mock.patch.object(
            x_client, "THREAD_TIME_BUDGET_S", 25.0
        ), mock.patch.object(
            time_module, "monotonic", fake_monotonic
        ):
            thread = self._thread("1")
        assert thread is not None
        self.assertEqual([i.item_id for i in thread.items], ["1", "2"])
        self.assertEqual((thread.total, thread.truncated), (5, 3))
        self.assertEqual([i.item_id for i in thread.dropped], ["3"])
        # the reader is told how much is missing and where to pick it up
        text = helpers.build_thread_text(thread)
        assert text is not None
        self.assertIn("۳ توییت دیگر", text)
        self.assertIn("https://x.com/a/status/3", text)
        self.assertIn("🧵 ترد ۲ از ۵ توییت", text)

    def test_items_in_the_response_are_used_without_a_second_call(self):
        items = [
            _thread_item("10", "hello", lang="en", translation={"text": "سلام", "provider": "bing"}),
            _thread_item("11", "world", lang="en", translation={"text": "دنیا", "provider": "bing"}),
        ]
        with mock.patch.object(
            x_client._session, "get", return_value=resp(200, _thread_envelope(items))
        ) as get, mock.patch.object(x_client.translator, "translate") as fb:
            thread = self._thread("10", "fa")
        assert thread is not None
        self.assertEqual(get.call_count, 1)
        fb.assert_not_called()
        self.assertEqual(
            [i.payload.tweet["translation"]["text"] for i in thread.items], ["سلام", "دنیا"]
        )

    def test_echo_is_scrubbed_and_the_item_is_recovered(self):
        """X can answer ``translation.text`` == the source; that is not a translation."""
        items = [
            _thread_item("10", "hello", lang="en", translation={"text": "hello", "provider": "grok"}),
            _thread_item("11", "world", lang="en"),
        ]
        recovered = [
            _envelope(_tweet(id="10", text="hello", lang="en", translation={"text": "سلام"})),
            _envelope(_tweet(id="11", text="world", lang="en", translation={"text": "دنیا"})),
        ]
        with mock.patch.object(
            x_client._session, "get",
            side_effect=[resp(200, _thread_envelope(items)), resp(200, recovered[0]),
                         resp(200, recovered[1])],
        ) as get, mock.patch.object(x_client.translator, "translate") as fb, \
             mock.patch.object(x_client.time, "sleep"):
            thread = self._thread("10", "fa")
        assert thread is not None
        urls = [c.args[0] for c in get.call_args_list]
        # The echoed item is scrubbed BEFORE the retry, so a "translation" that is
        # just the source text cannot make the recovery logic stand down.
        self.assertEqual(urls[1], "https://api.fxtwitter.com/i/status/10/fa")
        self.assertEqual(
            [i.payload.tweet["translation"]["text"] for i in thread.items], ["سلام", "دنیا"]
        )
        fb.assert_not_called()

    def test_untranslated_item_falls_back_per_item(self):
        items = [
            _thread_item("10", "hello", lang="en"),
            _thread_item("11", "world", lang="en"),
        ]
        answered = {
            "10": _envelope(_tweet(id="10", text="hello", lang="en", translation={"text": "سلام"})),
            "11": _envelope(_tweet(id="11", text="world", lang="en", translation={"text": "دنیا"})),
        }
        with mock.patch.object(
            x_client._session, "get",
            side_effect=[
                resp(200, _thread_envelope(items)),
                resp(200, answered["10"]),
                resp(200, answered["11"]),
            ],
        ) as get, mock.patch.object(x_client.translator, "translate") as fb, \
             mock.patch.object(x_client.time, "sleep"):
            thread = self._thread("10", "fa")
        assert thread is not None
        urls = [c.args[0] for c in get.call_args_list]
        self.assertEqual(urls[0], "https://api.fxtwitter.com/2/thread/10?lang=fa")
        # Each missing item is recovered by ITS OWN translate endpoint. Refetching
        # the thread per item would retranslate the whole chain to fix one line.
        self.assertEqual(urls[1], "https://api.fxtwitter.com/i/status/10/fa")
        self.assertEqual(urls[2], "https://api.fxtwitter.com/i/status/11/fa")
        self.assertEqual(
            [i.payload.tweet["translation"]["text"] for i in thread.items], ["سلام", "دنیا"]
        )
        fb.assert_not_called()

    def test_focal_link_outside_the_cap_is_still_reported(self):
        items = [_thread_item(str(i), f"توییت {i}") for i in range(1, 6)]
        with mock.patch.object(
            x_client._session, "get",
            return_value=resp(200, _thread_envelope(items, focal=items[4])),
        ):
            thread = self._thread("5", max_items=2)
        assert thread is not None
        self.assertEqual([i.item_id for i in thread.items], ["1", "2"])
        self.assertEqual(thread.requested_id, "5")
        self.assertEqual(thread.focal.tweet["text"], "توییت 5")

    def test_focal_quote_of_a_thread_item_is_parsed_once(self):
        items = [
            _thread_item("10", "اول"),
            _thread_item("11", "دوم", quote=_thread_item("99", "نقل‌قول", media={"photos": [{"url": "https://q/1.jpg"}]})),
        ]
        with mock.patch.object(
            x_client._session, "get", return_value=resp(200, _thread_envelope(items))
        ):
            thread = self._thread("10")
        assert thread is not None
        quote = thread.items[1].payload.quote
        assert quote is not None
        self.assertEqual(quote.tweet["id"], "99")
        self.assertIsNone(quote.quote)  # depth stays capped at 1

    def test_self_quote_back_to_the_link_is_dropped(self):
        items = [
            _thread_item("10", "اول"),
            _thread_item("11", "دوم", quote=_thread_item("11", "دوم")),
        ]
        with mock.patch.object(
            x_client._session, "get", return_value=resp(200, _thread_envelope(items))
        ):
            thread = self._thread("10")
        assert thread is not None
        self.assertIsNone(thread.items[1].payload.quote)

    def test_kill_switch_restores_the_single_tweet_path(self):
        with mock.patch.object(constants, "THREAD_FETCH", "off"), \
             mock.patch.object(x_client._session, "get") as get:
            self.assertIsNone(self._thread("10"))
        get.assert_not_called()

    def test_probe_failures_never_invent_a_thread(self):
        for response in (resp(404), resp(200, {"code": 404, "message": "nope"}), resp(200, {})):
            with mock.patch.object(x_client._session, "get", return_value=response):
                self.assertIsNone(self._thread("10"))
        with mock.patch.object(
            x_client._session, "get", side_effect=requests.RequestException("down")
        ):
            self.assertIsNone(self._thread("10"))

    def test_language_is_requested_once_on_the_thread_call(self):
        items = [
            _thread_item("10", "hello", lang="en", translation={"text": "سلام"}),
            _thread_item("11", "world", lang="en", translation={"text": "دنیا"}),
        ]
        with mock.patch.object(
            x_client._session, "get", return_value=resp(200, _thread_envelope(items))
        ) as get, mock.patch.object(x_client.translator, "translate") as fb:
            self._thread("10", "fa")
        self.assertEqual(get.call_args_list[0].args[0], "https://api.fxtwitter.com/2/thread/10?lang=fa")
        # One call total: the response carried every item, so nothing is refetched.
        self.assertEqual(get.call_count, 1)
        fb.assert_not_called()


class TranslationFallback(unittest.TestCase):
    """The measured gap: fxtwitter returns nothing for certain tweet/language pairs."""

    def test_note_tweet_retry_recovers_a_transient_gap(self):
        """The 2026-11-09 live case (id 2119479031611507061 -> fa): empty on the
        first lookup, translated on the next. Notes must NOT skip the re-fetch."""
        note = _tweet(id="9", text="x" * 1000, lang="en", is_note_tweet=True)
        answered = dict(note, translation={"text": "متن فارسی", "provider": "grok"})
        with mock.patch.object(
            x_client._session, "get",
            side_effect=[resp(200, _envelope(note)), resp(200, _envelope(answered))],
        ) as get, \
             mock.patch.object(x_client.translator, "translate",
                               return_value="این نباید صدا زده شود") as fb, \
             mock.patch.object(x_client.time, "sleep") as sleep:
            result = _fetched("9", translate_to="fa")
        self.assertEqual(get.call_count, 2)
        fb.assert_not_called()
        self.assertEqual(result.tweet["translation"]["text"], "متن فارسی")
        self.assertEqual(result.tweet["translation"]["provider"], "grok")
        sleep.assert_not_called()  # the first re-fetch is immediate

    def test_note_tweet_exhausts_its_retries_then_uses_the_fallback(self):
        """A deterministic gap still reaches the keyless fallback, after the
        extra attempts and the backoff between them."""
        note = _tweet(id="9", text="x" * 1000, lang="en", is_note_tweet=True)
        with mock.patch.object(
            x_client._session, "get", return_value=resp(200, _envelope(note))
        ) as get, \
             mock.patch.object(x_client.translator, "translate",
                               return_value="متن فارسی") as fb, \
             mock.patch.object(x_client.time, "sleep") as sleep:
            result = _fetched("9", translate_to="fa")
        self.assertEqual(get.call_count, 1 + x_client.TRANSLATE_REFETCH_TRIES_NOTE)
        fb.assert_called_once()
        self.assertEqual(result.tweet["translation"]["provider"], "google-fallback")
        self.assertEqual(
            [c.args[0] for c in sleep.call_args_list],
            [
                x_client.TRANSLATE_REFETCH_BACKOFF_S * (2 ** i)
                for i in range(x_client.TRANSLATE_REFETCH_TRIES_NOTE - 1)
            ],
        )

    def test_short_tweet_still_gets_exactly_one_retry(self):
        """Unchanged behaviour for the path that already had a re-fetch."""
        outer = _tweet(id="5", text="hello", lang="en")
        with mock.patch.object(
            x_client._session, "get",
            side_effect=[resp(200, _envelope(outer))] * 3,
        ) as get, \
             mock.patch.object(x_client.translator, "translate", return_value="سلام"), \
             mock.patch.object(x_client.time, "sleep"):
            _fetched("5", translate_to="fa")
        self.assertEqual(get.call_count, 1 + x_client.TRANSLATE_REFETCH_TRIES)

    def test_retries_can_be_switched_off_without_losing_the_fallback(self):
        """TRANSLATE_REFETCH_TRIES=0 restores the old one-call behaviour."""
        note = _tweet(id="9", text="x" * 1000, lang="en", is_note_tweet=True)
        with mock.patch.object(
            x_client._session, "get", return_value=resp(200, _envelope(note))
        ) as get, \
             mock.patch.object(x_client.translator, "translate", return_value="متن فارسی"), \
             mock.patch.object(x_client, "TRANSLATE_REFETCH_TRIES_NOTE", 0):
            result = _fetched("9", translate_to="fa")
        self.assertEqual(get.call_count, 1)
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
             mock.patch.object(x_client.translator, "translate", return_value=None), \
             mock.patch.object(x_client.time, "sleep"):
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
