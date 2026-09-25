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
    price_per_mtok: float | None = None  # JEV_PRICE_PER_MTOK when set; see list_price
    budget: float | None = None  # JEV_BUDGET when set: dollars, or math.inf for "none"; else the tool decides
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ, repr=False, compare=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        home = Path.home()
        raw_price = env.get("JEV_PRICE_PER_MTOK", "").strip()
        price = None
        if raw_price:
            try:
                price = float(raw_price)
            except ValueError:
                raise JevFatal(f"JEV_PRICE_PER_MTOK must be a number, not {raw_price!r}") from None
            if not math.isfinite(price) or price < 0:
                raise JevFatal("JEV_PRICE_PER_MTOK must be finite and nonnegative")
        raw_budget = env.get("JEV_BUDGET", "").strip().lower()
        budget = None
        if raw_budget in ("none", "unlimited"):
            budget = math.inf
        elif raw_budget:
            try:
                budget = float(raw_budget)
            except ValueError:
                raise JevFatal(
                    f"JEV_BUDGET must be a number of dollars or none, not {raw_budget!r}"
                ) from None
            if not math.isfinite(budget) or budget < 0:
                raise JevFatal("JEV_BUDGET must be a finite, nonnegative number of dollars, or none")
        return cls(
            config_dir=Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "jev",
            cache_dir=Path(env.get("XDG_CACHE_HOME") or home / ".cache") / "jev",
            api=env.get("JEV_API") or None,
            url=env.get("JEV_URL") or None,
            model=env.get("JEV_MODEL") or None,
            price_per_mtok=price,
            budget=budget,
            environ=env,
        )

    @property
    def list_price(self) -> float:
        """Dollars per million input tokens when neither the environment nor a provider says otherwise."""
        return DEFAULT_PRICE_PER_MTOK if self.price_per_mtok is None else self.price_per_mtok

    def credential(self, name: str, variable: str) -> tuple[str, str]:
        """A provider key and where it came from: `env`, `file`, or `none`."""
        key = self.environ.get(variable, "").strip()
        if key:
            return key, "env"
        path = self.config_dir / f"{name}.key"
        if path.is_file() and (key := path.read_text().strip()):
            return key, "file"
        return "", "none"
