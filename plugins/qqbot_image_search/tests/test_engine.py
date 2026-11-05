from __future__ import annotations

import json
import socket
import unittest
from unittest.mock import patch

from _support import install_astrbot_stub

install_astrbot_stub()

import image_search as engine


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 24
BMP = b"BM" + b"\x00" * 32
AVIF = b"\x00\x00\x00\x18ftypavif" + b"\x00" * 24


class UrlSafetyTests(unittest.TestCase):
    def test_accepts_public_https_and_http(self):
        self.assertEqual(
            engine.normalize_http_url("https://cdn.example.com/a.jpg"),
            "https://cdn.example.com/a.jpg",
        )
        self.assertEqual(
            engine.normalize_http_url("http://cdn.example.com/a.jpg"),
            "http://cdn.example.com/a.jpg",
        )

    def test_protocol_relative_url_becomes_https(self):
        self.assertEqual(
            engine.normalize_http_url("//cdn.example.com/a.jpg"),
            "https://cdn.example.com/a.jpg",
        )

    def test_rejects_unsupported_schemes(self):
        for value in (
            "ftp://cdn.example.com/a.jpg",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "data:image/png;base64,abc",
        ):
            with self.subTest(value=value):
                with self.assertRaises(engine.ImageSearchError):
                    engine.normalize_http_url(value)

    def test_rejects_credentials(self):
        for value in (
            "https://user@cdn.example.com/a.jpg",
            "https://user:pass@cdn.example.com/a.jpg",
            "https://@cdn.example.com/a.jpg",
        ):
            with self.subTest(value=value):
                with self.assertRaises(engine.ImageSearchError):
                    engine.normalize_http_url(value)

    def test_rejects_internal_names(self):
        for host in (
            "localhost",
            "metadata.google.internal",
            "printer.local",
            "foo.internal",
            "service.lan",
            "intranet",
        ):
            with self.subTest(host=host):
                with self.assertRaises(engine.ImageSearchError) as ctx:
                    engine.normalize_http_url(f"https://{host}/a.jpg")
                self.assertIn(ctx.exception.reason, {"blocked_host", "invalid_url"})

    def test_rejects_non_public_ips(self):
        for host in (
            "127.0.0.1",
            "10.0.0.2",
            "192.168.1.9",
            "169.254.169.254",
            "0.0.0.0",
            "224.0.0.1",
            "240.0.0.1",
            "::1",
            "fe80::1",
            "fc00::1",
            "::ffff:10.0.0.1",
        ):
            rendered = f"[{host}]" if ":" in host else host
            with self.subTest(host=host):
                with self.assertRaises(engine.ImageSearchError) as ctx:
                    engine.normalize_http_url(f"https://{rendered}/a.jpg")
                self.assertEqual(ctx.exception.reason, "blocked_ip")

    def test_accepts_public_ipv4_and_ipv6(self):
        self.assertEqual(
            engine.normalize_http_url("https://8.8.8.8/a.png"),
            "https://8.8.8.8/a.png",
        )
        self.assertEqual(
            engine.normalize_http_url("https://[2606:4700:4700::1111]/a.png"),
            "https://[2606:4700:4700::1111]/a.png",
        )

    def test_idna_host_normalization(self):
        self.assertEqual(
            engine.normalize_http_url("https://münich.example/a.jpg"),
            "https://xn--mnich-kva.example/a.jpg",
        )

    def test_rejects_control_characters_and_backslashes(self):
        for value in ("https://cdn.example.com/a\n.jpg", "https://cdn.example.com\\@evil/a.jpg"):
            with self.subTest(value=value):
                with self.assertRaises(engine.ImageSearchError):
                    engine.normalize_http_url(value)

    def test_rejects_bad_ports(self):
        for value in ("https://cdn.example.com:0/a.jpg", "https://cdn.example.com:99999/a.jpg"):
            with self.subTest(value=value):
                with self.assertRaises(engine.ImageSearchError):
                    engine.normalize_http_url(value)

    def test_rejects_overlong_urls(self):
        with self.assertRaises(engine.ImageSearchError):
            engine.normalize_http_url("https://cdn.example.com/" + "a" * 5000)

    def test_fragment_is_removed(self):
        self.assertEqual(
            engine.normalize_http_url("https://cdn.example.com/a.jpg#frag"),
            "https://cdn.example.com/a.jpg",
        )

    def test_require_image_extension(self):
        self.assertEqual(
            engine.normalize_http_url(
                "https://cdn.example.com/a.JPG?x=1",
                require_image_extension=True,
            ),
            "https://cdn.example.com/a.JPG?x=1",
        )
        for value in (
            "https://cdn.example.com/a.php",
            "https://cdn.example.com/image?id=1",
        ):
            with self.subTest(value=value):
                with self.assertRaises(engine.ImageSearchError):
                    engine.normalize_http_url(
                        value, require_image_extension=True
                    )

    def test_safe_http_url_wrapper_never_raises(self):
        self.assertEqual(engine.safe_http_url("file:///etc/passwd"), "")
        self.assertEqual(engine.safe_http_url(None), "")

    def test_is_public_ip_variants(self):
        self.assertTrue(engine.is_public_ip("1.1.1.1"))
        self.assertFalse(engine.is_public_ip("224.0.0.1"))
        self.assertFalse(engine.is_public_ip("::ffff:127.0.0.1"))
        self.assertFalse(engine.is_public_ip("invalid"))


