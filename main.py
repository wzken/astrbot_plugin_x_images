import asyncio
import json
import math
import re

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star

from .service import ImageService, PluginError, Settings

HELP = """Grok 图片助手
/grok 关键词或 from:账号 [--count 4]
/grok 推文链接 [--count 4]
/grok 生图 提示词 [--count 1]
/grok help 查看帮助
例如：/grok from:NASA 月球 --count 3
也可以让 AI「帮我找几张推特上的猫咪图片」。
数量按图片张数计算，上限由插件配置决定。
搜索和生图需配置相应的 Grok 中转模型；链接提图无需 API Key。
生图默认 1 张，尺寸在插件配置中设置。"""


def command_args(message: str) -> tuple[str, int]:
    match = re.search(r"(?<!\w)grok(?:\s+|$)", message)
    text = message[match.end() :].strip() if match else ""
    count = 0
    if "--count" in text:
        option = re.search(r"(?:^|\s)--count\s+(\d+)\s*$", text)
        if not option:
            raise PluginError("数量格式：在末尾填写 --count 3。")
        if len(option[1]) > 2:
            raise PluginError("图片数量不能超过 10。")
        count = int(option[1])
        if count < 1:
            raise PluginError("图片数量必须大于 0。")
        text = text[: option.start()].strip()
    return text, count


