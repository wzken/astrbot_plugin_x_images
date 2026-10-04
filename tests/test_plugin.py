"""Offline contract tests; AstrBot adapter is stubbed, HTTP uses MockTransport."""

import asyncio
import base64
import importlib
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_x_images.service import (  # noqa: E402
    GeneratedImages,
    ImageService,
    Lookup,
    Photo,
    PluginError,
    Settings,
    api_endpoint,
    extract_tweet_ids,
    original_image_url,
    search_result_ids,
)


def load_adapter():
    # Keep the stubs isolated from other test modules and real installations.
    modules = {
        name: types.ModuleType(name)
        for name in [
            "astrbot",
            "astrbot.api",
            "astrbot.api.event",
            "astrbot.api.message_components",
            "astrbot.api.star",
        ]
    }

    class Star:
        def __init__(self, context):
            self.context = context

    class Image:
        @staticmethod
        def fromBytes(data):
            return ("image", data)

    def decorator(*args, **kwargs):
        return lambda f: f

    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = types.SimpleNamespace(error=lambda *args: None)
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].filter = types.SimpleNamespace(
        command=decorator, llm_tool=decorator
    )
    modules["astrbot.api.message_components"].Image = Image
    modules["astrbot.api.message_components"].Plain = lambda text: ("text", text)
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].Star = Star
    with patch.dict(sys.modules, modules):
        return importlib.import_module("astrbot_plugin_x_images.main")


adapter = load_adapter()
JPEG = b"\xff\xd8\xff\xe0test-image"


def search_response(*urls, text=""):
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": [
                            {"type": "url_citation", "url": url} for url in urls
                        ],
                    },
                ],
            }
        ],
    }


def tweet(status_id="123", photos=None):
    return {
        "code": 200,
        "tweet": {
            "id": status_id,
            "author": {"screen_name": "NASA"},
            "media": {"photos": [{"url": url} for url in (photos or [])]},
        },
    }