class DnsTests(unittest.TestCase):
    def _infos(self, *addresses):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
            for address in addresses
        ]

    def test_all_public_answers_are_returned(self):
        with patch(
            "image_search.socket.getaddrinfo",
            return_value=self._infos("8.8.8.8", "1.1.1.1"),
        ):
            self.assertEqual(
                engine.resolve_public_addresses("example.com", 443),
                ["8.8.8.8", "1.1.1.1"],
            )

    def test_private_answer_rejects_whole_host(self):
        with patch(
            "image_search.socket.getaddrinfo",
            return_value=self._infos("8.8.8.8", "10.0.0.1"),
        ):
            with self.assertRaises(engine.ImageSearchError) as ctx:
                engine.resolve_public_addresses("example.com", 443)
            self.assertEqual(ctx.exception.reason, "blocked_ip")

    def test_empty_answer_is_dns_failed(self):
        with patch("image_search.socket.getaddrinfo", return_value=[]):
            with self.assertRaises(engine.ImageSearchError) as ctx:
                engine.resolve_public_addresses("example.com", 443)
            self.assertEqual(ctx.exception.reason, "dns_failed")

    def test_dns_error_is_wrapped(self):
        with patch(
            "image_search.socket.getaddrinfo",
            side_effect=socket.gaierror("nope"),
        ):
            with self.assertRaises(engine.ImageSearchError) as ctx:
                engine.resolve_public_addresses("example.com", 443)
            self.assertEqual(ctx.exception.reason, "dns_failed")

    def test_duplicate_answers_are_removed(self):
        with patch(
            "image_search.socket.getaddrinfo",
            return_value=self._infos("8.8.8.8", "8.8.8.8"),
        ):
            self.assertEqual(
                engine.resolve_public_addresses("example.com", 443),
                ["8.8.8.8"],
            )


class MagicAndValidationTests(unittest.TestCase):
    def test_detects_supported_magic_formats(self):
        cases = {
            JPEG: ("image/jpeg", ".jpg"),
            PNG: ("image/png", ".png"),
            GIF: ("image/gif", ".gif"),
            WEBP: ("image/webp", ".webp"),
            BMP: ("image/bmp", ".bmp"),
            AVIF: ("image/avif", ".avif"),
        }
        for data, expected in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(engine.detect_image_magic(data), expected)

    def test_rejects_unknown_magic(self):
        self.assertIsNone(engine.detect_image_magic(b"<html>not an image</html>"))

    def test_validate_image_payload_accepts_matching_type(self):
        self.assertEqual(
            engine.validate_image_payload(PNG, "image/png", max_bytes=1000),
            ("image/png", ".png"),
        )

    def test_validate_image_payload_allows_jpg_and_bmp_aliases(self):
        self.assertEqual(
            engine.validate_image_payload(JPEG, "image/jpg", max_bytes=1000)[0],
            "image/jpeg",
        )
        self.assertEqual(
            engine.validate_image_payload(BMP, "image/x-ms-bmp", max_bytes=1000)[0],
            "image/bmp",
        )

    def test_validate_image_payload_allows_octet_stream_with_magic(self):
        self.assertEqual(
            engine.validate_image_payload(PNG, "application/octet-stream", max_bytes=1000),
            ("image/png", ".png"),
        )

    def test_validate_image_payload_rejects_bad_declared_type(self):
        with self.assertRaises(engine.ImageSearchError) as ctx:
            engine.validate_image_payload(PNG, "text/html", max_bytes=1000)
        self.assertEqual(ctx.exception.reason, "bad_content_type")

    def test_validate_image_payload_rejects_type_mismatch(self):
        with self.assertRaises(engine.ImageSearchError) as ctx:
            engine.validate_image_payload(PNG, "image/jpeg", max_bytes=1000)
        self.assertEqual(ctx.exception.reason, "bad_content_type")

    def test_validate_image_payload_rejects_empty_and_oversize(self):
        with self.assertRaises(engine.ImageSearchError) as ctx:
            engine.validate_image_payload(b"", "image/png", max_bytes=1000)
        self.assertEqual(ctx.exception.reason, "empty")
        with self.assertRaises(engine.ImageSearchError) as ctx:
            engine.validate_image_payload(PNG, "image/png", max_bytes=4)
        self.assertEqual(ctx.exception.reason, "too_large")

    def test_validate_image_payload_rejects_bad_magic(self):
        with self.assertRaises(engine.ImageSearchError) as ctx:
            engine.validate_image_payload(b"not image", "image/png", max_bytes=1000)
        self.assertEqual(ctx.exception.reason, "bad_magic")


