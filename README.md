# Browser Publisher

Standalone automated browser publishing service for WeChat Official Accounts (微信公众号) and Xiaohongshu (小红书).

## 核心架构与边界

- **独立运行**：作为无状态发布引擎独立运行，与调用方（如 Notify Hub）通过 HTTP REST API 通信。
- **共享环境**：微信公众号与小红书共用同一个 Chromium 持久化 Profile 和单个内部串行 Worker。
- **可靠状态机**：基于 SQLite + WAL 的任务队列持久化，支持断点续发、防重复点击、人工确认等待恢复与风控熔断保护。
- **管理控制台**：服务端轻量级 Web 控制台与 `publisher-cli` 命令行工具。

## 默认服务端口与地址

- 局域网默认地址：`http://192.168.31.100:8790`
- 内部数据目录：`/app/data`

## 微信公众号发布边界

Browser Publisher 是公众号发布的执行网关：在配置 `PUBLISHER_WECHAT_MP_APP_ID` / `PUBLISHER_WECHAT_MP_APP_SECRET` 时，它自己通过官方 API 上传封面并创建完整草稿，然后由 Playwright 在公众号后台找到该草稿、发起最后的「发表」动作并核对结果。Notify Hub 只提交文章任务、保存调度历史，不持有或调用公众号 API。

公众号网页登录态失效时，已配置官方 API 凭据的任务仍先创建草稿。`draft` 模式到此完成；`publish` 模式保留草稿检查点并等待扫码，登录恢复后只继续最终发表。未配置官方 API 凭据时，旧版浏览器编辑器仍需要登录后才能建草稿。

未配置 Browser Publisher 的公众号 API 凭据时，保留旧的 Playwright 编辑器路径作为兼容兜底；该路径会在浏览器中编辑正文、上传图片和保存草稿。迁移到官方 API 后，生产环境应把公众号 AppID/Secret 放在 Browser Publisher 的环境变量中，而不是依赖 Notify Hub 的旧 `NOTIFY_HUB_MP_*` 配置。

官方 API 草稿请求固定开启 `need_open_comment=1`，并允许非仅粉丝留言；后台「群发通知 / 发送群通知」选项在发表前固定关闭。若微信要求管理员人工确认，服务只发送文字告警并进入 `waiting_manual_confirm`，之后只轮询发表结果，绝不再次点击「发表」。

登录态失效时仍保留登录二维码捕获；发表确认场景不截图、不转发二维码。

如果公众号 API 只能通过已配置 IP 白名单的受信任代理访问，将 `PUBLISHER_WECHAT_MP_API_BASE_URL` 指向该代理的 HTTPS 入口（包括必要的路径前缀）；不要在任务请求中使用任意代理。

## 图文任务与正文图片

调用方先用 `POST /v1/media` 的 multipart `file` 字段上传封面和正文图片，再用 `POST /v1/jobs` 提交 JSON 任务。`media` 数组按顺序放置封面和正文图片的 `media_id`；正文中的正文图片引用使用 `publisher-media://<media_id>`。Notify Hub 的 `browser` 模式会自动完成 HTTP 正文图片的下载、上传和 marker 替换；直接调用本服务时使用随附的 `wechat-mp-publisher` Skill。

`body_text` 可以继续传 Markdown/纯文本，`body_html` 可以传 HTML。Browser Publisher 只对 `publisher-media://` 做精确替换：正文图片会调用微信 `media/uploadimg`，再把返回 URL 写入最终的 `draft/add` HTML；不会从任意 HTML 中猜测本地路径，也不会把二进制图片塞进 JSON。微信公众号正文图片限 JPG/PNG、每张不超过 1 MiB；封面沿用 JPG/PNG/WebP、20 MiB 限制。

例如 HTML 正文可以是：

```html
<p>正文。</p>
<img src="publisher-media://med_inline_1" alt="正文配图">
```
