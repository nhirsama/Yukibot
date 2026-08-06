"""Errors exposed by the summarizer application layer."""


class SummarizerError(RuntimeError):
    """Base class for expected summarizer failures."""


class SummaryRuleNotFoundError(SummarizerError):
    """The requested summary rule does not exist."""


class SummaryModelUnavailableError(SummarizerError):
    """The configured model cannot currently generate a summary."""


class NoMessagesToSummarizeError(SummarizerError):
    """The selected source window contains no useful messages."""
