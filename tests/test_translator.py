import unittest
from unittest import mock

import requests

import translator


def gtx_response(*segments):
    """Shape of the keyless Google web endpoint: [[translated, original, ...], ...]."""
    r = mock.Mock()
    r.status_code = 200
    r.json.return_value = [
        [[seg, "", None, None] for seg in segments],
        None,
        "en",
    ]
    return r


class Chunks(unittest.TestCase):
    def test_keeps_lines_whole_under_the_limit(self):
        self.assertEqual(translator._chunks("a b\nc d\n", 100), ["a b\nc d\n"])

    def test_splits_on_line_boundaries(self):
        text = "\n".join(["x" * 40] * 10)
        pieces = translator._chunks(text, 100)
        self.assertTrue(pieces)
        self.assertTrue(all(len(p) <= 100 for p in pieces))
        self.assertEqual("".join(pieces), text)  # nothing lost, nothing reordered

    def test_hard_cuts_a_pathological_single_line(self):
        text = "word " * 100
        pieces = translator._chunks(text, 60)
        self.assertTrue(all(len(p) <= 60 for p in pieces))
        self.assertEqual("".join(pieces), text)

    def test_empty(self):
        self.assertEqual(translator._chunks("", 100), [])


class Translate(unittest.TestCase):
    def test_joins_segments(self):
        with mock.patch.object(translator._session, "get", return_value=gtx_response("سلام ", "دنیا")) as get:
            self.assertEqual(translator.translate("hello world", "fa"), "سلام دنیا")
        self.assertEqual(get.call_args.kwargs["params"]["tl"], "fa")
        self.assertEqual(get.call_args.kwargs["params"]["client"], "gtx")

    def test_long_text_is_sent_in_several_requests(self):
        text = "\n".join(["y" * 400] * 40)  # ~16k chars, far over the chunk limit
        responses = [gtx_response("ز") for _ in range(6)]
        with mock.patch.object(translator._session, "get", side_effect=responses) as get:
            out = translator.translate(text, "fa")
        self.assertGreater(get.call_count, 1)
        self.assertEqual(out, "ز" * get.call_count)

    def test_non_200_is_none_not_an_error(self):
        r = mock.Mock(status_code=429)
        with mock.patch.object(translator._session, "get", return_value=r):
            self.assertIsNone(translator.translate("hello", "fa"))

    def test_network_error_is_none(self):
        with mock.patch.object(translator._session, "get", side_effect=requests.RequestException("x")):
            self.assertIsNone(translator.translate("hello", "fa"))

    def test_html_body_is_none(self):
        r = mock.Mock(status_code=200)
        r.json.side_effect = ValueError("no json")
        with mock.patch.object(translator._session, "get", return_value=r):
            self.assertIsNone(translator.translate("hello", "fa"))

    def test_garbage_payload_is_none(self):
        r = mock.Mock(status_code=200)
        r.json.return_value = [None]
        with mock.patch.object(translator._session, "get", return_value=r):
            self.assertIsNone(translator.translate("hello", "fa"))

    def test_echo_counts_as_failure(self):
        """An untranslated echo must not be presented as a Persian translation."""
        with mock.patch.object(translator._session, "get", return_value=gtx_response("hello")):
            self.assertIsNone(translator.translate("hello", "fa"))

    def test_empty_input(self):
        self.assertIsNone(translator.translate("", "fa"))
        self.assertIsNone(translator.translate("hi", ""))


if __name__ == "__main__":
    unittest.main()