class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._client = None
        self._api_client = None
        self._tasks: set[asyncio.Task] = set()
        self._active = 0

    async def initialize(self):
        self.settings = Settings.from_config(self.config)
        try:
            self._client = httpx.AsyncClient(
                proxy=self.settings.proxy or None,
                timeout=self.settings.timeout,
                follow_redirects=False,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                headers={"User-Agent": "AstrBot-X-Images/1.3"},
            )
            self._api_client = (
                self._client
                if self.settings.proxy_api
                else httpx.AsyncClient(
                    timeout=self.settings.timeout,
                    trust_env=False,
                    follow_redirects=False,
                    limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
                    headers={"User-Agent": "AstrBot-X-Images/1.3"},
                )
            )
        except (ValueError, ImportError):
            if self._client:
                await self._client.aclose()
                self._client = None
            raise PluginError(
                "HTTP 代理初始化失败，请检查 http://主机:端口 地址。"
            ) from None
        self.service = ImageService(self.settings, self._client, self._api_client)

    async def terminate(self):
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._client:
            await self._client.aclose()
        if self._api_client and self._api_client is not self._client:
            await self._api_client.aclose()
        self._client = None
        self._api_client = None

    @filter.command("grok")
    async def grok_command(self, event: AstrMessageEvent):
        yield event.plain_result(await self._command(event))

    async def _command(self, event):
        try:
            query, count = command_args(event.message_str)
        except PluginError as exc:
            return str(exc)
        if not query or query.lower() in {"help", "帮助"}:
            return HELP
        parts = query.split(maxsplit=1)
        generate = parts[0] == "生图"
        if generate:
            query = parts[1] if len(parts) == 2 else ""
        report = await self._run(event, query, count, False, generate=generate)
        return report["summary"]

    @filter.llm_tool(name="generate_grok_image")
    async def generate_grok_image(
        self, event: AstrMessageEvent, prompt: str, count: int = 1
    ) -> str:
        """根据提示词用 Grok 生成新图片并直接发送到当前聊天。

        用户明确要求生成、绘制图片时使用；找推特上已有图片应使用 search_x_images。
        调用会消耗中转生图额度。返回实际发送数量，勿重复发图，失败时勿自动重复付费调用。

        Args:
            prompt(string): 描述要生成的图片内容、风格和构图，最多 4000 字符。
            count(number): 图片数量，默认 1，不能超过插件配置上限。
        """
        return json.dumps(
            await self._run(event, prompt, count, False, generate=True),
            ensure_ascii=False,
        )

    @filter.llm_tool(name="search_x_images")
    async def search_x_images(
        self, event: AstrMessageEvent, query: str, count: int = 0
    ) -> str:
        """搜索 X/Twitter 上的真实图片并直接发送到当前聊天，不生成图片。

        用户想找推特图片、某账号图片时使用。query 可含关键词、from:账号或完整推文链接。
        返回发送状态和真实来源；只有 sent_count 大于零才表示图片已发送，不要重复发送图片。
        error_code 以 search_ 开头时说明搜索结果或搜索协议有问题，不应笼统称为推特图片接口故障。

        Args:
            query(string): 搜索关键词、from:账号、日期要求或完整推文链接。
            count(number): 期望发送的图片张数，0 使用插件配置默认值，不能超过配置上限。
        """
        return json.dumps(
            await self._run(event, query, count, False), ensure_ascii=False
        )

    @filter.llm_tool(name="get_x_post_images")
    async def get_x_post_images(
        self, event: AstrMessageEvent, url: str, count: int = 0
    ) -> str:
        """提取指定 X/Twitter 推文的真实图片并直接发送到当前聊天，无需 Grok 搜索。

        用于用户提供推文链接要求看图、提图或下载图片。返回发送状态和来源，请勿重复发送图片。

        Args:
            url(string): 完整 x.com 或 twitter.com 推文链接；多个链接可用空格分隔。
            count(number): 期望发送的图片张数，0 使用插件配置默认值，不能超过配置上限。
        """
        return json.dumps(await self._run(event, url, count, True), ensure_ascii=False)

    async def _run(self, event, query, count, links_only, *, generate=False):
        report = {
            "kind": "generated" if generate else "twitter",
            "status": "error",
            "sent_count": 0,
            "sources": [],
            "warnings": [],
            "summary": "",
        }
        if self._active >= 2:
            report["summary"] = "已有两个图片任务运行中，请稍后重试。"
            return report
        self._active += 1
        task = None
        try:
            if not self._client:
                raise PluginError("插件尚未初始化，请检查插件配置后重载。")
            if not isinstance(query, str):
                raise PluginError("关键词、链接或生图提示词必须是文本。")
            if (
                isinstance(count, bool)
                or not isinstance(count, (int, float))
                or not math.isfinite(count)
                or count != int(count)
            ):
                raise PluginError("图片数量必须是整数。")
            count = int(count)
            if not 0 <= count <= self.settings.max_images:
                raise PluginError(
                    f"图片数量必须在 1–{self.settings.max_images} 之间，或用 0 表示默认值。"
                )
            count = count or (1 if generate else self.settings.max_images)
            report["requested_count"] = count
            task = asyncio.create_task(
                self._deliver_generated(event, query, count, report)
                if generate
                else self._deliver(event, query, count, links_only, report)
            )
            self._tasks.add(task)
            await asyncio.wait_for(task, timeout=150)
        except PluginError as exc:
            report["error_code"] = exc.code
            report["warnings"].append(str(exc))
        except asyncio.TimeoutError:
            report["warnings"].append("本次任务超过 150 秒，已停止后续处理。")
        except Exception as exc:
            # Do not log exception text: transport/platform errors may contain credentials or base64 images.
            logger.error(f"X 图片插件发生异常 ({type(exc).__name__})")
            report["warnings"].append("处理失败，请检查插件配置及平台发送日志。")
        finally:
            self._active -= 1
            if task is not None:
                self._tasks.discard(task)
        sent = report["sent_count"]
        report["status"] = (
            "partial" if sent and report["warnings"] else "ok" if sent else "error"
        )
        label = "生成图片" if generate else "推特图片"
        report["summary"] = f"已发送 {sent} 张{label}。" if sent else "没有发送图片。"
        if report["warnings"]:
            report["summary"] += "\n" + "\n".join(report["warnings"][:5])
        return report

    async def _deliver_generated(self, event, prompt, count, report):
        result = await self.service.generate(prompt, count)
        report["warnings"].extend(result.warnings)
        report["generated_count"] = len(result.images)
        for index, raw in enumerate(result.images, 1):
            if not await self._send_image(
                event, raw, f"Grok 生成图片 {index}/{len(result.images)}\n", report
            ):
                break

    async def _send_image(self, event, raw, caption, report):
        try:
            await event.send(event.chain_result([Plain(caption), Image.fromBytes(raw)]))
        except Exception as exc:
            logger.error(f"Grok 图片发送失败 ({type(exc).__name__})")
            report["warnings"].append(
                "平台发送图片失败，已停止发送；请检查机器人平台连接。"
            )
            return False
        report["sent_count"] += 1
        return True

    async def _deliver(self, event, query, count, links_only, report):
        lookup = await self.service.lookup(query, links_only=links_only)
        report["warnings"].extend(lookup.warnings)
        report["resolved_count"] = lookup.resolved_count
        if not lookup.photos:
            if lookup.resolved_count:
                report["warnings"].append(
                    "已解析的推文中没有静态图片；视频、GIF 和引用推文图片不在提取范围内。"
                )
                report["error_code"] = "posts_have_no_photos"
            else:
                report["warnings"].insert(
                    0,
                    "未能解析任何推文，不能判断是否含图片；请查看具体的链接或网络错误。",
                )
                report["error_code"] = "post_resolution_failed"
            return
        report["available_count"] = len(lookup.photos)
        # Bound download attempts too, even when candidates fail.
        for photo in lookup.photos[: min(count + 4, 14)]:
            if report["sent_count"] >= count:
                break
            try:
                raw = await self.service.download(photo)
            except PluginError as exc:
                report["warnings"].append(f"{photo.source}：{exc}")
                continue
            if not await self._send_image(
                event, raw, f"@{photo.author}\n{photo.source}\n", report
            ):
                break
            if photo.source not in report["sources"]:
                report["sources"].append(photo.source)