class _FakeResponse:
    def __init__(self, status=200, headers=None, body=b"ok"):
        self.status = status
        self.headers = headers or {}
        self._body = body

    def read(self, amount):
        return self._body[:amount]


class _FakeConnection:
    last = None

    def __init__(self, host, port, *, pinned_ip, timeout):
        self.host = host
        self.port = port
        self.pinned_ip = pinned_ip
        self.timeout = timeout
        _FakeConnection.last = self

    def request(self, method, path, headers=None):
        self.request_data = (method, path, dict(headers or {}))

    def getresponse(self):
        return _FakeConnection.response

    def close(self):
        self.closed = True


class RequestOnceTests(unittest.TestCase):
    def _run(self, response, max_bytes=1024):
        _FakeConnection.response = response
        with patch(
            "image_search.resolve_public_addresses",
            return_value=["93.184.216.34"],
        ), patch("image_search._PinnedHTTPSConnection", _FakeConnection):
            return engine._request_once(
                "https://example.com/path",
                timeout=3,
                max_bytes=max_bytes,
                headers={"User-Agent": "test"},
            )

    def test_connection_is_pinned(self):
        self._run(_FakeResponse(headers={"Content-Type": "text/html"}))
        self.assertEqual(_FakeConnection.last.pinned_ip, "93.184.216.34")

    def test_redirect_is_returned(self):
        result = self._run(
            _FakeResponse(status=301, headers={"Location": "/next"})
        )
        self.assertEqual(result.status, 301)
        self.assertEqual(result.location, "/next")

    def test_oversized_content_length_rejected(self):
        with self.assertRaises(engine.ImageSearchError) as ctx:
            self._run(
                _FakeResponse(
                    headers={"Content-Type": "text/html", "Content-Length": "9999"}
                ),
                max_bytes=100,
            )
        self.assertEqual(ctx.exception.reason, "too_large")

    def test_oversized_stream_rejected(self):
        with self.assertRaises(engine.ImageSearchError) as ctx:
            self._run(
                _FakeResponse(headers={"Content-Type": "text/html"}, body=b"x" * 101),
                max_bytes=100,
            )
        self.assertEqual(ctx.exception.reason, "too_large")

    def test_compressed_response_rejected(self):
        with self.assertRaises(engine.ImageSearchError) as ctx:
            self._run(
                _FakeResponse(
                    headers={
                        "Content-Type": "text/html",
                        "Content-Encoding": "gzip",
                    }
                )
            )
        self.assertEqual(ctx.exception.reason, "bad_encoding")

    def test_timeout_is_mapped(self):
        _FakeConnection.response = _FakeResponse()
        with patch(
            "image_search.resolve_public_addresses",
            return_value=["93.184.216.34"],
        ), patch("image_search._PinnedHTTPSConnection", _FakeConnection), patch.object(
            _FakeConnection, "request", side_effect=socket.timeout("slow")
        ):
            with self.assertRaises(engine.ImageSearchError) as ctx:
                engine._request_once(
                    "https://example.com/",
                    timeout=1,
                    max_bytes=10,
                    headers={},
                )
        self.assertEqual(ctx.exception.reason, "timeout")


