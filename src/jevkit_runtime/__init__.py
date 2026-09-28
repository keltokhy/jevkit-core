"""Shared runtime for the JevKit tools: one pipeline, one store, one provider catalog."""

from importlib.metadata import version

from .budget import Budget, Hold
from .client import Answers, Client, PackedAnswers, PackedPlan, Plan
from .errors import (
    JevBudgetExceeded,
    JevError,
    JevFatal,
    ProviderError,
    ProviderFatal,
    ProviderStatus,
    RequestExhausted,
)
from .meter import Meter
from .protocol import (
    Usage,
    answer_key,
    answer_keys,
    digest,
    error_detail,
    estimate_tokens,
    packed_keys,
    packed_request,
    parse_answers,
    parse_usage,
    request_body,
    validate_answer,
)
from .providers import PROVIDERS, Backend, Provider, catalog, resolve
from .question import Choice, Noul, Question, Score, from_body
from .run import Run, fingerprint
from .settings import DEFAULT_PRICE_PER_MTOK, Settings
from .store import AnswerStore, Entry
from .transport import FATAL, RETRYABLE, post
from .validate import Calibration, audit_sample, calibration, read_sample, write_sample

__version__ = version("jevkit-runtime")
__all__ = [
    "AnswerStore",
    "Answers",
    "Backend",
    "Budget",
    "Calibration",
    "Choice",
    "Client",
    "DEFAULT_PRICE_PER_MTOK",
    "Entry",
    "FATAL",
    "Hold",
    "JevBudgetExceeded",
    "JevError",
    "JevFatal",
    "Meter",
    "Noul",
    "PackedAnswers",
    "PackedPlan",
    "PROVIDERS",
    "Plan",
    "ProviderError",
    "ProviderFatal",
    "ProviderStatus",
    "Provider",
    "Question",
    "RETRYABLE",
    "RequestExhausted",
    "Run",
    "Score",
    "Settings",
    "Usage",
    "answer_key",
    "answer_keys",
    "audit_sample",
    "calibration",
    "catalog",
    "digest",
    "error_detail",
    "estimate_tokens",
    "fingerprint",
    "from_body",
    "packed_keys",
    "packed_request",
    "parse_answers",
    "parse_usage",
    "post",
    "read_sample",
    "request_body",
    "resolve",
    "validate_answer",
    "write_sample",
]