class ParsingTests(unittest.TestCase):
    def test_recorded_new_api_x_search_response(self):
        path = Path(__file__).parent / "fixtures" / "x_search_response.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            search_result_ids(data), ["2106353949347664211", "2106324631301062850"]
        )

    def test_uncited_fabricated_ids_are_never_used(self):
        text = "\n".join(
            f"https://x.com/a/status/{sid}"
            for sid in [
                "1871234567890123456",
                "1872345678901234567",
                "1873456789012345678",
            ]
        )
        with self.assertRaises(PluginError) as exc:
            search_result_ids(search_response(text=text))
        self.assertEqual(exc.exception.code, "search_sources_missing")

    def test_reasoning_urls_and_uncited_answers_are_ignored(self):
        data = search_response(
            "https://x.com/a/status/123", text="https://x.com/a/status/999"
        )
        data["output"].insert(
            0,
            {"type": "reasoning", "summary": [{"text": "https://x.com/a/status/888"}]},
        )
        self.assertEqual(search_result_ids(data), ["123"])

    def test_unexecuted_tool_text_is_reported(self):
        with self.assertRaises(PluginError) as exc:
            search_result_ids(
                search_response(
                    text="<tool_call>\nx_keyword_search\nquery\nplana ブルアカ\n</tool_call>"
                )
            )
        self.assertEqual(exc.exception.code, "search_tool_not_executed")

    def test_incomplete_and_non_responses_payloads_are_rejected(self):
        for data, expected in [
            (
                {"choices": [{"message": {"content": "https://x.com/a/status/123"}}]},
                "search_protocol_error",
            ),
            ({"status": "incomplete", "output": []}, "search_incomplete"),
        ]:
            with self.assertRaises(PluginError) as exc:
                search_result_ids(data)
            self.assertEqual(exc.exception.code, expected)

    def test_proxy_config_validation(self):
        for proxy in ["http://127.0.0.1:7890", "http://user:pass@proxy.test:8080"]:
            self.assertEqual(Settings.from_config({"proxy": proxy}).proxy, proxy)
        for proxy in [
            "127.0.0.1:7890",
            "ftp://proxy.test",
            "http://p:bad",
            "http://p/path",
            "socks5://localhost:7891",
        ]:
            with self.assertRaises(PluginError):
                Settings.from_config({"proxy": proxy})
        with self.assertRaises(PluginError):
            Settings.from_config({"proxy_api": "false"})

    def test_links_normalize_deduplicate_and_preserve_order(self):
        self.assertEqual(
            extract_tweet_ids(
                "[a](https://x.com/NASA/status/123/photo/1) "
                "https://mobile.twitter.com/NASA/status/123?s=20 "
                "https://twitter.com/i/web/status/456。https://x.com/i/status/789"
            ),
            ["123", "456", "789"],
        )

    def test_host_spoofing_and_credentials_are_rejected(self):
        for url in [
            "https://x.com.evil.test/a/status/1",
            "https://evil.test/x.com/a/status/1",
            "https://x.com@evil.test/a/status/1",
            "https://user@x.com/a/status/1",
            "http://127.0.0.1/a/status/1",
            "https://x.com:bad/a/status/1",
        ]:
            with self.subTest(url=url):
                self.assertEqual(extract_tweet_ids(url), [])

    def test_original_resolution_and_media_allowlist(self):
        self.assertEqual(
            original_image_url("https://pbs.twimg.com/media/abc.jpg?name=small"),
            "https://pbs.twimg.com/media/abc?format=jpg&name=orig",
        )
        for url in [
            "https://evil.test/media/a.jpg",
            "http://pbs.twimg.com/media/a.jpg",
            "https://pbs.twimg.com/profile_images/a.jpg",
            "https://pbs.twimg.com/media/a.svg",
            "https://pbs.twimg.com@127.0.0.1/media/a.jpg",
        ]:
            with self.subTest(url=url), self.assertRaises(PluginError):
                original_image_url(url)

    def test_base_url_and_config_validation(self):
        for base in ["https://relay.test", "https://relay.test/v1/"]:
            self.assertEqual(api_endpoint(base), "https://relay.test/v1/responses")
        self.assertEqual(
            api_endpoint("https://relay.test/api/v1"),
            "https://relay.test/api/v1/responses",
        )
        for config in [
            {"max_images": 0},
            {"max_images": 11},
            {"timeout": "oops"},
            {"max_image_mb": True},
        ]:
            with self.assertRaises(PluginError):
                Settings.from_config(config)

    def test_commands_preserve_spaces_and_parse_count(self):
        self.assertEqual(
            adapter.command_args("/grok from:NASA moon landing --count 3"),
            ("from:NASA moon landing", 3),
        )
        self.assertEqual(adapter.command_args("!grok 白色 猫咪"), ("白色 猫咪", 0))
        for text in ["/grok cat --count nope", "/grok cat --count 0"]:
            with self.assertRaises(PluginError):
                adapter.command_args(text)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_uncited_output_does_not_trigger_tweet_requests(self):
        requests = []

        def handler(req):
            requests.append(req)
            self.assertEqual(req.url.host, "relay.test")
            return httpx.Response(
                200,
                json=search_response(text="https://x.com/a/status/1871234567890123456"),
            )

        service = await self.service(
            handler, base_url="https://relay.test", api_key="secret"
        )
        with self.assertRaises(PluginError) as exc:
            await service.lookup("plana ブルアカ")
        self.assertEqual(exc.exception.code, "search_sources_missing")
        self.assertEqual(len(requests), 1)

    async def test_all_search_candidates_404_is_not_no_photos(self):
        def handler(req):
            if req.url.host == "relay.test":
                return httpx.Response(
                    200,
                    json=search_response(
                        "https://x.com/a/status/123", "https://x.com/a/status/456"
                    ),
                )
            return httpx.Response(404)

        service = await self.service(
            handler, base_url="https://relay.test", api_key="secret"
        )
        with self.assertRaises(PluginError) as exc:
            await service.lookup("plana ブルアカ")
        self.assertEqual(exc.exception.code, "search_candidates_unavailable")
        self.assertIn("2 条推文", str(exc.exception))

    async def test_search_network_failure_preserves_actual_cause(self):
        def handler(req):
            if req.url.host == "relay.test":
                return httpx.Response(
                    200, json=search_response("https://x.com/a/status/123")
                )
            raise httpx.ConnectError("secret proxy detail")

        service = await self.service(
            handler, base_url="https://relay.test", api_key="secret"
        )
        result = await service.lookup("cat")
        self.assertEqual(result.resolved_count, 0)
        self.assertIn("网络", result.warnings[0])
        self.assertNotIn("secret", result.warnings[0])

    async def test_json_404_and_successful_empty_post_are_distinct(self):
        service = await self.service(
            lambda req: httpx.Response(200, json={"code": 404, "tweet": None})
        )
        with self.assertRaises(PluginError) as exc:
            await service.resolve("123")
        self.assertEqual(exc.exception.code, "post_not_found")
        service = await self.service(lambda req: httpx.Response(200, json=tweet()))
        result = await service.lookup("https://x.com/a/status/123")
        self.assertEqual(result.resolved_count, 1)
        self.assertEqual(result.photos, [])

    async def test_generation_uses_api_client_and_standard_image_payload(self):
        requests = []

        def handler(req):
            requests.append(req)
            self.assertEqual(str(req.url), "https://relay.test/v1/images/generations")
            self.assertEqual(req.headers["authorization"], "Bearer secret")
            self.assertEqual(
                json.loads(req.content),
                {
                    "model": "grok-imagine-1.0",
                    "prompt": "a cat",
                    "n": 2,
                    "size": "1024x1024",
                    "response_format": "b64_json",
                },
            )
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"b64_json": base64.b64encode(JPEG).decode()},
                        {"b64_json": base64.b64encode(JPEG).decode()},
                    ]
                },
            )

        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as api,
            httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda req: self.fail("Media request during generation")
                )
            ) as media,
        ):
            service = ImageService(
                Settings(base_url="https://relay.test", api_key="secret"), media, api
            )
            result = await service.generate(" a cat ", 2)
        self.assertEqual(result.images, [JPEG, JPEG])
        self.assertEqual(result.warnings, [])
        self.assertEqual(len(requests), 1)

    async def test_generation_keeps_valid_image_and_reports_bad_base64(self):
        service = await self.service(
            lambda req: httpx.Response(
                200,
                json={
                    "data": [
                        {"b64_json": "not-base64!"},
                        {"b64_json": base64.b64encode(JPEG).decode()},
                    ]
                },
            ),
            base_url="https://relay.test",
            api_key="secret",
        )
        result = await service.generate("cat", 2)
        self.assertEqual(result.images, [JPEG])
        self.assertIn("Base64", result.warnings[0])

    async def test_generation_rejects_missing_key_empty_prompt_bad_size_and_count(self):
        service = await self.service(
            lambda req: self.fail("Invalid input must not make a request")
        )
        for prompt, count in [
            ("", 1),
            ("x" * 4001, 1),
            ("cat", 0),
            ("cat", 11),
            ("cat", True),
            ("cat", 1),
        ]:
            with self.assertRaises(PluginError):
                await service.generate(prompt, count)
        service.settings = Settings(
            api_key="secret", base_url="https://relay.test", image_size="invalid"
        )
        with self.assertRaisesRegex(PluginError, "image_size"):
            await service.generate("cat", 1)

    async def test_generation_rejects_url_only_non_images_and_oversized_images(self):
        for item in [
            {"url": "http://127.0.0.1/private"},
            {"b64_json": base64.b64encode(b"<html>error</html>").decode()},
            {"b64_json": base64.b64encode(JPEG + b"a" * (1024 * 1024)).decode()},
        ]:
            service = await self.service(
                lambda req: httpx.Response(200, json={"data": [item]}),
                api_key="secret",
                base_url="https://relay.test",
                max_image_mb=1,
            )
            result = await service.generate("cat", 1)
            self.assertEqual(result.images, [])
            self.assertTrue(result.warnings)

    async def test_generation_empty_result_or_upstream_error_is_not_retried(self):
        for response in [
            httpx.Response(200, json={"data": []}),
            httpx.Response(429, text="secret"),
            httpx.Response(500, text="secret"),
        ]:
            requests = []

            def handler(req):
                requests.append(req)
                return response

            service = await self.service(
                handler, base_url="https://relay.test", api_key="secret"
            )
            with self.assertRaises(PluginError) as error:
                await service.generate("cat", 1)
            self.assertNotIn("secret", str(error.exception))
            self.assertEqual(len(requests), 1)

    async def test_generation_reports_fewer_results_and_caps_unexpected_extras(self):
        service = await self.service(
            lambda req: httpx.Response(
                200,
                json={
                    "data": [
                        {"b64_json": base64.b64encode(JPEG).decode()},
                    ]
                },
            ),
            base_url="https://relay.test",
            api_key="secret",
        )
        result = await service.generate("cat", 2)
        self.assertEqual(result.images, [JPEG])
        self.assertIn("只返回 1", result.warnings[0])

    async def test_slow_candidate_does_not_lose_resolved_photos(self):
        service = await self.service(lambda req: self.fail("Unexpected HTTP request"))
        cancelled = asyncio.Event()

        async def resolve(sid):
            if sid == "123":
                return [Photo(sid, "NASA", "https://pbs.twimg.com/media/real.jpg")]
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        service.resolve = resolve
        with patch("astrbot_plugin_x_images.service.RESOLVE_BUDGET_SECONDS", 0.01):
            result = await service.lookup(
                "https://x.com/a/status/123 https://x.com/a/status/456"
            )
        self.assertEqual(len(result.photos), 1)
        self.assertIn("超时", result.warnings[0])
        self.assertTrue(cancelled.is_set())

    async def test_search_and_media_use_separate_clients(self):
        search_requests, media_requests = [], []

        def search_handler(req):
            search_requests.append(req)
            self.assertEqual(req.url.host, "relay.test")
            return httpx.Response(
                200,
                json=search_response("https://x.com/a/status/123"),
            )

        def media_handler(req):
            media_requests.append(req)
            self.assertNotIn("authorization", req.headers)
            if req.url.host == "api.fxtwitter.com":
                return httpx.Response(
                    200, json=tweet(photos=["https://pbs.twimg.com/media/a.jpg"])
                )
            return httpx.Response(200, content=JPEG)

        async with (
            httpx.AsyncClient(
                transport=httpx.MockTransport(search_handler)
            ) as api_client,
            httpx.AsyncClient(
                transport=httpx.MockTransport(media_handler)
            ) as media_client,
        ):
            service = ImageService(
                Settings(base_url="https://relay.test", api_key="secret"),
                media_client,
                api_client,
            )
            result = await service.lookup("moon")
            await service.download(result.photos[0])
        self.assertEqual(len(search_requests), 1)
        self.assertEqual(len(media_requests), 2)

    async def service(self, handler, **settings):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        return ImageService(Settings(**settings), client, client)

    async def test_search_resolve_download_contract_and_no_key_leak(self):
        requests = []

        def handler(req):
            requests.append(req)
            if req.url.host == "relay.test":
                payload = json.loads(req.content)
                self.assertEqual(payload["tools"], [{"type": "x_search"}])
                self.assertNotIn("search_parameters", payload)
                self.assertEqual(req.url.path, "/v1/responses")
                self.assertFalse(payload["stream"])
                self.assertEqual(req.headers["authorization"], "Bearer secret")
                return httpx.Response(
                    200,
                    json=search_response(
                        "https://x.com/NASA/status/123",
                        text="https://pbs.twimg.com/media/INVENTED.jpg",
                    ),
                )
            self.assertNotIn("authorization", req.headers)
            if req.url.host == "api.fxtwitter.com":
                return httpx.Response(
                    200, json=tweet(photos=["https://pbs.twimg.com/media/real.jpg"])
                )
            self.assertEqual(req.url.params["name"], "orig")
            return httpx.Response(200, content=JPEG)

        service = await self.service(
            handler, base_url="https://relay.test/v1", api_key="secret"
        )
        result = await service.lookup("moon")
        self.assertEqual(len(result.photos), 1)
        self.assertEqual(await service.download(result.photos[0]), JPEG)
        self.assertEqual(len(requests), 3)
        self.assertFalse(any("INVENTED" in str(req.url) for req in requests))

    async def test_direct_link_needs_no_search_key_and_deduplicates(self):
        def handler(req):
            self.assertEqual(req.url.host, "api.fxtwitter.com")
            return httpx.Response(
                200,
                json=tweet(
                    req.url.path.split("/")[-1],
                    [
                        "https://pbs.twimg.com/media/abc.jpg",
                        "https://pbs.twimg.com/media/abc?format=jpg&name=small",
                    ],
                ),
            )

        service = await self.service(handler)
        result = await service.lookup(
            "https://x.com/a/status/123 https://twitter.com/a/status/456",
            links_only=True,
        )
        self.assertEqual(len(result.photos), 1)
        with self.assertRaises(PluginError):
            await service.lookup("cats", links_only=True)

    async def test_partial_resolve_and_wrong_id(self):
        def handler(req):
            if req.url.path.endswith("/123"):
                return httpx.Response(200, json=tweet("999"))
            return httpx.Response(
                200, json=tweet("456", ["https://pbs.twimg.com/media/real.png"])
            )

        service = await self.service(handler)
        result = await service.lookup(
            "https://x.com/a/status/123 https://x.com/a/status/456"
        )
        self.assertEqual(len(result.photos), 1)
        self.assertIn("不匹配", result.warnings[0])

    async def test_no_photos_does_not_return_video_thumbnails(self):
        response = tweet()
        response["tweet"]["media"] = {
            "videos": [{"thumbnail_url": "https://pbs.twimg.com/media/a.jpg"}]
        }
        service = await self.service(lambda req: httpx.Response(200, json=response))
        self.assertEqual(
            (await service.lookup("https://x.com/a/status/123")).photos, []
        )

    async def test_citation_only_search(self):
        service = await self.service(
            lambda req: httpx.Response(
                200, json=search_response("https://x.com/a/status/123")
            ),
            api_key="secret",
            base_url="https://relay.test",
        )
        self.assertEqual(await service.search("cat"), ["123"])

    async def test_bad_search_response_and_no_results(self):
        for body in [
            {"choices": []},
            {"error": "secret"},
            search_response(text="no results"),
        ]:
            service = await self.service(
                lambda req: httpx.Response(200, json=body),
                api_key="secret",
                base_url="https://relay.test",
            )
            with self.assertRaises(PluginError):
                await service.search("cat")

    async def test_extra_body_cannot_replace_messages(self):
        service = await self.service(
            lambda req: self.fail("Unexpected HTTP request"),
            api_key="secret",
            base_url="https://relay.test",
            extra_body='{"messages": []}',
        )
        with self.assertRaises(PluginError):
            await service.search("cat")

    async def test_timeout_and_http_error_are_sanitized(self):
        for code in [401, 403, 404, 429, 500]:
            service = await self.service(
                lambda req: httpx.Response(code, text="secret body")
            )
            with self.assertRaises(PluginError) as error:
                await service.resolve("123")
            self.assertNotIn("secret", str(error.exception))
            self.assertIn(str(code), str(error.exception))

        def timeout(req):
            raise httpx.ReadTimeout("secret proxy url")

        service = await self.service(timeout)
        with self.assertRaisesRegex(PluginError, "超时"):
            await service.resolve("123")

    async def test_image_size_invalid_content_and_redirect(self):
        photo = Photo("123", "NASA", "https://pbs.twimg.com/media/real.jpg")
        for response in [
            httpx.Response(200, content=b"<html>login</html>"),
            httpx.Response(200, content=JPEG + b"a" * (1024 * 1024)),
            httpx.Response(302, headers={"location": "http://127.0.0.1/private"}),
        ]:
            calls = []

            def handler(req):
                calls.append(req)
                return response

            service = await self.service(handler, max_image_mb=1)
            with self.assertRaises(PluginError):
                await service.download(photo)
            self.assertEqual(len(calls), 1)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolution_failure_is_not_reported_as_no_images(self):
        self.plugin.service.lookup.return_value = Lookup(warnings=["推文不存在（404）"])
        result = await self.plugin._run(
            self.event, "https://x.com/a/status/123", 1, True
        )
        self.assertEqual(result["error_code"], "post_resolution_failed")
        self.assertNotIn("没有静态图片", result["summary"])
        self.assertIn("不能判断", result["summary"])
        self.event.send.assert_not_awaited()

    async def test_verified_no_images_and_search_errors_have_different_codes(self):
        self.plugin.service.lookup.return_value = Lookup(resolved_count=1)
        result = await self.plugin._run(
            self.event, "https://x.com/a/status/123", 1, True
        )
        self.assertEqual(result["error_code"], "posts_have_no_photos")
        self.plugin.service.lookup.side_effect = PluginError(
            "没有搜索来源", code="search_sources_missing"
        )
        result = json.loads(await self.plugin.search_x_images(self.event, "cat", 1))
        self.assertEqual(result["error_code"], "search_sources_missing")

    def setUp(self):
        self.plugin = adapter.Main(None, {})
        self.plugin.settings = Settings(max_images=4)
        self.plugin._client = types.SimpleNamespace(aclose=AsyncMock())
        self.photos = [
            Photo("123", "NASA", f"https://pbs.twimg.com/media/img{i}.jpg")
            for i in range(3)
        ]
        self.plugin.service = types.SimpleNamespace(
            lookup=AsyncMock(return_value=Lookup(self.photos)),
            download=AsyncMock(return_value=JPEG),
            generate=AsyncMock(return_value=GeneratedImages([JPEG])),
        )
        self.event = types.SimpleNamespace(
            send=AsyncMock(), chain_result=lambda chain: chain
        )

    async def test_generation_command_defaults_to_one_and_never_searches(self):
        self.event.plain_result = lambda text: text
        self.event.message_str = "/grok 生图 一只 白猫"
        result = [r async for r in self.plugin.grok_command(self.event)]
        self.assertIn("已发送 1 张生成图片", result[0])
        self.plugin.service.generate.assert_awaited_once_with("一只 白猫", 1)
        self.plugin.service.lookup.assert_not_awaited()
        self.assertIn("Grok 生成图片", self.event.send.await_args.args[0][0][1])

    async def test_generation_tool_partial_send_reports_generated_and_sent_counts(self):
        self.plugin.service.generate.return_value = GeneratedImages([JPEG, JPEG])
        self.event.send.side_effect = [None, RuntimeError("platform secret")]
        result = json.loads(await self.plugin.generate_grok_image(self.event, "cat", 2))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["generated_count"], 2)
        self.assertEqual(result["sent_count"], 1)
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["kind"], "generated")
        self.assertNotIn("secret", json.dumps(result))
        self.plugin.service.generate.assert_awaited_once()

    async def test_generation_count_limit_prevents_api_call(self):
        result = json.loads(await self.plugin.generate_grok_image(self.event, "cat", 5))
        self.assertEqual(result["status"], "error")
        self.plugin.service.generate.assert_not_awaited()

    async def test_unified_command_help_query_and_link(self):
        self.event.plain_result = lambda text: text
        for command in ["/grok", "/grok help", "/grok 帮助"]:
            self.event.message_str = command
            self.assertEqual(
                [r async for r in self.plugin.grok_command(self.event)], [adapter.HELP]
            )
        self.plugin.service.lookup.assert_not_awaited()
        for query in ["moon landing", "https://x.com/a/status/123"]:
            self.event.message_str = f"/grok {query} --count 1"
            result = [r async for r in self.plugin.grok_command(self.event)]
            self.assertIn("已发送 1 张", result[0])
            self.plugin.service.lookup.assert_awaited_with(query, links_only=False)

    async def test_llm_tool_sends_images_and_returns_delivery_receipt(self):
        result = json.loads(await self.plugin.search_x_images(self.event, "moon", 2))
        self.assertEqual(result["sent_count"], 2)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.event.send.await_count, 2)
        self.assertEqual(result["sources"], ["https://x.com/i/status/123"])
        self.assertEqual(self.event.send.await_args.args[0][1], ("image", JPEG))

    async def test_link_tool_does_not_use_search_mode(self):
        result = json.loads(
            await self.plugin.get_x_post_images(
                self.event, "https://x.com/a/status/123", 1
            )
        )
        self.assertEqual(result["sent_count"], 1)
        self.plugin.service.lookup.assert_awaited_once_with(
            "https://x.com/a/status/123", links_only=True
        )

    async def test_send_failure_preserves_partial_count_and_stops(self):
        self.event.send.side_effect = [None, RuntimeError("secret platform error")]
        result = await self.plugin._run(self.event, "moon", 3, False)
        self.assertEqual(result["sent_count"], 1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(self.event.send.await_count, 2)
        self.assertNotIn("secret", json.dumps(result))

    async def test_failed_download_skipped_and_later_image_sent(self):
        self.plugin.service.download.side_effect = [
            PluginError("too large"),
            JPEG,
            JPEG,
        ]
        result = await self.plugin._run(self.event, "moon", 2, False)
        self.assertEqual(result["sent_count"], 2)
        self.assertEqual(result["status"], "partial")

    async def test_bad_counts_and_busy_do_not_call_upstream(self):
        for count in [-1, 5, 1.2, True, float("nan"), "2"]:
            result = await self.plugin._run(self.event, "moon", count, False)
            self.assertEqual(result["sent_count"], 0)
        self.plugin._active = 2
        result = await self.plugin._run(self.event, "moon", 1, False)
        self.assertIn("运行中", result["summary"])
        self.plugin.service.lookup.assert_not_awaited()

    async def test_unload_cancels_inflight_and_closes_client(self):
        entered = asyncio.Event()

        async def slow(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.plugin.service.lookup.side_effect = slow
        task = asyncio.create_task(self.plugin._run(self.event, "moon", 1, False))
        await entered.wait()
        client = self.plugin._client
        await self.plugin.terminate()
        with self.assertRaises(asyncio.CancelledError):
            await task
        client.aclose.assert_awaited_once()
        self.assertEqual(self.plugin._active, 0)


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    """Exercise real httpx proxy transport using local HTTP servers, no external network."""

    async def local_server(self):
        requests = []
        handlers = set()

        async def handler(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            try:
                header = await reader.readuntil(b"\r\n\r\n")
                requests.append(header.split(b"\r\n")[0].decode())
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(task)

        server = await asyncio.start_server(handler, "127.0.0.1", 0)

        async def close():
            server.close()
            await server.wait_closed()
            if handlers:
                await asyncio.gather(*handlers)

        self.addAsyncCleanup(close)
        return server.sockets[0].getsockname()[1], requests

    async def test_http_proxy_and_direct_relay_ignores_environment_proxy(self):
        port, requests = await self.local_server()
        direct_port, direct_requests = await self.local_server()
        plugin = adapter.Main(
            None, {"proxy": f"http://127.0.0.1:{port}", "proxy_api": False}
        )
        # Even environment proxy settings must not override explicitly selected direct relay routing.
        with patch.dict(
            os.environ, {"HTTP_PROXY": f"http://127.0.0.1:{port}", "NO_PROXY": ""}
        ):
            await plugin.initialize()
            try:
                response = await plugin.service.client.get("http://media.invalid/photo")
                self.assertEqual(response.text, "OK")
                response = await plugin.service.api_client.get(
                    f"http://127.0.0.1:{direct_port}/relay"
                )
                self.assertEqual(response.text, "OK")
                self.assertEqual(requests, ["GET http://media.invalid/photo HTTP/1.1"])
                self.assertEqual(direct_requests, ["GET /relay HTTP/1.1"])
                clients = [plugin._client, plugin._api_client]
            finally:
                await plugin.terminate()
        self.assertTrue(all(client.is_closed for client in clients))

    async def test_http_proxy_covers_api_when_enabled(self):
        port, requests = await self.local_server()
        plugin = adapter.Main(None, {"proxy": f"http://127.0.0.1:{port}"})
        await plugin.initialize()
        try:
            self.assertIs(plugin.service.api_client, plugin.service.client)
            response = await plugin.service.api_client.get(
                "http://relay.invalid/search"
            )
            self.assertEqual(response.text, "OK")
            self.assertEqual(requests, ["GET http://relay.invalid/search HTTP/1.1"])
        finally:
            await plugin.terminate()


if __name__ == "__main__":
    unittest.main()
