# Yukibot

Yukibot 是一个基于 Python 3.12、Telethon 1.44 和 SQLite 的模块化 Telegram userbot。
当前实现包含：

- 与 Telegram 无关的事件总线、任务监管、生命周期和关闭协调；
- 不可变 Telegram/数据库契约；
- 环境配置和 JSON 结构化日志；
- SQLite 事务、按功能迁移、Forwarder 持久任务与崩溃恢复；
- 可排空的 Telethon event source 和 Forwarder 自有 gateway；
- 显式组合根与 `yukibot` CLI；
- 带外管理命令、SQLite 管理员与运行时模块开关；
- Forwarder 功能及其框架接入层和路由管理命令。

整体边界见 [`docs/architecture.md`](docs/architecture.md)，Forwarder 说明见
[`src/yukibot/features/forwarder/README.md`](src/yukibot/features/forwarder/README.md)。

## Run

```bash
cp .env.example .env
# 填写 Telegram API ID 和 API hash
uv sync --frozen
uv run yukibot
```

首次启动且 session 尚未登录时，Telethon 会执行交互式登录。登录完成后，当前账号可以在任意
聊天中发送已注册命令，结果会回复到同一聊天的原消息。命令是普通消息处理之外的带外控制信令，
不会进入 Forwarder；未注册的 `/xxx` 仍按普通消息处理。

框架直接提供：

```text
/help
/help /route
/help /admin
```

独立管理模块提供：

```text
/admin admin list
/admin admin add <telegram_user_id>
/admin admin remove <telegram_user_id>
/admin module list
/admin module enable forwarder
/admin module disable forwarder
```

只有当前登录账号发出的命令可以增删管理员。额外管理员使用稳定的 Telegram user ID 存储在
SQLite 中，可以执行其他已注册命令。管理模块本身始终保持可用，不属于可关闭模块。

Forwarder 提供：

```text
/route list
/route show <id>
/route add <id> <source_chat> <destination_chat> [forward|copy] [source_topic|-] [destination_topic|-]
/route set <id> <source_chat> <destination_chat> [forward|copy] [source_topic|-] [destination_topic|-]
/route enable <id>
/route disable <id>
/route remove <id>
```

例如：

```text
/route add 1 -1001234567890 -1009876543210
```

路由默认使用 Telegram 原生转发；来源禁止转发或当前操作无法原生转发时自动回退为复制。目标是
论坛超级群且没有指定 `destination_topic` 时，Yukibot 会创建一个与源频道同名的话题并保存映射；
源频道改名后，话题名会同步更新。多条相同“源频道 -> 目标论坛群”路由复用同一个自动话题。
账号需要在目标群拥有创建和管理话题的权限。

要使用已有话题，可以显式传入话题 ID：

```text
/route add 2 -1001234567890 -1009876543210 forward - 12345
```

目标不是论坛群时，省略 `destination_topic` 表示直接发送到该群。显式使用 `copy` 可以始终复制
消息内容，不保留 Telegram 的“转发自”标记。

动态转发路由保存在 `forwarder_routes` 表中。路由 ID 必须显式指定；重复执行相同的 `add`、
`enable`、`disable` 或 `remove` 是幂等的，修改已有路由使用 `set`。

Forwarder handler 只将事件幂等写入 `forwarder_jobs`，由单个受监管 worker 按任务顺序发送。
进程中断时，处于 `processing` 的任务会在下次启动恢复为 `pending`。严格 exactly-once 仍受
Telegram 发送与本地映射落库之间无法建立跨系统事务的限制。

## Development

```bash
uv sync --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```
