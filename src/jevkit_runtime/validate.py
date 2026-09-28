"""Validation from any tool's answers: a sample to label by hand, then calibration with sampling weights.

A tool's output rows carry a probability, `p` unless named otherwise. `audit_sample` draws a sample
stratified by probability bin and gives each row its inverse-inclusion weight. A person fills in
`label` (1 or 0, yes or no, blank to skip), and `calibration` turns the labeled rows into a reliability
table, a Brier score and an expected calibration error, with intervals from a bootstrap within bins.
This is what makes a probability citable: when the model says 0.8, how often is it right?

Rows keep every field they came with, so an answer key or a record ID travels with its label and a
labeled file can be read again after a rerun. `write_sample` and `read_sample` use CSV or JSONL.
jlink's audit, which this generalizes, adds link-level precision and recall on top.
"""

from __future__ import annotations

import bisect
import csv
import json
import math
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

BINS = (0.0, 0.05, 0.2, 0.5, 0.8, 0.95, 1.0)
_LABELS = {
    "1": 1.0, "1.0": 1.0, "true": 1.0, "y": 1.0, "yes": 1.0,
    "0": 0.0, "0.0": 0.0, "false": 0.0, "n": 0.0, "no": 0.0,
}  # fmt: skip
_RESTORE = "restore the 'weight' column from the file that audit_sample wrote"


