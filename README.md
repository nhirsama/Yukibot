# Yukibot

Yukibot is currently a collection of reusable userbot feature modules. The application kernel
and the Telethon adapter have intentionally not been implemented yet.

The first module is documented in
[`src/yukibot/features/forwarder/README.md`](src/yukibot/features/forwarder/README.md).
The planned application architecture is documented in
[`docs/architecture.md`](docs/architecture.md).

## Development

```bash
uv sync --dev
uv run pytest
uv run ruff check .
uv run mypy
```