class HttpGetTests(unittest.TestCase):
    def test_follows_relative_redirect(self):
        hops = [
            engine.HopResult(302, "/next", "", b""),
            engine.HopResult(200, "", "text/html; charset=utf-8", b"<html>ok</html>"),
        ]
        with patch("image_search._request_once", side_effect=hops) as request:
            result = engine.http_get("https://example.com/start", expected="html")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(result.data, b"<html>ok</html>")

    def test_redirect_to_private_host_is_bad_redirect(self):
        with patch(
            "image_search._request_once",
            return_value=engine.HopResult(302, "https://127.0.0.1/x", "", b""),
        ):
            with self.assertRaises(engine.ImageSearchError) as ctx:
                engine.http_get("https://example.com/", expected="html")
        self.assertEqual(ctx.exception.reason, "bad_redirect")

    def test_too_many_redirects(self):
        with patch(
            "image_search._request_once",
            return_value=engine.HopResult(302, "/again", "", b""),
        ):
            with self.assertRaises(engine.ImageSearchError) as ctx:
                engine.http_get(
                    "https://example.com/",
                    max_redirects=2,
                    expected="html",
                )
        self.assertEqual(ctx.exception.reason, "too_many_redirects")

    def test_missing_location(self):
        with patch(
            "image_search._request_once",
            return_value=engine.HopResult(302, "", "", b""),
        ):
            with self.assertRaises(engine.ImageSearchError) as ctx:
                engine.http_get("https://example.com/", expected="html")
        self.assertEqual(ctx.exception.reason, "bad_redirect")

    def test_non_200_is_http_error(self):
        with patch(
            "image_search._request_once",
            return_value=engine.HopResult(404, "", "", b""),
        ):
            with self.assertRaises(engine.ImageSearchError) as ctx:
                engine.http_get("https://example.com/", expected="html")
        self.assertEqual(ctx.exception.reason, "http_error")

    def test_html_content_type_is_enforced(self):
        with patch(
            "image_search._request_once",
            return_value=engine.HopResult(200, "", "image/png", b"x"),
        ):
            with self.assertRaises(engine.ImageSearchError) as ctx:
                engine.http_get("https://example.com/", expected="html")
        self.assertEqual(ctx.exception.reason, "bad_content_type")

    def test_json_content_type_is_enforced(self):
        with patch(
            "image_search._request_once",
            return_value=engine.HopResult(200, "", "text/html", b"{}"),
        ):
            with self.assertRaises(engine.ImageSearchError) as ctx:
                engine.http_get("https://example.com/", expected="json")
        self.assertEqual(ctx.exception.reason, "bad_content_type")

    def test_image_content_type_is_enforced(self):
        with patch(
            "image_search._request_once",
            return_value=engine.HopResult(200, "", "text/html", PNG),
        ):
            with self.assertRaises(engine.ImageSearchError) as ctx:
                engine.http_get("https://example.com/", expected="image")
        self.assertEqual(ctx.exception.reason, "bad_content_type")

    def test_content_type_parameters_are_ignored(self):
        with patch(
            "image_search._request_once",
            return_value=engine.HopResult(
                200,
                "",
                "application/json; charset=utf-8",
                b'{"data": []}',
            ),
        ):
            result = engine.http_get("https://example.com/", expected="json")
        self.assertEqual(result.data, b'{"data": []}')

    def test_timeout_propagates(self):
        with patch(
            "image_search._request_once",
            side_effect=engine.ImageSearchError("timeout"),
        ):
            with self.assertRaises(engine.ImageSearchError) as ctx:
                engine.http_get("https://example.com/", expected="html")
        self.assertEqual(ctx.exception.reason, "timeout")


