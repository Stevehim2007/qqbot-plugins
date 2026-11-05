from __future__ import annotations

import socket
import unittest
from unittest.mock import patch

from _support import install_astrbot_stub

install_astrbot_stub()

import web_fetch


class UrlValidationTests(unittest.TestCase):
    def test_accepts_public_https(self):
        self.assertEqual(
            web_fetch.validate_fetch_url("https://example.com/a?b=1#frag"),
            "https://example.com/a?b=1",
        )

    def test_rejects_http_by_default(self):
        with self.assertRaises(web_fetch.WebFetchError) as ctx:
            web_fetch.validate_fetch_url("http://example.com/")
        self.assertEqual(ctx.exception.reason, "invalid_url")

    def test_accepts_http_when_explicitly_allowed(self):
        self.assertEqual(
            web_fetch.validate_fetch_url("http://example.com/", allow_http=True),
            "http://example.com/",
        )

    def test_rejects_credentials(self):
        for url in (
            "https://user@example.com/",
            "https://user:pass@example.com/",
            "https://@example.com/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(web_fetch.WebFetchError):
                    web_fetch.validate_fetch_url(url)

    def test_rejects_unsupported_schemes(self):
        for url in ("ftp://example.com/", "file:///etc/passwd", "javascript:alert(1)"):
            with self.subTest(url=url):
                with self.assertRaises(web_fetch.WebFetchError):
                    web_fetch.validate_fetch_url(url)

    def test_rejects_missing_host_and_bad_ports(self):
        for url in ("https:///path", "https://example.com:0/", "https://example.com:99999/"):
            with self.subTest(url=url):
                with self.assertRaises(web_fetch.WebFetchError):
                    web_fetch.validate_fetch_url(url)

    def test_rejects_localhost_and_internal_names(self):
        for host in (
            "localhost",
            "metadata.google.internal",
            "printer.local",
            "foo.internal",
            "service.lan",
            "host.home.arpa",
        ):
            with self.subTest(host=host):
                with self.assertRaises(web_fetch.WebFetchError) as ctx:
                    web_fetch.validate_fetch_url(f"https://{host}/")
                self.assertEqual(ctx.exception.reason, "blocked_host")

    def test_rejects_private_loopback_linklocal_reserved_ips(self):
        for host in (
            "127.0.0.1",
            "10.1.2.3",
            "192.168.1.1",
            "172.16.0.1",
            "169.254.169.254",
            "0.0.0.0",
            "224.0.0.1",
            "240.0.0.1",
            "::1",
            "fe80::1",
            "fc00::1",
            "::ffff:127.0.0.1",
        ):
            rendered = f"[{host}]" if ":" in host else host
            with self.subTest(host=host):
                with self.assertRaises(web_fetch.WebFetchError) as ctx:
                    web_fetch.validate_fetch_url(f"https://{rendered}/")
                self.assertEqual(ctx.exception.reason, "blocked_ip")

    def test_accepts_public_ipv4_and_ipv6(self):
        self.assertEqual(
            web_fetch.validate_fetch_url("https://93.184.216.34/x"),
            "https://93.184.216.34/x",
        )
        self.assertEqual(
            web_fetch.validate_fetch_url("https://[2606:4700:4700::1111]/"),
            "https://[2606:4700:4700::1111]/",
        )

    def test_idna_host_is_normalized(self):
        self.assertEqual(
            web_fetch.validate_fetch_url("https://münich.example/"),
            "https://xn--mnich-kva.example/",
        )

    def test_rejects_control_chars_and_backslashes(self):
        for url in ("https://example.com/\x00", "https://example.com\\@evil.test/"):
            with self.subTest(url=url):
                with self.assertRaises(web_fetch.WebFetchError):
                    web_fetch.validate_fetch_url(url)

    def test_rejects_overlong_url(self):
        with self.assertRaises(web_fetch.WebFetchError):
            web_fetch.validate_fetch_url("https://example.com/" + "a" * 3000)

    def test_is_public_ip_handles_mapped_and_invalid_values(self):
        self.assertTrue(web_fetch.is_public_ip("8.8.8.8"))
        self.assertTrue(web_fetch.is_public_ip("2606:4700:4700::1111"))
        self.assertFalse(web_fetch.is_public_ip("127.0.0.1"))
        self.assertFalse(web_fetch.is_public_ip("::ffff:10.0.0.1"))
        self.assertFalse(web_fetch.is_public_ip("not-an-ip"))


class DnsTests(unittest.TestCase):
    def _infos(self, *addresses):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
            for address in addresses
        ]

    def test_resolves_all_public_answers(self):
        with patch("web_fetch.socket.getaddrinfo", return_value=self._infos("8.8.8.8", "1.1.1.1")):
            self.assertEqual(
                web_fetch.resolve_public_addresses("example.com", 443),
                ["8.8.8.8", "1.1.1.1"],
            )

    def test_mixed_public_and_private_answer_is_rejected(self):
        with patch(
            "web_fetch.socket.getaddrinfo",
            return_value=self._infos("8.8.8.8", "192.168.0.5"),
        ):
            with self.assertRaises(web_fetch.WebFetchError) as ctx:
                web_fetch.resolve_public_addresses("example.com", 443)
            self.assertEqual(ctx.exception.reason, "blocked_ip")

    def test_empty_dns_answer_is_rejected(self):
        with patch("web_fetch.socket.getaddrinfo", return_value=[]):
            with self.assertRaises(web_fetch.WebFetchError) as ctx:
                web_fetch.resolve_public_addresses("example.com", 443)
            self.assertEqual(ctx.exception.reason, "dns_failed")

    def test_dns_error_is_wrapped(self):
        with patch("web_fetch.socket.getaddrinfo", side_effect=socket.gaierror("nope")):
            with self.assertRaises(web_fetch.WebFetchError) as ctx:
                web_fetch.resolve_public_addresses("example.com", 443)
            self.assertEqual(ctx.exception.reason, "dns_failed")

    def test_duplicate_addresses_are_removed(self):
        with patch(
            "web_fetch.socket.getaddrinfo",
            return_value=self._infos("8.8.8.8", "8.8.8.8"),
        ):
            self.assertEqual(web_fetch.resolve_public_addresses("example.com", 443), ["8.8.8.8"])


