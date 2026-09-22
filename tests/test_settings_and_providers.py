import pytest

from jevkit_runtime import PROVIDERS, JevFatal, Provider, Settings, catalog, resolve


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
    assert settings.list_price == 0.5
    monkeypatch.delenv("JEV_PRICE_PER_MTOK")
    assert Settings.from_env().price_per_mtok is None and Settings.from_env().list_price == 0.042
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
    with pytest.raises(ValueError, match="unknown provider 'typo'"):
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


def test_local_servers_are_free_keyless_and_chosen_only_by_name(monkeypatch):
    providers = catalog("typesafe", "diffusiongemma", "laya")
    monkeypatch.setenv("JEV_DIFFUSIONGEMMA_API_KEY", "optional")
    with pytest.raises(JevFatal, match="Set TYPESAFE_API_KEY, or put"):
        resolve(providers)
    gemma = resolve(providers, "diffusiongemma")
    assert (gemma.url, gemma.model, gemma.key, gemma.price_per_mtok, gemma.joint_reads) == (
        "http://127.0.0.1:8080/v1/systemone",
        "openjev-latest",
        "optional",
        0.0,
        True,
    )
    laya = resolve(providers, "laya")
    assert (laya.key, laya.price_per_mtok, laya.joint_reads) == ("", 0.0, False)
    monkeypatch.setenv("JEV_LAYA_URL", "http://gpu-box:8081/v1/systemone")
    assert resolve(providers, "laya").url == "http://gpu-box:8081/v1/systemone"


def test_price_comes_from_the_environment_then_the_provider_then_the_list(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "key")
    monkeypatch.setenv("M_KEY", "key")
    metered = Provider("metered", "https://m.invalid/v1", "m", "M_KEY", price_per_mtok=0.25)
    providers = catalog("typesafe", "laya", metered)
    assert resolve(providers, "typesafe").price_per_mtok == 0.042
    assert resolve(providers, "laya").price_per_mtok == 0.0
    assert resolve(providers, "metered").price_per_mtok == 0.25
    monkeypatch.setenv("JEV_PRICE_PER_MTOK", "0.5")
    assert {resolve(providers, name).price_per_mtok for name in providers} == {0.5}


def test_a_tool_can_add_its_own_provider():
    local = Provider(
        "local", "http://127.0.0.1:9000/v1", "local-v1", "LOCAL_KEY", requires_key=False, auto_select=False
    )
    providers = catalog("typesafe", local)
    assert list(providers) == ["typesafe", "local"]
    with pytest.raises(JevFatal, match="no API key"):
        resolve(providers)
    backend = resolve(providers, "local")
    assert (backend.key, backend.price_per_mtok, backend.url) == ("", 0.042, "http://127.0.0.1:9000/v1")