class ParserTests(unittest.TestCase):
    def _bing_payload(self, **overrides):
        payload = {
            "murl": "https://cdn.example.com/cat.jpg",
            "t": "cute cat",
            "desc": "a cat photo",
            "purl": "https://example.com/page",
        }
        payload.update(overrides)
        return payload

    def test_bing_primary_json_parser(self):
        import html as html_lib

        payload = json.dumps(self._bing_payload())
        page = f'<a class="iusc" m="{html_lib.escape(payload, quote=True)}"></a>'
        candidates = engine.parse_bing_image_candidates(page, limit=3)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["url"], "https://cdn.example.com/cat.jpg")
        self.assertEqual(candidates[0]["title"], "cute cat")
        self.assertEqual(candidates[0]["description"], "a cat photo")

    def test_bing_escaped_murl_fragment_fallback(self):
        page = (
            '&#34;murl&#34;:&#34;https://cdn.example.com/a.jpg&#34;,'
            '&#34;t&#34;:&#34;title&#34;'
        )
        urls = engine.parse_bing_image_urls(page, limit=3)
        self.assertEqual(urls, ["https://cdn.example.com/a.jpg"])

    def test_bing_absolute_image_url_fallback(self):
        page = '<img src="https://cdn.example.com/fallback.png">'
        self.assertEqual(
            engine.parse_bing_image_urls(page, limit=3),
            ["https://cdn.example.com/fallback.png"],
        )

    def test_bing_rejects_non_image_paths(self):
        page = '<img src="https://cdn.example.com/page.php">'
        self.assertEqual(engine.parse_bing_image_urls(page, limit=3), [])

    def test_bing_rejects_private_and_duplicate_urls(self):
        page = (
            '&#34;murl&#34;:&#34;https://cdn.example.com/a.jpg&#34;'
            '&#34;murl&#34;:&#34;https://cdn.example.com/a.jpg&#34;'
            '&#34;murl&#34;:&#34;http://127.0.0.1/evil.jpg&#34;'
        )
        self.assertEqual(
            engine.parse_bing_image_urls(page, limit=5),
            ["https://cdn.example.com/a.jpg"],
        )

    def test_bing_limit_is_respected(self):
        page = " ".join(
            f'&#34;murl&#34;:&#34;https://cdn.example.com/{index}.jpg&#34;'
            for index in range(10)
        )
        self.assertEqual(len(engine.parse_bing_image_urls(page, limit=3)), 3)

    def _baidu_payload(self):
        return {
            "data": [
                {
                    "middleURL": "https://img.example.com/cat.jpg",
                    "fromPageTitleEnc": "可爱的猫",
                    "fromPageTitle": "可爱的猫",
                    "fromURLHost": "img.example.com",
                    "fromURL": "https://example.com/page",
                },
                {
                    "thumbURL": "https://img.example.com/dog.jpg",
                    "fromPageTitleEnc": "小狗",
                },
            ]
        }

    def test_baidu_json_parser(self):
        candidates = engine.parse_baidu_image_candidates(
            self._baidu_payload(), limit=5
        )
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]["url"], "https://img.example.com/cat.jpg")
        self.assertIn("可爱的猫", candidates[0]["title"])
        self.assertEqual(candidates[1]["url"], "https://img.example.com/dog.jpg")

    def test_baidu_accepts_json_string_and_replace_url(self):
        payload = json.dumps(
            {
                "data": [
                    {
                        "replaceUrl": [
                            {
                                "ObjURL": "https://img.example.com/a.png",
                                "FromURL": "https://example.com/source",
                            }
                        ],
                        "fromPageTitle": "A",
                    }
                ]
            }
        )
        candidates = engine.parse_baidu_image_candidates(payload, limit=3)
        self.assertEqual(candidates[0]["url"], "https://img.example.com/a.png")
        self.assertEqual(candidates[0]["page_url"], "https://example.com/source")

    def test_baidu_rejects_invalid_entries_and_dedupes(self):
        payload = {
            "data": [
                None,
                "bad",
                {"middleURL": "http://127.0.0.1/a.jpg"},
                {"middleURL": "https://img.example.com/a.jpg"},
                {"middleURL": "https://img.example.com/a.jpg"},
            ]
        }
        self.assertEqual(
            engine.parse_baidu_image_urls(payload, limit=5),
            ["https://img.example.com/a.jpg"],
        )

    def test_parsers_ignore_malformed_payloads(self):
        self.assertEqual(engine.parse_baidu_image_candidates("{bad json", limit=3), [])
        self.assertEqual(engine.parse_baidu_image_candidates({"data": "no"}, limit=3), [])
        self.assertEqual(engine.parse_bing_image_candidates("", limit=3), [])

    def test_dedupe_candidates_is_url_based(self):
        candidates = [
            {"url": "https://cdn.example.com/a.jpg", "title": "one"},
            {"url": "https://cdn.example.com/a.jpg#frag", "title": "two"},
            {"url": "https://cdn.example.com/b.jpg", "title": "three"},
        ]
        deduped = engine.dedupe_candidates(candidates)
        self.assertEqual([item["url"] for item in deduped], [
            "https://cdn.example.com/a.jpg",
            "https://cdn.example.com/b.jpg",
        ])


class RelevanceTests(unittest.TestCase):
    def test_exact_cjk_title_scores_highest(self):
        self.assertEqual(engine.relevance_score("猫", "可爱的猫", ""), 1.0)

    def test_description_match_scores_high(self):
        self.assertGreaterEqual(
            engine.relevance_score("猫", "宠物", "一只猫的照片"),
            0.9,
        )

    def test_ascii_whole_word_match(self):
        self.assertEqual(engine.relevance_score("cat", "a cat photo", ""), 1.0)
        self.assertEqual(engine.relevance_score("cat", "catalog page", ""), 0.0)

    def test_alias_translation(self):
        self.assertGreaterEqual(
            engine.relevance_score("初音未来", "Hatsune Miku wallpaper", ""),
            0.9,
        )
        self.assertGreaterEqual(
            engine.relevance_score("hatsune miku", "初音未来 壁纸", ""),
            0.9,
        )

    def test_traditional_variant_normalization(self):
        self.assertGreaterEqual(
            engine.relevance_score("樱花", "櫻花 壁纸", ""),
            0.9,
        )

    def test_normalize_relevance_text(self):
        self.assertEqual(
            engine.normalize_relevance_text("  Hello，WORLD!! "),
            "hello world",
        )

    def test_filter_candidates_threshold_and_sort(self):
        candidates = [
            {"url": "https://x/a.jpg", "title": "无关", "description": ""},
            {"url": "https://x/b.jpg", "title": "猫", "description": ""},
            {"url": "https://x/c.jpg", "title": "宠物", "description": "一只猫"},
        ]
        filtered = engine.filter_candidates("猫", candidates, min_score=0.6)
        self.assertEqual([item["url"] for item in filtered], [
            "https://x/b.jpg",
            "https://x/c.jpg",
        ])
        self.assertEqual(filtered[0]["relevance_score"], 1.0)

    def test_relevance_empty_inputs(self):
        self.assertEqual(engine.relevance_score("", "title", "desc"), 0.0)
        self.assertEqual(engine.relevance_score("cat", "", ""), 0.0)


