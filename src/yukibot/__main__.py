"""Command-line entry point."""

from __future__ import annotations

import asyncio
import logging

from pydantic import ValidationError

from yukibot.adapters.observability import configure_logging
from yukibot.bootstrap import build_application
from yukibot.config import Settings


def main() -> None:
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as error:
        raise SystemExit(f"invalid configuration:\n{error}") from error

    configure_logging(settings.log_level)
    try:
        asyncio.run(build_application(settings).run())
    except KeyboardInterrupt:
        return
    except Exception:
        logging.getLogger(__name__).exception("application terminated with an error")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
