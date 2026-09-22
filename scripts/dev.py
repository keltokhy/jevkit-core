"""Set up, check, and run the independent JevKit development checkouts."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

CORE = Path(__file__).resolve().parents[1]
TOOLS = ("jgrep", "jsort", "jlink", "jselect", "jcol")
PACKAGED_ASSETS = {
    "jcol": ["static/index.html"],
    "jlink": ["assets/review.html", "assets/review.js", "assets/review.css"],
}


def command(argv, *, cwd=CORE, env=None):
    print("+", " ".join(map(str, argv)), flush=True)
    subprocess.run(list(map(str, argv)), cwd=cwd, env=env, check=True)


def environment(directory, tokenizer_cache):
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("JEV_", "JGREP_", "JSORT_", "OPENROUTER_", "TYPESAFE_"))
    }
    env.update(
        XDG_CACHE_HOME=str(directory / "cache"),
        XDG_CONFIG_HOME=str(directory / "config"),
        TIKTOKEN_CACHE_DIR=str(tokenizer_cache),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=str(CORE / "scripts/offline"),
    )
    return env


def python(repo):
    return repo / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def runtime_environment(executable, env=None):
    """Use the selected environment for child CLI processes as well as Python."""
    result = dict(os.environ if env is None else env)
    result["PATH"] = os.pathsep.join((str(executable.parent), result.get("PATH", os.defpath)))
    result["VIRTUAL_ENV"] = str(executable.parent.parent)
    return result


def expect_core(executable, location, *, cwd, env):
    """The consumer's interpreter must import the core from `location`, not a stray install."""
    command(
        [
            executable,
            "-c",
            "import sys, pathlib, jevkit_runtime; "
            "here = pathlib.Path(jevkit_runtime.__file__).resolve().parent; "
            "assert here == pathlib.Path(sys.argv[1]).resolve(), here",
            location,
        ],
        cwd=cwd,
        env=env,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repos-root", type=Path, default=CORE.parent)
    parser.add_argument("--suffix", default="", help="checkout name suffix, e.g. --suffix=-wip")
    parser.add_argument("--tool", choices=TOOLS, help="operate on only one consumer")
    parser.add_argument(
        "--tokenizer-cache", type=Path, default=Path(tempfile.gettempdir()) / "data-gym-cache"
    )
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("setup", help="install local editable dependencies and prepare public tokenizer data")
    commands.add_parser("check", help="test the core and consumers, with model/network access blocked")
    commands.add_parser(
        "wheel-check", help="build and test separate wheel installations outside the source trees"
    )
    run = commands.add_parser("run", help="run a tool using its editable development environment")
    run.add_argument("name", choices=TOOLS)
    run.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    root = args.repos_root.resolve()
    names = [args.tool] if args.tool else list(TOOLS)
    repos = {name: root / (name + args.suffix) for name in names}
    if args.action == "run":
        repo = root / (args.name + args.suffix)
        arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
        command(
            [python(repo), "-m", args.name, *arguments],
            cwd=Path.cwd(),
            env=runtime_environment(python(repo)),
        )
        return
    for repo in repos.values():
        if not (repo / "pyproject.toml").is_file():
            parser.error(f"missing checkout: {repo}")
    if args.action == "setup":
        for name, repo in [("core", CORE), *repos.items()]:
            options = (
                ["--extra", "code"] if name == "jgrep" else ["--group", "bench"] if name == "jlink" else []
            )
            command(["uv", "sync", "--locked", *options], cwd=repo)
        if "jselect" in repos:
            env = os.environ | {"TIKTOKEN_CACHE_DIR": str(args.tokenizer_cache)}
            command(
                [
                    python(repos["jselect"]),
                    "-c",
                    "import tiktoken; [tiktoken.get_encoding(n) for n in ('o200k_base', 'cl100k_base')]",
                ],
                env=env,
            )
        return
    with tempfile.TemporaryDirectory(prefix="jevkit-check-") as temporary:
        temp = Path(temporary)
        env = environment(temp, args.tokenizer_cache)
        if args.action == "check":
            command([python(CORE), "-m", "pytest", "-q"], env=runtime_environment(python(CORE), env))
            for repo in repos.values():
                consumer_env = runtime_environment(python(repo), env)
                expect_core(python(repo), CORE / "src/jevkit_runtime", cwd=temp, env=consumer_env)
                command([python(repo), "-m", "pytest", "-q"], cwd=repo, env=consumer_env)
            return
        wheels = temp / "wheels"
        command(["uv", "build", "--no-sources", "--out-dir", wheels])
        core_wheel = next(wheels.glob("jevkit_runtime-*.whl"))
        for name, repo in repos.items():
            destination = wheels / name
            command(["uv", "build", "--no-sources", "--out-dir", destination], cwd=repo)
            package = next(destination.glob("*.whl"))
            venv = temp / name
            command(["uv", "venv", "--python", python(repo), venv])
            executable = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            wheel_env = runtime_environment(executable, env)
            command(["uv", "pip", "install", "--python", executable, core_wheel, package])
            site = subprocess.check_output(
                [str(executable), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True
            ).strip()
            expect_core(executable, Path(site) / "jevkit_runtime", cwd=temp, env=wheel_env)
            command([executable, "-m", name, "--version"], cwd=temp, env=wheel_env)
            for asset in PACKAGED_ASSETS.get(name, []):
                command(
                    [
                        executable,
                        "-c",
                        "from importlib.resources import files; "
                        f"assert files({name!r}).joinpath({asset!r}).is_file()",
                    ],
                    cwd=temp,
                    env=wheel_env,
                )
            if name == "jcol":
                cli = venv / ("Scripts/jcol.exe" if os.name == "nt" else "bin/jcol")
                command([executable, repo / "tests/test_process.py", cli], cwd=temp, env=wheel_env)
        print(json.dumps({"wheel_checks": names, "status": "passed"}))


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