def bing_page_for(title: str, url: str) -> str:
    import html as html_lib

    payload = json.dumps(
        {
            "murl": url,
            "t": title,
            "desc": title,
            "purl": "https://example.com/page",
        }
    )
    return f'<a m="{html_lib.escape(payload, quote=True)}"></a>'


def baidu_payload_for(title: str, url: str) -> str:
    return json.dumps(
        {
            "data": [
                {
                    "middleURL": url,
                    "fromPageTitleEnc": title,
                    "fromPageTitle": title,
                    "fromURLHost": "img.example.com",
                }
            ]
        }
    )


class SearchPipelineTests(unittest.TestCase):
    def test_bing_success_does_not_call_baidu(self):
        options = engine.SearchOptions(result_count=1, max_candidates=5)
        page = bing_page_for("猫", "https://cdn.example.com/cat.jpg")
        with patch(
            "image_search.fetch_search_html", return_value=page
        ) as bing, patch("image_search.fetch_baidu_json") as baidu:
            candidates = engine.search_image_candidates("猫", options)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["source"], "bing")
        bing.assert_called_once()
        baidu.assert_not_called()

    def test_bing_failure_falls_back_to_baidu(self):
        options = engine.SearchOptions(result_count=1, max_candidates=5)
        with patch(
            "image_search.fetch_search_html",
            side_effect=engine.ImageSearchError("network"),
        ), patch(
            "image_search.fetch_baidu_json",
            return_value=baidu_payload_for("猫", "https://img.example.com/cat.jpg"),
        ):
            candidates = engine.search_image_candidates("猫", options)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["source"], "baidu")
        self.assertEqual(candidates[0]["url"], "https://img.example.com/cat.jpg")

    def test_baidu_merge_dedupes_when_bing_insufficient(self):
        options = engine.SearchOptions(result_count=3, max_candidates=10)
        page = bing_page_for("猫", "https://cdn.example.com/cat.jpg")
        baidu_payload = json.dumps(
            {
                "data": [
                    {
                        "middleURL": "https://cdn.example.com/cat.jpg",
                        "fromPageTitleEnc": "猫",
                    },
                    {
                        "middleURL": "https://cdn.example.com/cat2.jpg",
                        "fromPageTitleEnc": "猫",
                    },
                ]
            }
        )
        with patch("image_search.fetch_search_html", return_value=page), patch(
            "image_search.fetch_baidu_json", return_value=baidu_payload
        ):
            candidates = engine.search_image_candidates("猫", options)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(
            [item["url"] for item in candidates],
            [
                "https://cdn.example.com/cat.jpg",
                "https://cdn.example.com/cat2.jpg",
            ],
        )

    def test_candidate_limit_is_respected(self):
        options = engine.SearchOptions(
            result_count=3,
            max_candidates=1,
            relevance_enabled=False,
        )
        page = bing_page_for("猫", "https://cdn.example.com/cat1.jpg") + bing_page_for(
            "猫", "https://cdn.example.com/cat2.jpg"
        )
        with patch("image_search.fetch_search_html", return_value=page), patch(
            "image_search.fetch_baidu_json"
        ):
            candidates = engine.search_image_candidates("猫", options)
        self.assertEqual(len(candidates), 1)

    def test_all_sources_failed_raises_network(self):
        options = engine.SearchOptions(result_count=1)
        with patch(
            "image_search.fetch_search_html",
            side_effect=engine.ImageSearchError("network"),
        ), patch(
            "image_search.fetch_baidu_json",
            side_effect=engine.ImageSearchError("http_error"),
        ):
            with self.assertRaises(engine.ImageSearchError) as ctx:
                engine.search_image_candidates("猫", options)
        self.assertEqual(ctx.exception.reason, "network")

    def test_timeout_error_is_preserved(self):
        options = engine.SearchOptions(result_count=1)
        with patch(
            "image_search.fetch_search_html",
            side_effect=engine.ImageSearchError("timeout"),
        ), patch(
            "image_search.fetch_baidu_json",
            side_effect=engine.ImageSearchError("network"),
        ):
            with self.assertRaises(engine.ImageSearchError) as ctx:
                engine.search_image_candidates("猫", options)
        self.assertEqual(ctx.exception.reason, "timeout")

    def test_empty_keyword_rejected(self):
        with self.assertRaises(engine.ImageSearchError) as ctx:
            engine.search_image_candidates("   ")
        self.assertEqual(ctx.exception.reason, "invalid_keyword")

    def test_relevance_disabled_keeps_empty_metadata_candidates(self):
        options = engine.SearchOptions(
            result_count=1,
            max_candidates=5,
            relevance_enabled=False,
        )
        page = '<img src="https://cdn.example.com/no-meta.png">'
        with patch("image_search.fetch_search_html", return_value=page), patch(
            "image_search.fetch_baidu_json"
        ):
            candidates = engine.search_image_candidates("猫", options)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["relevance_score"], 1.0)


