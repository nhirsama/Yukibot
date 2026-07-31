# Forwarder feature

这是一个不依赖 Telegram SDK、数据库驱动和应用主框架的可复用转发模块。它参考了
[`alpersamur3/telegram-forwarder`](https://github.com/alpersamur3/telegram-forwarder)
（MIT License）的功能行为，但重新定义了模块边界，没有复制其 Pyrogram handler 结构。

## 已抽象的行为

- 来源 chat/topic 到目标 chat/topic 的多路由匹配；
- 不区分大小写的关键词过滤；
- 内容类型白名单、黑名单及服务消息开关；
- native forward 和 copy 模式；
- native forward 受限时按路由回退到 copy；
- 回复链 source/destination message ID 映射；
- 相册按 `(chat_id, media_group_id)` 缓冲、排序并整体发送；
- 编辑与删除同步；
- 缺少来源 chat 的删除事件默认不做危险的模糊匹配；
- 服务消息规范化为纯文本；
- chat 级路由图循环检测；
- 可供 Telegram gateway 使用的滑动窗口限流器。

## 模块边界

```text
Telethon adapter
        |
        | IncomingMessage / TelegramGateway
        v
Forwarder / ForwarderService
        |
        +-- RouteRepository
        +-- MessageLinkRepository
```

核心模块不会创建 Telethon client、读取环境变量、注册事件 handler、处理管理员命令或决定数据库。
现有 `feature.py`、`repository.py` 和全局组合根在核心边界之外完成这些接入工作。

`TelegramGateway` 是功能本地的 Protocol。适配器应完成以下转换：

- Telethon new/edit/delete update -> `IncomingMessage` / `MessagesDeleted`；
- Telethon FloodWait -> `RetryAfter`；
- 原生转发受限 -> `NativeForwardUnsupported`；
- 消息不存在 -> `MessageNotFound`；
- 内容未变化 -> `MessageNotModified`。

格式化实体和媒体不被复制到领域模型。gateway 收到稳定的 `MessageRef` 后，应使用 Telethon 读取、
复制或转发原始消息，从而完整保留 Telegram 能力且不污染业务边界。

## 最小用法

```python
from datetime import UTC, datetime

from yukibot.features.forwarder import (
    ContentType,
    DestinationEndpoint,
    Forwarder,
    ForwarderService,
    IncomingMessage,
    InMemoryMessageLinkRepository,
    InMemoryRouteRepository,
    MessageRef,
    Route,
    SourceEndpoint,
)

routes = InMemoryRouteRepository(
    [
        Route(
            id=1,
            source=SourceEndpoint(-100100),
            destination=DestinationEndpoint(-100200, topic_id=10),
        )
    ]
)
links = InMemoryMessageLinkRepository()

# gateway 是满足 TelegramGateway Protocol 的 Telethon adapter。
service = ForwarderService(routes, links, gateway)
forwarder = Forwarder(service)

await forwarder.handle_message(
    IncomingMessage(
        ref=MessageRef(-100100, 42),
        content_type=ContentType.TEXT,
        text="hello",
        occurred_at=datetime.now(UTC),
    )
)
await forwarder.close()
```

## 持久化要求

`InMemoryRouteRepository` 和 `InMemoryMessageLinkRepository` 只用于测试、开发或短生命周期嵌入。
生产运行使用 `SqliteRouteRepository` 和 `SqliteMessageLinkRepository` 持久化路由与消息映射。

`MessageLinkRepository.save_many()` 应实现幂等 upsert。`ForwardingReport.failures` 保留原始应用级
异常，未来的 durable job worker 可以据此区分重试、永久失败和 `RetryAfter`。持久任务表、租约和
崩溃恢复尚未在本阶段实现，它们属于 forwarder 的下一层运行适配器，而不是核心转发策略。

## 明确排除

- Pyrogram 兼容层；
- Pyrogram 事件注册；
- JSON 路由文件；
- `/add`、`/remove`、`/status` 等展示层命令；
- 全局可变单例。