class _FakeResponse:
    def __init__(self, status=200, headers=None, body=b"ok"):
        self.status = status
        self.headers = headers or {}
        self._body = body

    def read(self, amount):
        return self._body[:amount]


class _FakeConnection:
    last_init = None

    def __init__(self, host, port, *, pinned_ip, timeout):
        self.host = host
        self.port = port
        self.pinned_ip = pinned_ip
        self.timeout = timeout
        self.closed = False
        _FakeConnection.last_init = self

    def request(self, method, path, headers=None):
        self.request_data = (method, path, dict(headers or {}))

    def getresponse(self):
        return _FakeConnection.response

    def close(self):
        self.closed = True


class RequestOnceTests(unittest.TestCase):
    def _run(self, response, *, max_bytes=1024):
        _FakeConnection.response = response
        with patch("web_fetch.resolve_public_addresses", return_value=["93.184.216.34"]), patch(
            "web_fetch._PinnedHTTPSConnection", _FakeConnection
        ):
            return web_fetch._request_once(
                "https://example.com/path?q=1",
                timeout=3,
                max_bytes=max_bytes,
                user_agent="test-agent",
            )

    def test_connection_is_pinned_to_validated_ip(self):
        self._run(_FakeResponse(headers={"Content-Type": "text/html"}))
        self.assertEqual(_FakeConnection.last_init.pinned_ip, "93.184.216.34")
        self.assertEqual(_FakeConnection.last_init.host, "example.com")

    def test_returns_redirect_without_reading_body(self):
        result = self._run(
            _FakeResponse(status=302, headers={"Location": "/next"}, body=b"ignored")
        )
        self.assertEqual(result.status, 302)
        self.assertEqual(result.location, "/next")

    def test_rejects_missing_or_bad_content_type(self):
        for headers in ({}, {"Content-Type": "image/png"}):
            with self.subTest(headers=headers):
                with self.assertRaises(web_fetch.WebFetchError) as ctx:
                    self._run(_FakeResponse(headers=headers))
                self.assertEqual(ctx.exception.reason, "bad_content_type")

    def test_rejects_compressed_encoding(self):
        with self.assertRaises(web_fetch.WebFetchError) as ctx:
            self._run(
                _FakeResponse(
                    headers={"Content-Type": "text/html", "Content-Encoding": "gzip"}
                )
            )
        self.assertEqual(ctx.exception.reason, "bad_encoding")

    def test_rejects_oversized_content_length(self):
        with self.assertRaises(web_fetch.WebFetchError) as ctx:
            self._run(
                _FakeResponse(
                    headers={"Content-Type": "text/html", "Content-Length": "9999"},
                    body=b"<html></html>",
                ),
                max_bytes=100,
            )
        self.assertEqual(ctx.exception.reason, "too_large")

    def test_rejects_oversized_stream(self):
        with self.assertRaises(web_fetch.WebFetchError) as ctx:
            self._run(
                _FakeResponse(headers={"Content-Type": "text/plain"}, body=b"x" * 101),
                max_bytes=100,
            )
        self.assertEqual(ctx.exception.reason, "too_large")

    def test_timeout_is_mapped(self):
        with patch("web_fetch.resolve_public_addresses", return_value=["93.184.216.34"]), patch(
            "web_fetch._PinnedHTTPSConnection", _FakeConnection
        ):
            _FakeConnection.response = _FakeResponse()
            with patch.object(_FakeConnection, "request", side_effect=socket.timeout("slow")):
                with self.assertRaises(web_fetch.WebFetchError) as ctx:
                    web_fetch._request_once("https://example.com/", timeout=1, max_bytes=10)
            self.assertEqual(ctx.exception.reason, "timeout")


