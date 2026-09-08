# Browser Publisher

Standalone automated browser publishing service for WeChat Official Accounts (微信公众号) and Xiaohongshu (小红书).

## 核心架构与边界

- **独立运行**：作为无状态发布引擎独立运行，与调用方（如 Notify Hub）通过 HTTP REST API 通信。
- **共享环境**：微信公众号与小红书共用同一个 Chromium 持久化 Profile 和单个内部串行 Worker。
- **可靠状态机**：基于 SQLite + WAL 的任务队列持久化，支持断点续发、防重复点击、扫码等待恢复与风控熔断保护。
- **管理控制台**：服务端轻量级 Web 控制台与 `publisher-cli` 命令行工具。

## 默认服务端口与地址

- 局域网默认地址：`http://192.168.31.100:8790`
- 内部数据目录：`/app/data`
