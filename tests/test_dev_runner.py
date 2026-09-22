"""The development runner must not dispatch subprocesses to unrelated installs."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture")
@pytest.mark.parametrize("action", ["run", "check", "wheel-check"])
def test_subprocess_uses_selected_environment(tmp_path, monkeypatch, action):
    spec = importlib.util.spec_from_file_location("dev_runner", Path(__file__).parents[1] / "scripts/dev.py")
    dev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dev)
    core = tmp_path / "jevkit-core"
    repo = tmp_path / "jlink"
    for directory in (core, repo):
        directory.mkdir()
        (directory / "pyproject.toml").touch()
    inherited = tmp_path / "unrelated-bin"
    inherited.mkdir()
    wrong = inherited / "jlink"
    wrong.write_text("#!/bin/sh\necho unrelated\n")
    wrong.chmod(0o755)
    monkeypatch.setenv("PATH", str(inherited))
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "unrelated-env"))
    monkeypatch.setattr(dev, "CORE", core)
    monkeypatch.setattr(
        sys, "argv", ["dev.py", "--tool", "jlink", action] + (["jlink"] if action == "run" else [])
    )
    checked = []

    def command(argv, *, cwd=None, env=None):
        if argv[0] == "uv":
            if argv[1] == "build":
                destination = Path(argv[-1])
                destination.mkdir(parents=True, exist_ok=True)
                name = "jlink" if cwd == repo else "jevkit_runtime"
                (destination / f"{name}-0.1.0-py3-none-any.whl").touch()
            return
        executable = Path(argv[0])
        if executable == dev.python(core):
            return
        bindir = executable.parent
        bindir.mkdir(parents=True, exist_ok=True)
        selected = bindir / "jlink"
        selected.write_text("#!/bin/sh\necho selected\n")
        selected.chmod(0o755)
        result = subprocess.run(["jlink"], env=env, capture_output=True, text=True, check=True)
        assert result.stdout.strip() == "selected"
        assert env["VIRTUAL_ENV"] == str(bindir.parent)
        checked.append(executable)

    monkeypatch.setattr(dev, "command", command)
    monkeypatch.setattr(dev.subprocess, "check_output", lambda *a, **kw: str(tmp_path / "site"))
    dev.main()
    assert checked