class BuilderTests(unittest.TestCase):
    def test_bing_url_contains_keyword_and_bounds(self):
        url = engine.build_bing_image_search_url(
            "猫", result_count=3, safe_search="strict"
        )
        self.assertTrue(url.startswith(engine.BING_IMAGES_ENDPOINT))
        parsed = dict(
            part.split("=", 1) for part in url.split("?", 1)[1].split("&")
        )
        self.assertEqual(parsed["q"], "%E7%8C%AB")
        self.assertEqual(parsed["adlt"], "strict")
        self.assertEqual(parsed["setlang"], "zh-hans")

    def test_baidu_url_contains_keyword_and_count(self):
        url = engine.build_baidu_image_search_url("猫", result_count=2)
        self.assertTrue(url.startswith(engine.BAIDU_IMAGES_ENDPOINT))
        self.assertIn("word=%E7%8C%AB", url)
        self.assertIn("rn=16", url)


class DownloadTests(unittest.TestCase):
    def test_download_image_bytes_success(self):
        with patch(
            "image_search.http_get",
            return_value=engine.HopResult(200, "", "image/png", PNG),
        ):
            data, mime, suffix = engine.download_image_bytes(
                "https://cdn.example.com/a.png"
            )
        self.assertEqual(data, PNG)
        self.assertEqual((mime, suffix), ("image/png", ".png"))

    def test_download_image_bytes_rejects_bad_magic(self):
        with patch(
            "image_search.http_get",
            return_value=engine.HopResult(200, "", "image/png", b"<html>"),
        ):
            with self.assertRaises(engine.ImageSearchError) as ctx:
                engine.download_image_bytes("https://cdn.example.com/a.png")
        self.assertEqual(ctx.exception.reason, "bad_magic")

    def test_download_image_bytes_rejects_text_content_type(self):
        with patch(
            "image_search.http_get",
            return_value=engine.HopResult(200, "", "text/html", PNG),
        ):
            with self.assertRaises(engine.ImageSearchError) as ctx:
                engine.download_image_bytes("https://cdn.example.com/a.png")
        self.assertEqual(ctx.exception.reason, "bad_content_type")

    def test_download_image_bytes_rejects_oversize(self):
        oversized = PNG + b"x" * 2048
        with patch(
            "image_search.http_get",
            return_value=engine.HopResult(200, "", "image/png", oversized),
        ):
            with self.assertRaises(engine.ImageSearchError) as ctx:
                engine.download_image_bytes(
                    "https://cdn.example.com/a.png", max_bytes=1024
                )
        self.assertEqual(ctx.exception.reason, "too_large")

    def test_download_image_bytes_rejects_empty(self):
        with patch(
            "image_search.http_get",
            return_value=engine.HopResult(200, "", "image/png", b""),
        ):
            with self.assertRaises(engine.ImageSearchError) as ctx:
                engine.download_image_bytes("https://cdn.example.com/a.png")
        self.assertEqual(ctx.exception.reason, "empty")

    def test_download_image_to_temp_writes_suffix(self):
        with patch(
            "image_search.download_image_bytes",
            return_value=(PNG, "image/png", ".png"),
        ):
            item = engine.download_image_to_temp("https://cdn.example.com/a.png")
        try:
            self.assertTrue(item.path.endswith(".png"))
            with open(item.path, "rb") as handle:
                self.assertEqual(handle.read(), PNG)
            self.assertEqual(item.mime, "image/png")
        finally:
            import os

            os.unlink(item.path)

    def test_download_images_tries_bounded_candidates(self):
        first = engine.DownloadedImage("/tmp/a.jpg", "image/jpeg", ".jpg", "https://x/a.jpg")
        second = engine.DownloadedImage("/tmp/b.png", "image/png", ".png", "https://x/b.png")
        candidates = [
            {"url": "https://cdn.example.com/a.jpg"},
            {"url": "https://cdn.example.com/b.png"},
            {"url": "https://cdn.example.com/c.gif"},
        ]
        options = engine.SearchOptions(download_attempts=3)
        with patch(
            "image_search.download_image_to_temp",
            side_effect=[first, engine.ImageSearchError("bad_magic"), second],
        ) as downloader:
            downloaded, remote = engine.download_images(
                candidates,
                result_limit=2,
                options=options,
            )
        self.assertEqual(len(downloaded), 2)
        self.assertEqual(downloader.call_count, 3)
        self.assertEqual(remote, [c["url"] for c in candidates])

    def test_download_images_empty_candidates(self):
        downloaded, remote = engine.download_images([], result_limit=3)
        self.assertEqual(downloaded, [])
        self.assertEqual(remote, [])


