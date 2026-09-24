"""The provider catalog, and how a run picks one target to send requests to."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import httpx

from .errors import JevFatal
from .settings import DEFAULT_PRICE_PER_MTOK, Settings


@dataclass(frozen=True)
class Provider:
    """A catalog entry: where a provider lives and how it is configured."""

    name: str
    url: str
    model: str
    key_env: str
    url_env: str | None = None
    requires_key: bool = True
    auto_select: bool = True
    price_per_mtok: float | None = None  # when the server reports no cost; None means the list price
    joint_reads: bool = False  # every answer depends on the whole batch of questions, not on its own

    def key_file(self, settings: Settings) -> Path:
        return settings.config_dir / f"{self.name}.key"

    def url_file(self, settings: Settings) -> Path:
        return settings.config_dir / f"{self.name}.url"

    def credential(self, settings: Settings) -> tuple[str, str]:
        return settings.credential(self.name, self.key_env)

    def endpoint(self, settings: Settings) -> str | None:
        """The run-wide override, then the provider's variable, its file, then the catalog URL."""
        if settings.url:
            return settings.url
        if self.url_env:
            if configured := settings.environ.get(self.url_env, "").strip():
                return configured
            path = self.url_file(settings)
            if path.is_file() and (configured := path.read_text().strip()):
                return configured
        return self.url or None


@dataclass(frozen=True)
class Backend:
    """A resolved target: one endpoint, one model, and the key that will be sent."""

    name: str
    url: str
    model: str
    key: str = ""
    key_source: str = "none"
    price_per_mtok: float = DEFAULT_PRICE_PER_MTOK
    joint_reads: bool = False


PROVIDERS = {
    "typesafe": Provider(
        "typesafe", "https://api.typesafe.ai/v1/systemone", "jev-latest", "TYPESAFE_API_KEY"
    ),
    "openrouter": Provider(
        "openrouter",
        "https://openrouter.ai/api/alpha/decisions",
        "~typesafe/jev-latest",
        "OPENROUTER_API_KEY",
    ),
    "gateway": Provider("gateway", "", "jev-latest", "JEV_GATEWAY_API_KEY", url_env="JEV_GATEWAY_URL"),
    # Local servers: chosen only by name, never in place of a configured hosted provider, and free
    # of API fees. The models run in their own processes; no JevKit package ships them.
    "diffusiongemma": Provider(
        "diffusiongemma",
        "http://127.0.0.1:8080/v1/systemone",
        "openjev-latest",
        "JEV_DIFFUSIONGEMMA_API_KEY",
        url_env="JEV_DIFFUSIONGEMMA_URL",
        requires_key=False,
        auto_select=False,
        price_per_mtok=0.0,
        joint_reads=True,  # a diffusion read answers every slot in the light of the others
    ),
    "laya": Provider(
        "laya",
        "http://127.0.0.1:8081/v1/systemone",
        "laya-421m",
        "JEV_LAYA_API_KEY",
        url_env="JEV_LAYA_URL",
        requires_key=False,
        auto_select=False,
        price_per_mtok=0.0,
    ),
    "gliner": Provider(
        "gliner",
        "http://127.0.0.1:8082/v1/systemone",
        "gliner2.5-decide",
        "JEV_GLINER_API_KEY",
        url_env="JEV_GLINER_URL",
        requires_key=False,
        auto_select=False,
        price_per_mtok=0.0,
    ),
}


def catalog(*items: str | Provider, models: dict[str, str] | None = None) -> dict[str, Provider]:
    """A tool's providers in its priority order, by catalog name or as its own definitions."""
    providers: dict[str, Provider] = {}
    for item in items:
        provider = item if isinstance(item, Provider) else PROVIDERS.get(item)
        if provider is None:
            raise ValueError(f"unknown provider {item!r}")
        providers[provider.name] = provider
    models = models or {}
    if unused := models.keys() - providers.keys():
        raise ValueError(f"model overrides for unselected providers: {', '.join(sorted(unused))}")
    return {name: replace(p, model=models.get(name, p.model)) for name, p in providers.items()}


def resolve(
    providers: dict[str, Provider],
    name: str | None = None,
    *,
    model: str | None = None,
    require_key: bool = True,
    missing_ok: bool = False,
    settings: Settings | None = None,
) -> Backend | None:
    """Pick the named provider, else the first auto-selectable one with a key and an endpoint.

    `require_key=False` serves cache-only runs. `missing_ok=True` returns None instead of failing
    when nothing is named and nothing is configured; a named provider must always resolve.
    """
    settings = settings or Settings.from_env()
    name = name or settings.api
    if name:
        if name not in providers:
            raise JevFatal(f"unknown API {name!r}; choose from {', '.join(providers)}")
        provider = providers[name]
        key, source = provider.credential(settings)
        if require_key and provider.requires_key and not key:
            raise JevFatal(
                f"no key for {name}. Set {provider.key_env} or put the key in {provider.key_file(settings)}"
            )
        return _backend(provider, key, source, model, settings)
    for provider in providers.values():
        if not provider.auto_select:
            continue
        key, source = provider.credential(settings)
        if key and provider.endpoint(settings):
            return _backend(provider, key, source, model, settings)
    if not require_key:
        return _backend(next(iter(providers.values())), "", "none", model, settings)
    if missing_ok:
        return None
    options = " or ".join(p.key_env for p in providers.values() if p.auto_select)
    raise JevFatal(f"no API key. Set {options}, or put a key in {settings.config_dir}/<api>.key")


def _backend(provider: Provider, key: str, source: str, model: str | None, settings: Settings) -> Backend:
    url = provider.endpoint(settings)
    if not url:
        raise JevFatal(
            f"no URL for {provider.name}. Set {provider.url_env} to the full System One endpoint "
            "(for example https://gateway.example.com/v1/systemone) "
            f"or put it in {provider.url_file(settings)}"
        )
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        parsed = None
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.host:
        raise JevFatal(f"{provider.name} endpoint must be a complete HTTP or HTTPS URL, not {url!r}")
    price = (
        settings.price_per_mtok
        if settings.price_per_mtok is not None
        else provider.price_per_mtok
        if provider.price_per_mtok is not None
        else DEFAULT_PRICE_PER_MTOK
    )
    return Backend(
        provider.name,
        url,
        model or settings.model or provider.model,
        key,
        source,
        price,
        provider.joint_reads,
    )
