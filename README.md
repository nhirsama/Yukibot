# Yukibot

Yukibot 是一个基于 Python 3.12、Telethon v2 和 SQLite 的模块化 Telegram userbot。
当前实现包含：

- 与 Telegram 无关的事件总线、任务监管、生命周期和关闭协调；
- 不可变 Telegram/数据库契约；
- 环境配置和 JSON 结构化日志；
- SQLite 事务、按功能迁移与 Forwarder 持久仓储；
- Telethon v2 event source 和 Forwarder gateway；
- 显式组合根与 `yukibot` CLI；
- Forwarder 功能及其框架接入层。

整体边界见 [`docs/architecture.md`](docs/architecture.md)，Forwarder 说明见
[`src/yukibot/features/forwarder/README.md`](src/yukibot/features/forwarder/README.md)。

## Run

```bash
cp .env.example .env
# 填写 Telegram API ID 和 API hash
uv sync --frozen
uv run yukibot
```

首次启动且 session 尚未登录时，Telethon 会执行交互式登录。动态转发路由保存在
`forwarder_routes` 表中；当前尚未提供 Telegram 管理命令，可以通过仓储 API 或运维脚本配置。

可靠 `ForwardJob`、租约、崩溃恢复和管理命令属于后续 Forwarder 运行层，当前版本不宣称提供。

## Development

```bash
uv sync --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```
