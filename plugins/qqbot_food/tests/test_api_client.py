from __future__ import annotations

import json
import unittest
from urllib.request import Request

from support import food_api, food_engine


class FakeTransport:
    def __init__(self, results=None, error=None):
        self.results = list(results or [])
        self.error = error
        self.requests: list[Request] = []
        self.timeouts: list[float] = []
        self.max_bytes_seen: list[int] = []

    def __call__(self, request, timeout, max_bytes):
        self.requests.append(request)
        self.timeouts.append(timeout)
        self.max_bytes_seen.append(max_bytes)
        if self.error is not None:
            raise self.error
        if not self.results:
            raise RuntimeError("no fake transport result")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def result(status=200, body=None, content_type="application/json", final_url=None, extra_headers=None):
    if body is None:
        body = b"{}"
    elif isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False).encode("utf-8")
    elif isinstance(body, str):
        body = body.encode("utf-8")
    headers = {"Content-Type": content_type}
    if extra_headers:
        headers.update(extra_headers)
    return food_api.TransportResult(
        status,
        headers,
        body,
        final_url or "https://food.dlsz.top/api/restaurants",
    )


class ApiClientTests(unittest.TestCase):
    def test_get_json_success_and_fixed_request(self):
        transport = FakeTransport([result(body={"ok": True})])
        client = food_api.FoodAPIClient(timeout=7, max_bytes=1234, transport=transport)
        payload = client.get_json(food_engine.FOOD_RESTAURANTS_PATH, {"q": "火锅"})
        self.assertEqual(payload, {"ok": True})
        request = transport.requests[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertTrue(request.full_url.startswith("https://food.dlsz.top/api/restaurants?"))
        self.assertEqual(transport.timeouts, [7.0])
        self.assertEqual(transport.max_bytes_seen, [1234])

    def test_get_rejects_unknown_path_before_transport(self):
        transport = FakeTransport([result()])
        client = food_api.FoodAPIClient(transport=transport)
        with self.assertRaises(food_engine.FoodAPIError):
            client.get_json("/api/admin", {})
        self.assertEqual(transport.requests, [])

    def test_get_non_200_raises_with_status(self):
        transport = FakeTransport([result(status=404, body={"error": "x"})])
        client = food_api.FoodAPIClient(transport=transport)
        with self.assertRaises(food_api.FoodHTTPError) as ctx:
            client.get_json(food_engine.FOOD_LOCATIONS_PATH, {"q": "x"})
        self.assertEqual(ctx.exception.status, 404)

    def test_get_redirect_final_url_is_rejected(self):
        transport = FakeTransport(
            [result(final_url="https://evil.example/api/restaurants")]
        )
        client = food_api.FoodAPIClient(transport=transport)
        with self.assertRaises(food_api.FoodTransportError):
            client.get_json(food_engine.FOOD_RESTAURANTS_PATH, {})

    def test_get_html_content_type_rejected(self):
        transport = FakeTransport([result(content_type="text/html", body=b"<html>")])
        client = food_api.FoodAPIClient(transport=transport)
        with self.assertRaises(food_api.FoodTransportError):
            client.get_json(food_engine.FOOD_RESTAURANTS_PATH, {})

    def test_get_non_json_body_rejected(self):
        transport = FakeTransport([result(content_type="text/plain", body=b"not json")])
        client = food_api.FoodAPIClient(transport=transport)
        with self.assertRaises(food_api.FoodTransportError):
            client.get_json(food_engine.FOOD_RESTAURANTS_PATH, {})

    def test_get_content_length_oversize_rejected(self):
        transport = FakeTransport(
            [result(extra_headers={"Content-Length": "999999"})]
        )
        client = food_api.FoodAPIClient(max_bytes=1024, transport=transport)
        with self.assertRaises(food_api.FoodResponseTooLarge):
            client.get_json(food_engine.FOOD_RESTAURANTS_PATH, {})

    def test_get_actual_body_oversize_rejected(self):
        transport = FakeTransport([result(body=b"x" * 2048)])
        client = food_api.FoodAPIClient(max_bytes=1024, transport=transport)
        with self.assertRaises(food_api.FoodResponseTooLarge):
            client.get_json(food_engine.FOOD_RESTAURANTS_PATH, {})

    def test_post_submit_success_fixed_endpoint_and_body(self):
        payload = {"name": "店", "password": "123456", "tags": ["火锅"]}
        transport = FakeTransport(
            [
                food_api.TransportResult(
                    200,
                    {"Content-Type": "application/json"},
                    json.dumps({"success": True, "review_id": 1}).encode(),
                    "https://food.dlsz.top/api/upload",
                )
            ]
        )
        client = food_api.FoodAPIClient(transport=transport)
        status, body = client.post_submit(payload)
        self.assertEqual(status, 200)
        self.assertTrue(body["success"])
        request = transport.requests[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://food.dlsz.top/api/upload")
        sent = json.loads(request.data.decode("utf-8"))
        self.assertEqual(sent["password"], "123456")
        self.assertEqual(sent["tags"], ["火锅"])

    def test_post_submit_returns_known_http_error_body(self):
        error_body = {"success": False, "code": "DUPLICATE_USERNAME", "error": "已存在"}
        transport = FakeTransport(
            [
                food_api.TransportResult(
                    400,
                    {"Content-Type": "application/json"},
                    json.dumps(error_body).encode(),
                    "https://food.dlsz.top/api/upload",
                )
            ]
        )
        client = food_api.FoodAPIClient(transport=transport)
        status, body = client.post_submit({"name": "店", "password": "123"})
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "DUPLICATE_USERNAME")

    def test_post_submit_500_html_is_not_success(self):
        transport = FakeTransport(
            [
                food_api.TransportResult(
                    500,
                    {"Content-Type": "text/html"},
                    b"<html>error</html>",
                    "https://food.dlsz.top/api/upload",
                )
            ]
        )
        client = food_api.FoodAPIClient(transport=transport)
        status, body = client.post_submit({"name": "店", "password": "123"})
        self.assertEqual(status, 500)
        self.assertEqual(body, {})

    def test_post_submit_rejects_cross_host_final_url(self):
        transport = FakeTransport(
            [
                food_api.TransportResult(
                    200,
                    {"Content-Type": "application/json"},
                    b'{"success":true}',
                    "https://evil.example/api/upload",
                )
            ]
        )
        client = food_api.FoodAPIClient(transport=transport)
        with self.assertRaises(food_api.FoodTransportError):
            client.post_submit({"name": "店", "password": "123"})

    def test_post_submit_redirect_status_cannot_be_success(self):
        transport = FakeTransport(
            [
                food_api.TransportResult(
                    302,
                    {"Content-Type": "application/json"},
                    b'{"success":true}',
                    "https://food.dlsz.top/api/upload",
                )
            ]
        )
        client = food_api.FoodAPIClient(transport=transport)
        status, body = client.post_submit({"name": "店", "password": "123"})
        self.assertEqual(status, 302)
        self.assertFalse(
            food_engine.parse_food_submit_response(status, body)["ok"]
        )

    def test_post_submit_timeout_raises_safe_error(self):
        transport = FakeTransport(error=food_api.FoodTimeoutError("timeout"))
        client = food_api.FoodAPIClient(transport=transport)
        with self.assertRaises(food_api.FoodTimeoutError):
            client.post_submit({"name": "店", "password": "123"})

    def test_post_submit_transport_oversize(self):
        transport = FakeTransport(
            [
                food_api.TransportResult(
                    200,
                    {"Content-Type": "application/json"},
                    b"x" * 5000,
                    "https://food.dlsz.top/api/upload",
                )
            ]
        )
        client = food_api.FoodAPIClient(max_bytes=1024, transport=transport)
        with self.assertRaises(food_api.FoodResponseTooLarge):
            client.post_submit({"name": "店", "password": "123"})

    def test_redirect_handler_never_builds_redirect_request(self):
        handler = food_api._NoRedirectHandler()
        self.assertIsNone(
            handler.redirect_request(
                Request("https://food.dlsz.top/api/restaurants"),
                None,
                302,
                "Found",
                {},
                "https://evil.example/",
            )
        )


if __name__ == "__main__":
    unittest.main()
