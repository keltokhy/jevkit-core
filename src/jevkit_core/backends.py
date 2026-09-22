"""Backend capabilities and credentials; product adapters select their supported backends."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
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


PROVIDERS = {
    "typesafe": Backend("typesafe", "https://api.typesafe.ai/v1/systemone", "jev-latest", "TYPESAFE_API_KEY"),
    "openrouter": Backend(
        "openrouter",
        "https://openrouter.ai/api/alpha/decisions",
        "~typesafe/jev-latest",
        "OPENROUTER_API_KEY",
    ),
    "gateway": Backend("gateway", "", "jev-latest", "JEV_GATEWAY_API_KEY", url_env="JEV_GATEWAY_URL"),
    # Local servers are explicit opt-ins and never replace a configured hosted provider.
    "diffusiongemma": Backend(
        "diffusiongemma",
        "http://127.0.0.1:8080/v1/systemone",
        "openjev-latest",
        "JEV_DIFFUSIONGEMMA_API_KEY",
        url_env="JEV_DIFFUSIONGEMMA_URL",
        requires_key=False,
        auto_select=False,
        price_per_mtok=float(os.environ.get("JEV_PRICE_PER_MTOK", 0)),
        cache_by_request=True,
    ),
    "laya": Backend(
        "laya",
        "http://127.0.0.1:8081/v1/systemone",
        "laya-421m",
        "JEV_LAYA_API_KEY",
        url_env="JEV_LAYA_URL",
        requires_key=False,
        auto_select=False,
        price_per_mtok=float(os.environ.get("JEV_PRICE_PER_MTOK", 0)),
    ),
}


def backend_catalog(
    *names: str, models: dict[str, str] | None = None, backend_type: type[Backend] = Backend
) -> dict[str, Backend]:
    """Select providers in priority order, retaining tool-owned model defaults and adapters.

    Both the mapping and its definitions are fresh, so a consumer's overrides never
    change another consumer's catalog. Unknown providers or unused overrides fail early.
    """
    models = {} if models is None else models
    if unknown := models.keys() - set(names):
        raise ValueError(f"model overrides for unselected providers: {', '.join(sorted(unknown))}")
    return {
        name: backend_type(**(asdict(PROVIDERS[name]) | {"model": models.get(name, PROVIDERS[name].model)}))
        for name in names
    }


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
