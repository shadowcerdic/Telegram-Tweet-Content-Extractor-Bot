import unittest
from helpers import extract_youtube_ids


class YouTubeExtractionTests(unittest.TestCase):
    def test_standard_watch_link(self):
        text = "Check this out https://www.youtube.com/watch?v=dQw4w9WgXcQ it is great"
        self.assertEqual(extract_youtube_ids(text), ["dQw4w9WgXcQ"])

    def test_short_link(self):
        text = "Listen https://youtu.be/kJQP7kiw5Fk here"
        self.assertEqual(extract_youtube_ids(text), ["kJQP7kiw5Fk"])

    def test_shorts_link(self):
        text = "Shorts: https://youtube.com/shorts/dQw4w9WgXcQ"
        self.assertEqual(extract_youtube_ids(text), ["dQw4w9WgXcQ"])

    def test_duplicate_links_deduped(self):
        text = "https://youtu.be/dQw4w9WgXcQ and https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        self.assertEqual(extract_youtube_ids(text), ["dQw4w9WgXcQ"])

    def test_no_link(self):
        self.assertEqual(extract_youtube_ids("No youtube here"), [])


class YouTubeFriendlyErrorTests(unittest.TestCase):
    def test_maps_age_live_private_and_generic_errors(self):
        from youtube_client import _friendly_youtube_error

        self.assertIn("+۱۸", _friendly_youtube_error("ERROR: Sign in to confirm your age"))
        self.assertIn("پخش زنده", _friendly_youtube_error("ERROR: This live event will begin in 2 hours"))
        self.assertIn("خصوصی", _friendly_youtube_error("ERROR: Private video. Sign in if you've been granted access"))
        self.assertEqual(
            _friendly_youtube_error("ERROR: some internal extractor stack trace"),
            "دریافت این ویدیو از یوتیوب ممکن نشد.",
        )


if __name__ == "__main__":
    unittest.main()