class FetchFlowTests(unittest.TestCase):
    def test_follows_relative_redirect_and_revalidates(self):
        hops = [
            web_fetch.HopResult(302, "/next", "", b""),
            web_fetch.HopResult(200, "", "text/html; charset=utf-8", b"<title>T</title><p>Hello</p>"),
        ]
        with patch("web_fetch._request_once", side_effect=hops) as request:
            page = web_fetch.fetch_page_text("https://example.com/start")
        self.assertEqual(page.url, "https://example.com/next")
        self.assertEqual(page.title, "T")
        self.assertEqual(page.text, "Hello")
        self.assertEqual(request.call_count, 2)

    def test_redirect_to_http_is_rejected_when_https_only(self):
        with patch(
            "web_fetch._request_once",
            return_value=web_fetch.HopResult(302, "http://example.com/next", "", b""),
        ):
            with self.assertRaises(web_fetch.WebFetchError) as ctx:
                web_fetch.fetch_page_text("https://example.com/start")
        self.assertEqual(ctx.exception.reason, "bad_redirect")

    def test_redirect_to_private_ip_is_rejected(self):
        with patch(
            "web_fetch._request_once",
            return_value=web_fetch.HopResult(302, "https://127.0.0.1/", "", b""),
        ):
            with self.assertRaises(web_fetch.WebFetchError) as ctx:
                web_fetch.fetch_page_text("https://example.com/start")
        self.assertEqual(ctx.exception.reason, "bad_redirect")

    def test_missing_location_is_bad_redirect(self):
        with patch(
            "web_fetch._request_once",
            return_value=web_fetch.HopResult(302, "", "", b""),
        ):
            with self.assertRaises(web_fetch.WebFetchError) as ctx:
                web_fetch.fetch_page_text("https://example.com/start")
        self.assertEqual(ctx.exception.reason, "bad_redirect")

    def test_too_many_redirects(self):
        with patch(
            "web_fetch._request_once",
            return_value=web_fetch.HopResult(302, "/again", "", b""),
        ):
            with self.assertRaises(web_fetch.WebFetchError) as ctx:
                web_fetch.fetch_page_text("https://example.com/start", max_redirects=2)
        self.assertEqual(ctx.exception.reason, "too_many_redirects")

    def test_non_200_is_http_error(self):
        with patch(
            "web_fetch._request_once",
            return_value=web_fetch.HopResult(404, "", "", b""),
        ):
            with self.assertRaises(web_fetch.WebFetchError) as ctx:
                web_fetch.fetch_page_text("https://example.com/")
        self.assertEqual(ctx.exception.reason, "http_error")

    def test_http_allowed_explicitly(self):
        with patch(
            "web_fetch._request_once",
            return_value=web_fetch.HopResult(200, "", "text/plain", b"hello"),
        ):
            page = web_fetch.fetch_page_text(
                "http://example.com/", allow_http=True
            )
        self.assertEqual(page.text, "hello")

    def test_utf8_charset_header_is_honoured(self):
        page = web_fetch.build_page_result(
            "https://example.com/",
            "你好世界",
            "text/plain; charset=utf-8",
            max_chars=100,
        )
        self.assertEqual(page.text, "你好世界")

    def test_meta_charset_is_honoured(self):
        raw = b'<meta charset="gbk"><p>\xc4\xe3\xba\xc3</p>'
        text = web_fetch.decode_body(raw, "text/html")
        self.assertIn("你好", text)

    def test_unknown_charset_falls_back_to_utf8(self):
        self.assertEqual(web_fetch.decode_body("你好".encode(), "text/plain; charset=nope"), "你好")


