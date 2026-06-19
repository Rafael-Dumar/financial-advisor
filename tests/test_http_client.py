import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from advisor.http_client import fetch_json


class HttpClientTests(unittest.TestCase):
    def test_fetch_json_converts_http_error_to_clean_runtime_error(self):
        error = HTTPError("https://example.test", 400, "Bad Request", hdrs=None, fp=None)

        with patch("advisor.http_client.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "http_error:400"):
                fetch_json("https://example.test")

    def test_fetch_json_includes_short_http_error_body_when_available(self):
        error = HTTPError(
            "https://example.test",
            402,
            "Payment Required",
            hdrs=None,
            fp=BytesIO(b'{"Error Message":"Plan limit reached"}'),
        )

        with patch("advisor.http_client.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "http_error:402:.*Plan limit reached"):
                fetch_json("https://example.test")

    def test_fetch_json_converts_url_error_to_clean_runtime_error(self):
        with patch("advisor.http_client.urlopen", side_effect=URLError("offline")):
            with self.assertRaisesRegex(RuntimeError, "network_error:offline"):
                fetch_json("https://example.test")

    def test_fetch_json_passes_ssl_context_to_urlopen(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"ok": true}'

        context = object()
        with (
            patch("advisor.http_client._ssl_context", return_value=context),
            patch("advisor.http_client.urlopen", return_value=FakeResponse()) as urlopen,
        ):
            payload = fetch_json("https://example.test")

        self.assertEqual(payload, {"ok": True})
        self.assertIs(urlopen.call_args.kwargs["context"], context)

    def test_fetch_json_uses_windows_tls_fallback_for_certificate_verify_errors(self):
        with (
            patch("advisor.http_client.sys.platform", "win32"),
            patch("advisor.http_client.urlopen", side_effect=URLError("[SSL: CERTIFICATE_VERIFY_FAILED] expired")),
            patch("advisor.http_client._fetch_json_via_powershell", return_value={"ok": True}) as fallback,
        ):
            payload = fetch_json("https://example.test", headers={"x-test": "1"})

        self.assertEqual(payload, {"ok": True})
        fallback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
