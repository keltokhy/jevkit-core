"""Errors shared by all JevKit client adapters."""


class JevError(Exception):
    """One request failed; the rest of the run may continue."""


class JevFatal(Exception):
    """Stop the run until configuration, credentials, or metering is corrected."""


class JevBudgetExceeded(Exception):
    """No cached or in-flight answer exists and a new paid request is forbidden."""


class RequestExhausted(JevError):
    """Structured exhaustion details let each CLI retain its error wording."""

    def __init__(self, timeout: float, last: str, *, timed_out: bool = False):
        self.timeout, self.last = timeout, last
        self.timed_out = timed_out
        super().__init__(f"gave up after {timeout:g}s ({last})")