class FormattingTests(unittest.TestCase):
    def test_html_to_text_removes_scripts_and_block_markup(self):
        title, body = web_fetch.html_to_text(
            "<html><head><title>My Title</title><style>x{}</style></head>"
            "<body><h1>Heading</h1><script>alert(1)</script><p>Hello <b>world</b></p></body></html>"
        )
        self.assertEqual(title, "My Title")
        self.assertIn("Heading", body)
        self.assertIn("Hello world", body)
        self.assertNotIn("alert", body)
        self.assertNotIn("x{}", body)

    def test_html_entities_and_whitespace_are_normalized(self):
        _, body = web_fetch.html_to_text("<p>a&nbsp;&nbsp;b</p>\n\n\n<p>c</p>")
        self.assertEqual(body, "a b\nc")

    def test_tool_markers_are_neutralized(self):
        self.assertEqual(
            web_fetch.neutralize_tool_markers("[[tool:submit_food_review]]"),
            "[ [tool:submit_food_review]]",
        )

    def test_truncate_text_reports_truncation(self):
        text, truncated = web_fetch.truncate_text("abcdef", 3)
        self.assertEqual(text, "abc")
        self.assertTrue(truncated)
        self.assertFalse(web_fetch.truncate_text("abc", 3)[1])

    def test_format_page_includes_title_body_and_truncation(self):
        page = web_fetch.PageResult(
            "https://example.com/",
            "Title",
            "Body",
            True,
            "text/html",
        )
        formatted = web_fetch.format_page_result(page)
        self.assertIn("网页：Title", formatted)
        self.assertIn("Body", formatted)
        self.assertIn("内容已截断", formatted)

    def test_format_empty_body_uses_placeholder(self):
        page = web_fetch.PageResult("https://example.com/", "", "", False, "text/plain")
        formatted = web_fetch.format_page_result(page)
        self.assertIn("https://example.com/", formatted)
        self.assertIn("没有可读正文", formatted)

    def test_safe_error_message_never_echoes_reason(self):
        message = web_fetch.safe_error_message("blocked_ip")
        self.assertIn("公开网页", message)
        self.assertNotIn("blocked_ip", message)
        self.assertEqual(web_fetch.safe_error_message("unknown"), "无法读取该网页。")


if __name__ == "__main__":
    unittest.main()
