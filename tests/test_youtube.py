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


if __name__ == "__main__":
    unittest.main()
