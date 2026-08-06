"""Infrastructure adapters owned by the summarizer feature."""

from .openai_model import OpenAISummaryGenerator
from .telethon_gateway import TelethonSummaryGateway

__all__ = ["OpenAISummaryGenerator", "TelethonSummaryGateway"]