def _whole(value, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be a whole number at least {minimum}")
    return value


def _edges(bins: Sequence[float]) -> list[float]:
    try:
        edges = [float(edge) for edge in bins]
    except (TypeError, ValueError) as exc:
        raise ValueError("bins must be increasing numeric boundaries from 0 to 1") from exc
    if len(edges) < 2 or edges[0] != 0 or edges[-1] != 1 or any(high <= low for low, high in pairwise(edges)):
        raise ValueError("bins must be strictly increasing boundaries starting at 0 and ending at 1")
    return edges


def _names(edges: list[float]) -> list[str]:
    """`[0, 0.05]`, `(0.05, 0.2]`, ...: the first bin holds 0 itself, every bin holds its upper edge."""
    pairs = list(pairwise(edges))
    names = [f"{'[' if i == 0 else '('}{low:g}, {high:g}]" for i, (low, high) in enumerate(pairs)]
    if len(set(names)) != len(names):  # edges that differ past six significant digits
        names = [f"{'[' if i == 0 else '('}{low!r}, {high!r}]" for i, (low, high) in enumerate(pairs)]
    return names


def _probability(value, name: str) -> float | None:
    """A probability, or None for a row outside the population (unanswered, failed, blank)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"column {name!r} has {value!r}, which is not a probability") from None
    if math.isnan(number):
        return None
    if not 0 <= number <= 1:
        raise ValueError(f"column {name!r} has {value!r}; probabilities lie between 0 and 1")
    return number


def _allocation(sizes: list[int], n: int) -> list[int]:
    """Equal allocation, handing the places a small bin cannot fill to the others."""
    counts = [0] * len(sizes)
    remaining = min(n, sum(sizes))
    while remaining:
        available = [i for i, size in enumerate(sizes) if counts[i] < size]
        share, extra = divmod(remaining, len(available))
        for rank, i in enumerate(available):
            take = min(share + (rank < extra), sizes[i] - counts[i])
            counts[i] += take
            remaining -= take
    return counts


def audit_sample(
    rows: Iterable[Mapping],
    n: int = 200,
    *,
    p: str = "p",
    label: str = "label",
    bins: Sequence[float] = BINS,
    seed: int = 0,
) -> list[dict]:
    """Draw about `n` rows for hand labeling, the same number from each probability bin.

    Each sampled row gains an empty `label`, its `bin` and its `weight`: how many rows of the run it
    stands for, the bin's size over the number drawn from it. Bins with few rows give their places to
    the others. Rows without a probability are outside the population. The same rows and `seed` give
    the same sample.
    """
    n, seed, edges = _whole(n, "n", 0), _whole(seed, "seed", 0), _edges(bins)
    names = _names(edges)
    groups: dict[int, list[Mapping]] = {}
    for row in rows:
        if label in row or "bin" in row or "weight" in row:
            raise ValueError(f"rows already have a {label!r}, 'bin' or 'weight' field; rename it first")
        value = _probability(row.get(p), p)
        if value is not None:
            groups.setdefault(max(bisect.bisect_left(edges, value) - 1, 0), []).append(row)
    order = sorted(groups)
    counts = _allocation([len(groups[i]) for i in order], n)
    rng = random.Random(seed)
    sample = []
    for i, count in zip(order, counts, strict=True):
        weight = len(groups[i]) / count if count else None
        sample += [
            {label: None, **row, "bin": names[i], "weight": weight} for row in rng.sample(groups[i], count)
        ]
    rng.shuffle(sample)
    return sample


def _label(value, name: str) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    text = str(value).strip().casefold()
    if not text:
        return None
    if text not in _LABELS:
        raise ValueError(f"column {name!r} has {value!r}; use 1/0, True/False, y/n, yes/no, or blank")
    return _LABELS[text]


def _weight(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number


def _average(values: Sequence[float], weights: Sequence[float]) -> float:
    total = sum(weights)
    return sum(v * w for v, w in zip(values, weights, strict=True)) / total if total > 0 else math.nan


def _quantile(ordered: list[float], q: float) -> float:
    """Linear interpolation between order statistics, as numpy's default does."""
    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _format(value: float) -> str:
    return "NaN" if math.isnan(value) else f"{value:.4f}"


@dataclass
class Calibration:
    """Weighted calibration of a run's probabilities against hand labels, with 95% bootstrap intervals.

    `table` has a row per bin: `n` labeled rows, and their weighted `mean_p` and `rate` of true labels.
    `brier` and `ece` are (estimate, low, high). `ece` is the expected calibration error over the
    sampling bins: the weighted mean gap between a bin's mean probability and its rate.
    """

    brier: tuple[float, float, float]
    ece: tuple[float, float, float]
    table: list[dict]
    n_labeled: int
    n_unlabeled: int
    notes: tuple[str, ...] = field(default=())

    def summary(self) -> str:
        parts = [f"{self.n_labeled:,} labeled rows; {self.n_unlabeled:,} labels left blank."]
        for name, label in (("brier", "Brier score"), ("ece", "Expected calibration error")):
            estimate, low, high = getattr(self, name)
            parts.append(f"{label} {_format(estimate)} (95% CI {_format(low)} to {_format(high)}).")
        parts.append("Estimates use sampling weights; 95% intervals use a bootstrap within bins.")
        return " ".join([*parts, *self.notes])

    def to_markdown(self) -> str:
        lines = ["| Measure | Estimate | 95% interval |", "|:--|--:|:--|"]
        for name, label in (("brier", "Brier score"), ("ece", "Expected calibration error")):
            estimate, low, high = getattr(self, name)
            lines.append(f"| {label} | {_format(estimate)} | {_format(low)} to {_format(high)} |")
        lines += [
            "",
            "| Probability bin | Labeled rows | Weighted mean p | Weighted rate |",
            "|:--|--:|--:|--:|",
        ]
        for row in self.table:
            name = str(row["bin"]).replace("|", "\\|")
            lines.append(f"| {name} | {row['n']:,} | {_format(row['mean_p'])} | {_format(row['rate'])} |")
        return "\n".join([*lines, "", self.summary()])


def _bin_order(names: Iterable[str]) -> list[str]:
    """Bins in probability order when their names are audit_sample's, else in order of appearance."""
    names = list(dict.fromkeys(names))
    try:
        return sorted(names, key=lambda name: float(name[1:].split(",")[0]))
    except (ValueError, IndexError):
        return names


def _estimates(groups: Mapping[str, list[tuple[float, float, float]]]) -> tuple[float, float]:
    """Weighted Brier score and expected calibration error from each bin's (p, label, weight) rows."""
    squared = total = gap = 0.0
    for members in groups.values():
        weight = sum(w for _, _, w in members)
        squared += sum(w * (p - y) ** 2 for p, y, w in members)
        mean_p = sum(w * p for p, _, w in members) / weight
        rate = sum(w * y for _, y, w in members) / weight
        gap += weight * abs(mean_p - rate)
        total += weight
    return squared / total, gap / total


def calibration(
    rows: Iterable[Mapping], *, p: str = "p", label: str = "label", n_boot: int = 2000, seed: int = 0
) -> Calibration:
    """Weighted calibration from an audit sample with some labels filled in.

    Keep the rows left blank, weights included: within each bin the labeled rows take over the weight
    of its blank rows, so a bin with more blanks is not underrepresented. That assumes a blank is
    unrelated to the truth within its bin. A bin with sampled rows but no label cannot be estimated,
    so the population-wide estimates are then NaN and the notes say how much weight those bins hold.
    """
    n_boot, seed = _whole(n_boot, "n_boot", 1), _whole(seed, "seed", 0)
    rows = list(rows)
    for name in (label, "bin", "weight"):
        if rows and any(name not in row for row in rows):
            raise ValueError(f"every row needs a {name!r} field; label the file audit_sample wrote")
    labels = [_label(row[label], label) for row in rows]
    sampled = [_weight(row["weight"]) for row in rows]
    bad = [w for w in sampled if not (math.isfinite(w) and w > 0)]
    if bad:
        raise ValueError(
            f"column 'weight' needs a positive number in every row, but {len(bad):,} rows have none; "
            f"{_RESTORE}"
        )
    if any(row["bin"] in (None, "") for row in rows):
        raise ValueError("column 'bin' must name a sampling stratum for every row")
    top = max(sampled, default=1.0)
    sampled = [w / top for w in sampled]  # ratios are unchanged, and large weights cannot overflow
    order = _bin_order(str(row["bin"]) for row in rows)
    bin_weight = dict.fromkeys(order, 0.0)
    labeled_weight = dict.fromkeys(order, 0.0)
    blanks = dict.fromkeys(order, 0)
    for row, y, w in zip(rows, labels, sampled, strict=True):
        name = str(row["bin"])
        bin_weight[name] += w
        if y is None:
            blanks[name] += 1
        else:
            labeled_weight[name] += w
    factors = {
        name: bin_weight[name] / labeled_weight[name] if blanks[name] and labeled_weight[name] else 1.0
        for name in order
    }
    groups: dict[str, list[tuple[float, float, float]]] = {name: [] for name in order}
    for row, y, w in zip(rows, labels, sampled, strict=True):
        if y is not None:
            value = _probability(row.get(p), p)
            if value is None:
                raise ValueError(f"a labeled row has no {p!r}; every labeled row needs its probability")
            groups[str(row["bin"])].append((value, y, w * factors[str(row["bin"])]))
    n_labeled = sum(len(members) for members in groups.values())
    table, empty = [], []
    for name in order:
        members = groups[name]
        if members:
            weights = [w for _, _, w in members]
            mean_p = _average([v for v, _, _ in members], weights)
            rate = _average([y for _, y, _ in members], weights)
        else:
            mean_p = rate = math.nan
            empty.append(name)
        table.append({"bin": name, "n": len(members), "mean_p": mean_p, "rate": rate})
    notes = []
    nan3 = (math.nan, math.nan, math.nan)
    brier = ece = nan3
    if not n_labeled:
        notes.append("No labeled rows: all estimates are NaN.")
    elif empty:
        total = sum(bin_weight.values())
        detail = ", ".join(f"{name} {bin_weight[name] / total:.1%}" for name in empty)
        share = sum(bin_weight[name] for name in empty) / total
        notes.append(
            f"No labels in bin(s) {', '.join(empty)}, which hold {share:.1%} "
            f"of the sampled weight ({detail}); no labeled row can stand in for them, so the population-wide "
            "estimates are NaN. Label rows in every bin."
        )
    else:
        brier_hat, ece_hat = _estimates(groups)
        rng = random.Random(seed)
        replicates = [
            _estimates({name: rng.choices(members, k=len(members)) for name, members in groups.items()})
            for _ in range(n_boot)
        ]
        brier = (brier_hat, *(_quantile(sorted(r[0] for r in replicates), q) for q in (0.025, 0.975)))
        ece = (ece_hat, *(_quantile(sorted(r[1] for r in replicates), q) for q in (0.025, 0.975)))
        if any(len(members) == 1 for members in groups.values()):
            notes.append(
                "A bin has only one label; its within-bin uncertainty cannot be estimated by resampling. "
                "Label more rows for reliable intervals."
            )
    adjusted = [name for name in order if factors[name] != 1.0]
    if adjusted and not empty:
        detail = ", ".join(f"{name} x{factors[name]:.3g}" for name in adjusted)
        notes.append(
            "Rows with a blank label were kept as part of the sample: in each bin that has them, the labeled "
            f"rows were reweighted to the bin's full sampling weight ({detail}). Labels left blank for "
            "reasons related to the truth within a bin can still bias the estimates."
        )
    return Calibration(brier, ece, table, n_labeled, len(rows) - n_labeled, tuple(notes))


def write_sample(path: str | Path, rows: Sequence[Mapping]) -> None:
    """Write rows as CSV or JSONL, by the file's extension; a CSV's columns are every field, in order."""
    path = Path(path)
    if path.suffix.lower() == ".jsonl":
        with path.open("w", encoding="utf-8") as out:
            for row in rows:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
        return
    if path.suffix.lower() != ".csv":
        raise ValueError("write a sample to a .csv or .jsonl file")
    columns = list(dict.fromkeys(name for row in rows for name in row))
    with path.open("w", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: "" if v is None else v for k, v in row.items()})


def read_sample(path: str | Path) -> list[dict]:
    """Read a CSV or JSONL sample back; CSV values come back as text, which `calibration` accepts."""
    path = Path(path)
    if path.suffix.lower() == ".jsonl":
        with path.open(encoding="utf-8") as lines:
            return [json.loads(line) for line in lines if line.strip()]
    if path.suffix.lower() != ".csv":
        raise ValueError("read a sample from a .csv or .jsonl file")
    with path.open(encoding="utf-8", newline="") as lines:
        return list(csv.DictReader(lines))
