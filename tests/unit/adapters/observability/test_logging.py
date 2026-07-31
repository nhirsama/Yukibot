import io
import json
import logging

from yukibot.adapters.observability import configure_logging


def test_json_logging_includes_context_and_redacts_secrets() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)

    logging.getLogger("test").info(
        "operation complete",
        extra={"feature": "forwarder", "api_hash": "do-not-log"},
    )

    record = json.loads(stream.getvalue())
    assert record["level"] == "INFO"
    assert record["logger"] == "test"
    assert record["message"] == "operation complete"
    assert record["feature"] == "forwarder"
    assert record["api_hash"] == "[redacted]"
    assert "do-not-log" not in stream.getvalue()
