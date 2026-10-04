# Grok 图片助手

AstrBot 插件，支持 X/Twitter 图片搜索、推文链接提图和 Grok 文生图，图片直接发送到当前聊天。提供三个 LLM tools 和 HTTP 代理配置。

## 安装与配置

要求 AstrBot `>=4.13.0,<5`。在插件管理中上传 ZIP 安装；也可将目录放到 `AstrBot/data/plugins/astrbot_plugin_x_images`，在 AstrBot 的 Python 环境中执行 `python -m pip install -r requirements.txt`，随后重启或重载插件。

在配置面板填写中转 `base_url` 和 `api_key`：

- 搜图使用 `model`，默认 `grok-4.6`，须支持 Responses API 的服务端 `x_search`，并返回来源引用。
- 生图使用 `image_model`，默认 `grok-imagine-1.0`，须支持 Images API 和 `b64_json` 格式。
- `image_size` 设置生图尺寸，默认 `1024x1024`，以中转模型支持的尺寸为准。
- 仅使用推文链接提图无需填写 API Key。

模型名以你的中转实际提供的名称为准。保存配置后重载插件。

## 命令

```text
/grok 猫咪 摄影 --count 3
/grok from:NASA 月球 --count 2
/grok https://x.com/用户名/status/推文ID
/grok 生图 一只白猫坐在窗边，水彩画风格
/grok 生图 雨夜的未来城市 --count 2
/grok help
```

`/grok` 无参数或 `/grok 帮助` 也会显示帮助。`生图` 后要用空格分隔提示词；其他输入自动识别为关键词搜索或推文链接提图。

`--count` 放在末尾，按图片张数计算。搜图默认最多 4 张，生图默认 1 张；所有功能的数量上限由 `max_images` 控制，可设为 1–10。多个推文链接以空格分隔，最多解析 12 条。链接提图不调用 Grok。

## LLM tools

在 AstrBot 工具管理中启用以下工具，并使用支持工具调用的聊天模型和 Agent runner。

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `search_x_images` | `query`, `count=0` | 按关键词或账号找已有推特图片；传链接则直接提图 |
| `get_x_post_images` | `url`, `count=0` | 提取指定推文图片 |
| `generate_grok_image` | `prompt`, `count=1` | 根据提示词生成新图片 |

例如对机器人说「找 3 张 NASA 发的月球图片」，或「生成一张水彩风格的白猫图片」。搜索工具的 `count=0` 使用搜图默认数量，生图默认 1 张。

工具自行发送图片，再返回 JSON 回执：`kind`（`twitter` 或 `generated`）、`status`、`sent_count`、`sources`、`warnings`、`summary`。生图成功返回结果时另有 `generated_count`；即使生成成功，平台发送失败时也不会虚报已发送。生成图片明确标注「Grok 生成图片」，不会冒充推特原图或附加虚构推文来源。

生图会消耗中转额度，不自动重试付费请求。发送失败后应先检查平台连接，再决定是否重新生成。LLM 不应重复发图或在失败后自动重复付费调用。

整个任务最长 150 秒，建议 AstrBot 工具执行超时设为至少 180 秒。

## HTTP 代理

`proxy` 填写 HTTP 代理，例如：

```text
http://127.0.0.1:7890
```

端口以代理软件的 HTTP/Mixed 端口配置为准。HTTP 代理可通过 CONNECT 访问 HTTPS 接口；这里的地址仍填写 `http://`。留空时使用 httpx 的环境代理设置（环境代理也应设置为 HTTP 代理地址）。

`proxy_api=true` 时，搜索、生图、推文解析和图片下载共用代理设置。国内中转需要直连时，关闭「Grok 中转请求也使用代理」：搜索和生图忽略配置及环境代理，推文解析、图片下载仍使用 `proxy`。

Docker Desktop 可按实际网络使用 `http://host.docker.internal:7890`。Linux Docker 需配置宿主机网关映射或填写可达的局域网地址，并允许容器连接代理。容器内的 `127.0.0.1` 指容器自身。

## 配置项

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `base_url` | 空 | 中转域名或以 `/v1` 结尾的基础地址 |
| `api_key` | 空 | 搜索和生图使用的中转密钥 |
| `model` | `grok-4.6` | 联网搜索模型 |
| `extra_body` | `{}` | 仅用于搜索的额外 JSON 参数 |
| `image_model` | `grok-imagine-1.0` | 文生图模型 |
| `image_size` | `1024x1024` | 文生图尺寸，宽x高 |
| `max_images` | 4 | 搜图默认数量及所有功能的数量上限，1–10 |
| `timeout` | 120 | 搜索及推文接口超时秒数，10–180 |
| `max_image_mb` | 10 | 单张图片大小上限，1–20 MB |
| `proxy` | 空 | HTTP 代理地址 |
| `proxy_api` | true | 搜索和生图使用代理；false 为中转强制直连 |

## 中转接口

