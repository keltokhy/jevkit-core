"""Backend capabilities and credentials; product adapters select their supported backends."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import JevFatal

PRICE_PER_MTOK = float(os.environ.get("JEV_PRICE_PER_MTOK", 0.042))


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "jev"


def credential(name: str, variable: str) -> tuple[str, str]:
    """Return a key and its source, without putting secrets in errors or logs."""
    key = os.environ.get(variable, "").strip()
    if key:
        return key, "env"
    path = config_dir() / f"{name}.key"
    return (path.read_text().strip(), "config") if path.is_file() else ("", "config")


@dataclass(frozen=True)
class Backend:
    name: str
    url: str
    model: str
    key_env: str
    url_env: str | None = None
    requires_key: bool = True
    auto_select: bool = True
    price_per_mtok: float = PRICE_PER_MTOK
    cache_by_request: bool = False

    @property
    def key_file(self) -> Path:
        return config_dir() / f"{self.name}.key"

    @property
    def url_file(self) -> Path:
        return config_dir() / f"{self.name}.url"

    def key(self) -> str | None:
        # Retain the historical precedence of an explicitly set environment value.
        if os.environ.get(self.key_env):
            return os.environ[self.key_env].strip()
        return self.key_file.read_text().strip() if self.key_file.exists() else None

    def configured_url(self) -> str | None:
        if not self.url_env:
            return self.url
        if os.environ.get(self.url_env):
            return os.environ[self.url_env].strip()
        return (self.url_file.read_text().strip() if self.url_file.exists() else None) or self.url or None

    def endpoint(self) -> str:
        return os.environ.get("JEV_URL") or self.configured_url() or self.url


def resolve_backend(
    backends: dict[str, Backend], name: str | None = None, *, require_key: bool = True, help_suffix: str = ""
) -> tuple[Backend, str]:
    name = name or os.environ.get("JEV_API")
    if name:
        if name not in backends:
            raise JevFatal(f"unknown API {name!r}; choose from {', '.join(backends)}")
        backend = backends[name]
        key = backend.key() or ""
        if require_key and backend.requires_key and not key:
            raise JevFatal(f"no key for {name}. Set {backend.key_env} or put the key in {backend.key_file}")
        if backend.url_env and not backend.configured_url() and not os.environ.get("JEV_URL"):
            raise JevFatal(
                f"no URL for {name}. Set {backend.url_env} to the full System One endpoint "
                f"(for example https://gateway.example.com/v1/systemone) or put it in {backend.url_file}"
            )
        return backend, key
    for backend in backends.values():
        if (
            backend.auto_select
            and (key := backend.key())
            and (not backend.url_env or backend.configured_url())
        ):
            return backend, key
    if not require_key:
        return next(iter(backends.values())), ""
    options = " or ".join(b.key_env for b in backends.values() if b.auto_select)
    raise JevFatal(f"no API key. Set {options}, or put a key in {config_dir()}/<api>.key{help_suffix}")