class NaturalLanguageTests(unittest.TestCase):
    def test_detects_basic_request(self):
        intent = engine.detect_natural_language_image_search("帮我找三张猫的图片")
        self.assertEqual(intent["keyword"], "猫")
        self.assertEqual(intent["result_count"], 3)

    def test_detects_alias_keyword(self):
        intent = engine.detect_natural_language_image_search("来几张初音未来壁纸")
        self.assertEqual(intent["keyword"], "初音未来")
        self.assertEqual(intent["result_count"], 3)

    def test_xia_does_not_mean_one(self):
        intent = engine.detect_natural_language_image_search("搜一下白色小狗照片")
        self.assertEqual(intent["keyword"], "白色小狗")
        self.assertEqual(intent["result_count"], 3)

    def test_quantity_two(self):
        intent = engine.detect_natural_language_image_search("我要两张千早爱音的插画")
        self.assertEqual(intent["keyword"], "千早爱音")
        self.assertEqual(intent["result_count"], 2)

    def test_refusal_is_not_detected(self):
        self.assertIsNone(engine.detect_natural_language_image_search("不要找猫图"))

    def test_generic_request_without_keyword_is_not_detected(self):
        for text in ("找图片", "帮我找几张图片", "今天天气不错", ""):
            with self.subTest(text=text):
                self.assertIsNone(engine.detect_natural_language_image_search(text))

    def test_later_request_after_negation_still_detected(self):
        intent = engine.detect_natural_language_image_search(
            "找不到猫图，帮我找几张猫图"
        )
        self.assertEqual(intent["keyword"], "猫")

    def test_clean_keyword_strips_filler(self):
        intent = engine.detect_natural_language_image_search(
            "帮我找一下可爱的猫图片"
        )
        self.assertEqual(intent["keyword"], "可爱的猫")
        self.assertEqual(
            engine.clean_natural_language_image_keyword("一下可爱的猫图片"),
            "可爱的猫",
        )

    def test_result_count_mapping_and_cap(self):
        self.assertEqual(engine.natural_language_result_count("一"), 1)
        self.assertEqual(engine.natural_language_result_count("五"), 5)
        self.assertEqual(engine.natural_language_result_count("九"), 3)
        self.assertEqual(engine.natural_language_result_count(None, default_limit=2), 2)

    def test_cooldown_allows_then_denies(self):
        now = [100.0]
        tracker = engine.CooldownTracker(clock=lambda: now[0])
        self.assertEqual(
            tracker.consume("g", "u", group_seconds=10, user_seconds=5), 0
        )
        self.assertEqual(
            tracker.consume("g", "u", group_seconds=10, user_seconds=5), 10
        )
        now[0] = 106.0
        self.assertEqual(
            tracker.remaining("g", "u", group_seconds=10, user_seconds=5), 4
        )
        now[0] = 110.0
        self.assertEqual(
            tracker.consume("g", "u", group_seconds=10, user_seconds=5), 0
        )

    def test_cooldown_is_per_user_and_group(self):
        now = [100.0]
        tracker = engine.CooldownTracker(clock=lambda: now[0])
        tracker.consume("group-a", "user-1", group_seconds=10, user_seconds=30)
        self.assertEqual(
            tracker.remaining("group-b", "user-1", group_seconds=10, user_seconds=30),
            30,
        )
        self.assertEqual(
            tracker.remaining("group-a", "user-2", group_seconds=10, user_seconds=30),
            10,
        )

    def test_cooldown_zero_disables(self):
        tracker = engine.CooldownTracker(clock=lambda: 1.0)
        for _ in range(3):
            self.assertEqual(
                tracker.consume("g", "u", group_seconds=0, user_seconds=0), 0
            )

    def test_cooldown_clear(self):
        now = [100.0]
        tracker = engine.CooldownTracker(clock=lambda: now[0])
        tracker.consume("g", "u", group_seconds=100, user_seconds=100)
        tracker.clear()
        self.assertEqual(
            tracker.remaining("g", "u", group_seconds=100, user_seconds=100), 0
        )


if __name__ == "__main__":
    unittest.main()
