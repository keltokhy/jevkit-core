import pytest

from jevkit_core import PROVIDERS, Backend, JevFatal, backend_catalog, credential, resolve_backend


@pytest.fixture(autouse=True)
def config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for name in ("JEV_API", "JEV_URL", "TEST_KEY", "TEST_URL"):
        monkeypatch.delenv(name, raising=False)


def test_environment_credentials_take_precedence_over_files(monkeypatch, tmp_path):
    (tmp_path / "jev").mkdir()
    (tmp_path / "jev/test.key").write_text("file-key\n")
    backend = Backend("test", "https://fixture.invalid", "v1", "TEST_KEY")
    assert credential("test", "TEST_KEY") == ("file-key", "config")
    monkeypatch.setenv("TEST_KEY", " env-key ")
    assert credential("test", "TEST_KEY") == ("env-key", "env")
    assert resolve_backend({"test": backend}) == (backend, "env-key")


def test_local_backend_is_explicit_and_can_be_keyless():
    backend = Backend(
        "local", "http://127.0.0.1:8080", "local-v1", "TEST_KEY", requires_key=False, auto_select=False
    )
    with pytest.raises(JevFatal, match="no API key"):
        resolve_backend({"local": backend})
    assert resolve_backend({"local": backend}, "local") == (backend, "")


def test_cache_only_selection_does_not_require_credentials():
    backend = Backend("test", "https://fixture.invalid", "v1", "TEST_KEY")
    assert resolve_backend({"test": backend}, require_key=False) == (backend, "")


def test_gateway_url_configuration_and_override(monkeypatch, tmp_path):
    backend = Backend("test", "", "v1", "TEST_KEY", url_env="TEST_URL")
    monkeypatch.setenv("TEST_KEY", "key")
    with pytest.raises(JevFatal, match="no URL"):
        resolve_backend({"test": backend}, "test")
    (tmp_path / "jev").mkdir()
    (tmp_path / "jev/test.url").write_text("https://file.invalid\n")
    assert backend.endpoint() == "https://file.invalid"
    monkeypatch.setenv("TEST_URL", "https://env.invalid")
    assert backend.endpoint() == "https://env.invalid"
    monkeypatch.setenv("JEV_URL", "https://override.invalid")
    assert backend.endpoint() == "https://override.invalid"


def test_catalog_preserves_priority_subclasses_and_isolates_model_overrides():
    class Adapter(Backend):
        pass

    selected = backend_catalog(
        "openrouter", "typesafe", models={"typesafe": "pinned-v1"}, backend_type=Adapter
    )
    assert list(selected) == ["openrouter", "typesafe"]
    assert isinstance(selected["typesafe"], Adapter)
    assert selected["typesafe"].model == "pinned-v1"
    assert selected["openrouter"].model == "~typesafe/jev-latest"
    selected.pop("typesafe")
    assert backend_catalog("typesafe")["typesafe"].model == "jev-latest"
    assert PROVIDERS["typesafe"].model == "jev-latest"


def test_catalog_local_capabilities_do_not_enable_automatic_selection():
    local = backend_catalog("diffusiongemma", "laya")
    assert local["diffusiongemma"].cache_by_request
    assert not local["laya"].cache_by_request
    assert all(not b.requires_key and not b.auto_select for b in local.values())
    for name in local:
        assert resolve_backend(local, name) == (local[name], "")
    with pytest.raises(JevFatal, match="no API key"):
        resolve_backend(local)


def test_catalog_rejects_unknown_providers_and_unused_overrides():
    with pytest.raises(KeyError, match="typo"):
        backend_catalog("typo")
    with pytest.raises(ValueError, match="unselected providers: gateway"):
        backend_catalog("typesafe", models={"gateway": "pinned-v1"})
