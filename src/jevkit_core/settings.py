"""Every environment and filesystem convention the runtime honors, read in one place."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .errors import JevFatal

DEFAULT_PRICE_PER_MTOK = 0.042


@dataclass(frozen=True)
class Settings:
    """Run-wide configuration: directories, overrides, and the list price for estimates."""

    config_dir: Path
    cache_dir: Path
    api: str | None = None
    url: str | None = None
    model: str | None = None
    price_per_mtok: float = DEFAULT_PRICE_PER_MTOK
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ, repr=False, compare=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        home = Path.home()
        raw_price = env.get("JEV_PRICE_PER_MTOK", "").strip()
        try:
            price = float(raw_price) if raw_price else DEFAULT_PRICE_PER_MTOK
        except ValueError:
            raise JevFatal(f"JEV_PRICE_PER_MTOK must be a number, not {raw_price!r}") from None
        if not math.isfinite(price) or price < 0:
            raise JevFatal("JEV_PRICE_PER_MTOK must be finite and nonnegative")
        return cls(
            config_dir=Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "jev",
            cache_dir=Path(env.get("XDG_CACHE_HOME") or home / ".cache") / "jev",
            api=env.get("JEV_API") or None,
            url=env.get("JEV_URL") or None,
            model=env.get("JEV_MODEL") or None,
            price_per_mtok=price,
            environ=env,
        )

    def credential(self, name: str, variable: str) -> tuple[str, str]:
        """A provider key and where it came from: `env`, `file`, or `none`."""
        key = self.environ.get(variable, "").strip()
        if key:
            return key, "env"
        path = self.config_dir / f"{name}.key"
        if path.is_file() and (key := path.read_text().strip()):
            return key, "file"
        return "", "none"
