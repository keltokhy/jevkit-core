import pytest

from jevkit_core import PROVIDERS, JevFatal, Settings, catalog, resolve


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for name in (
        "JEV_API",
        "JEV_URL",
        "JEV_MODEL",
        "JEV_PRICE_PER_MTOK",
        "TYPESAFE_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_settings_read_every_convention_once(monkeypatch, tmp_path):
    monkeypatch.setenv("JEV_API", "gateway")
    monkeypatch.setenv("JEV_MODEL", "pinned")
    monkeypatch.setenv("JEV_PRICE_PER_MTOK", "0.5")
    settings = Settings.from_env()
    assert settings.config_dir == tmp_path / "jev"
    assert (settings.api, settings.model, settings.price_per_mtok) == ("gateway", "pinned", 0.5)
    monkeypatch.setenv("JEV_PRICE_PER_MTOK", "lots")
    with pytest.raises(JevFatal, match="JEV_PRICE_PER_MTOK"):
        Settings.from_env()


def test_environment_credentials_take_precedence_over_files(monkeypatch, tmp_path):
    (tmp_path / "jev").mkdir()
    (tmp_path / "jev/typesafe.key").write_text("file-key\n")
    settings = Settings.from_env()
    assert settings.credential("typesafe", "TYPESAFE_API_KEY") == ("file-key", "file")
    monkeypatch.setenv("TYPESAFE_API_KEY", " env-key ")
    assert Settings.from_env().credential("typesafe", "TYPESAFE_API_KEY") == ("env-key", "env")
    backend = resolve(catalog("typesafe"))
    assert (backend.name, backend.key, backend.key_source) == ("typesafe", "env-key", "env")


def test_catalog_keeps_priority_and_isolates_model_overrides():
    selected = catalog("openrouter", "typesafe", models={"typesafe": "pinned-v1"})
    assert list(selected) == ["openrouter", "typesafe"]
    assert selected["typesafe"].model == "pinned-v1"
    assert PROVIDERS["typesafe"].model == "jev-latest"
    with pytest.raises(ValueError, match="unknown providers: typo"):
        catalog("typo")
    with pytest.raises(ValueError, match="unselected providers: gateway"):
        catalog("typesafe", models={"gateway": "pinned-v1"})


def test_resolution_prefers_the_first_configured_provider(monkeypatch):
    providers = catalog("typesafe", "openrouter")
    with pytest.raises(JevFatal, match="TYPESAFE_API_KEY or OPENROUTER_API_KEY"):
        resolve(providers)
    assert resolve(providers, missing_ok=True) is None
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    assert resolve(providers).name == "openrouter"
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-key")
    assert resolve(providers).name == "typesafe"
    assert resolve(providers, "openrouter", model="override").model == "override"
    monkeypatch.setenv("JEV_MODEL", "from-env")
    assert resolve(providers, "openrouter").model == "from-env"
    assert resolve(providers, "openrouter", model="argument").model == "argument"


def test_named_provider_must_have_a_key_unless_the_run_is_cache_only():
    providers = catalog("typesafe")
    with pytest.raises(JevFatal, match="no key for typesafe"):
        resolve(providers, "typesafe")
    with pytest.raises(JevFatal, match="unknown API 'nope'"):
        resolve(providers, "nope")
    assert resolve(providers, require_key=False).key == ""
    assert resolve(providers, "typesafe", require_key=False).key_source == "none"


def test_gateway_needs_a_complete_endpoint(monkeypatch, tmp_path):
    providers = catalog("gateway")
    monkeypatch.setenv("JEV_GATEWAY_API_KEY", "key")
    with pytest.raises(JevFatal, match="no URL for gateway"):
        resolve(providers, "gateway")
    (tmp_path / "jev").mkdir()
    (tmp_path / "jev/gateway.url").write_text("https://file.invalid/v1\n")
    assert resolve(providers, "gateway").url == "https://file.invalid/v1"
    monkeypatch.setenv("JEV_GATEWAY_URL", "not a url")
    with pytest.raises(JevFatal, match="complete HTTP or HTTPS URL"):
        resolve(providers, "gateway")
    monkeypatch.setenv("JEV_GATEWAY_URL", "https://env.invalid/v1")
    assert resolve(providers, "gateway").url == "https://env.invalid/v1"
    monkeypatch.setenv("JEV_URL", "https://override.invalid/v1")
    assert resolve(providers, "gateway").url == "https://override.invalid/v1"


def test_local_providers_are_keyless_and_never_auto_selected():
    local = catalog("diffusiongemma", "laya")
    with pytest.raises(JevFatal, match="no API key"):
        resolve(local)
    backend = resolve(local, "laya")
    assert (backend.key, backend.price_per_mtok) == ("", 0.0)