只填写域名时会补 `/v1`。不要在 `base_url` 中填写完整的 `/responses` 或 `/images/generations` 地址。

搜索使用 `POST /v1/responses`，显式发送 `tools: [{"type":"x_search"}]`、`input` 和 `stream:false`，由上游执行真实的 X 搜索。只从 Responses 的 `output[].content[].annotations[]` 中读取 `url_citation` 推文来源，随后逐条通过 FxTwitter 核验。正文和推理文本中的链接不作为搜索依据。

v1.3.0 删除了原先的 Chat Completions / `search_parameters` 搜索路径。New API 或模型能进行普通对话，不等于会执行服务端搜索；只返回 `<tool_call>` 文本或没有来源引用时，插件会明确报搜索错误，不再把生成的链接逐条当作搜索结果访问。`extra_body` 只允许追加 Responses 参数，不能覆盖 `model/input/tools/stream/tool_choice/include`，也不能携带旧 `messages/search_parameters`。通常保持 `{}` 即可。

升级后请检查：模型使用实际支持 X 搜索的名称（已实测 `grok-4.6`），`extra_body` 保持 `{}`，`timeout` 建议设为 120；内网中转可将 `proxy_api` 关闭，使中转请求直连，推文和图片仍走 HTTP 代理。

错误区分：`search_protocol_error` 是接口格式不符，`search_tool_not_executed` 是只返回工具调用而没有来源，`search_sources_missing` 是缺少可核验的推文引用，`search_candidates_unavailable` 是引用候选全部无法访问。`post_resolution_failed` 表示未解析出任何推文，不能据此声称推文没有图片；只有成功解析后无静态图才返回 `posts_have_no_photos`。

生图使用 `POST /v1/images/generations`，请求示例：

```json
{
  "model": "grok-imagine-1.0",
  "prompt": "一只白猫坐在窗边，水彩画风格",
  "n": 1,
  "size": "1024x1024",
  "response_format": "b64_json"
}
```

生图读取标准 `data[].b64_json` 返回值，验证 Base64、图片字节类型和大小后直接发送。若中转仅返回 URL，会明确提示不支持当前返回格式；不会自动改协议重试。单次生图请求超时 120 秒，整个响应最多 64 MB。单张图片仍受 `max_image_mb` 限制，上游少返回图片或部分图片无效时会报告实际结果。

本插件按当前中转需求实现，不自动切换模型或接口。暂不支持图生图、图片编辑和视频生成。

## 推特图片来源

搜索从服务端来源引用中取得候选推文链接，不采用模型正文或推理文本中的链接。随后调用 [FxTwitter/FxEmbed](https://docs.fxembed.com/api/introduction) 的 `https://api.fxtwitter.com/status/{id}`，核对推文 ID 并读取 `tweet.media.photos`。仅下载 `https://pbs.twimg.com/media/…` 上的原图，检查大小及 JPEG/PNG/WebP 文件头，不跟随重定向。图片附作者和推文来源，插件不保存磁盘缓存。

账号、时间、主题条件交由 Grok 搜索，相关性和时效性依赖中转模型；插件核验推文和媒体是否存在，不保证自然语言筛选条件完全准确，也不抓取账号完整时间线。只提取推文本身的静态照片，不提取头像、视频封面、GIF 或引用推文图片，不绕过私密推文权限。解析服务不可用时会报告失败。

关键词和生图提示词发送给配置的中转，待解析的推文 ID 发送给 FxTwitter。API Key 仅附加到中转请求，不发送给 FxTwitter 或图片 CDN。

同时最多运行两个图片任务。推文解析最多并发 3 条，最多等待 30 秒，保留成功结果并取消超时候选。下载失败时尝试后续候选，最多尝试 `min(请求张数 + 4, 14)` 次下载。平台发送失败则停止发送并保留已发送计数。

## 验证

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

2026-10-05，v1.3.0 的 46 项测试通过，覆盖 Responses X 搜索、真实响应格式、无引用的伪造链接拦截、未执行工具、404 与无图的区别，以及提图、生图、代理和发送回执。HTTP 使用 MockTransport，代理测试使用临时本地服务器，AstrBot 发送接口使用测试桩。Ruff 检查和格式检查通过。

已使用 New API 中转的 `grok-4.6` 实测关键词「plana ブルアカ」：服务端执行了 3 次 X 搜索，返回 2 条带引用的公开推文，成功解析并下载 2 张原图（322,053 和 336,111 字节）。已知推文链接提图也通过实测。生图和聊天平台发送尚未实际联调；离线测试不消耗 API 额度。

开发资料：[AstrBot 插件规范](https://docs.astrbot.app/dev/star/plugin-new.html)、[xAI 文档](https://docs.x.ai/overview)、[Grok 接口参考项目](https://github.com/muqing-kg/astrbot_plugin_grok_suite)、[FxEmbed](https://github.com/FxEmbed/FxEmbed)。
