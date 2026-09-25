"""Failure types shared by every JevKit tool."""


class JevError(Exception):
    """One request failed; the rest of the run may continue."""


class JevFatal(Exception):
    """Stop the run until configuration, credentials, or metering is corrected."""


class JevBudgetExceeded(Exception):
    """A new request does not fit in the budget; no stored or in-flight answer could stand in for it."""


class ProviderStatus(Exception):
    """An HTTP status the provider answered with, kept structured so tools can word or redact it."""

    def __init__(self, provider: str, status: int, detail: str):
        self.provider, self.status, self.detail = provider, status, detail
        super().__init__(self.message())

    def message(self) -> str:
        return f"HTTP {self.status}: {self.detail}"


class ProviderError(ProviderStatus, JevError):
    """A status that is neither retryable nor a reason to stop the run, such as a bad request."""


class ProviderFatal(ProviderStatus, JevFatal):
    """Authentication, payment, or permission failed; no further request can succeed."""

    def message(self) -> str:
        return f"{self.provider} said {self.status}: {self.detail}"


class RequestExhausted(JevError):
    """Every attempt within the deadline failed; `last` names the final failure."""

    def __init__(self, timeout: float, last: str, *, timed_out: bool = False):
        self.timeout, self.last, self.timed_out = timeout, last, timed_out
        super().__init__(f"gave up after {timeout:g}s ({last})")
