import math
import random
from collections import Counter

import pytest

from jevkit_runtime import audit_sample, calibration, read_sample, write_sample

LOW, HIGH = "[0, 0.05]", "(0.8, 0.95]"


def population(n=1000, seed=0):
    rng = random.Random(seed)
    return [{"key": f"k{i}", "p": rng.random() ** 3, "text": f"row {i}"} for i in range(n)]


def test_each_bin_gives_the_same_number_and_weights_undo_the_sampling():
    rows = population()
    sample = audit_sample(rows, n=60, seed=1)
    assert len(sample) == 60 and len({r["key"] for r in sample}) == 60
    sizes = Counter(audit_sample([row], n=1)[0]["bin"] for row in rows)
    assert len(sizes) == 6
    for name, size in sizes.items():
        drawn = [r for r in sample if r["bin"] == name]
        assert len(drawn) == 10 and all(r["weight"] == size / 10 for r in drawn)
    assert sum(r["weight"] for r in sample) == pytest.approx(len(rows))
    first = sample[0]
    assert list(first)[0] == "label" and first["label"] is None and first["text"].startswith("row ")
    assert audit_sample(rows, n=60, seed=1) == sample and audit_sample(rows, n=60, seed=2) != sample


def test_small_bins_give_their_places_away_and_rows_without_a_probability_are_left_out():
    rows = [{"p": 0.01}] * 2 + [{"p": 0.9}] * 50 + [{"p": None}, {"p": ""}, {"p": float("nan")}]
    sample = audit_sample(rows, n=20)
    assert Counter((r["bin"], r["weight"]) for r in sample) == {(LOW, 1.0): 2, (HIGH, 50 / 18): 18}
    assert len(audit_sample(rows, n=500)) == 52
    with pytest.raises(ValueError, match="between 0 and 1"):
        audit_sample([{"p": 1.5}])
    with pytest.raises(ValueError, match="rename it first"):
        audit_sample([{"p": 0.5, "weight": 3}])
    with pytest.raises(ValueError, match="strictly increasing"):
        audit_sample(rows, bins=(0, 0.5, 0.5, 1))
    with pytest.raises(ValueError, match="strictly increasing"):
        audit_sample(rows, bins=(0, float("nan"), 1))
    with pytest.raises(ValueError, match="at least 2, one row for each bin"):
        audit_sample(rows, n=1)
    with pytest.raises(ValueError, match="cannot be 'bin'"):
        audit_sample(rows, label="bin")


def test_calibration_weights_each_label_by_what_its_row_stands_for():
    rows = [
        {"p": 0.0, "label": "no", "bin": LOW, "weight": 10},
        {"p": 0.04, "label": 0, "bin": LOW, "weight": "10"},
        {"p": 0.9, "label": "yes", "bin": HIGH, "weight": 5},
        {"p": "0.9", "label": False, "bin": HIGH, "weight": 5},
    ]
    result = calibration(rows, n_boot=200)
    assert result.brier[0] == pytest.approx((10 * 0.04**2 + 5 * 0.1**2 + 5 * 0.9**2) / 30)
    assert result.ece[0] == pytest.approx((20 * 0.02 + 10 * 0.4) / 30)
    assert result.table == [
        {"bin": LOW, "n": 2, "mean_p": pytest.approx(0.02), "rate": 0.0},
        {"bin": HIGH, "n": 2, "mean_p": pytest.approx(0.9), "rate": 0.5},
    ]
    assert result.brier[1] <= result.brier[0] <= result.brier[2]
    assert (result.n_labeled, result.n_unlabeled) == (4, 0)

    blank = calibration([*rows, {"p": 0.9, "label": " ", "bin": HIGH, "weight": 5}], n_boot=200)
    # The blank row's weight moves to the labeled rows of its bin: each now stands for 7.5.
    assert blank.brier[0] == pytest.approx((10 * 0.04**2 + 7.5 * 0.01 + 7.5 * 0.81) / 35)
    assert blank.ece[0] == pytest.approx((20 * 0.02 + 15 * 0.4) / 35)
    assert blank.n_unlabeled == 1 and any("reweighted" in note for note in blank.notes)


def test_a_bin_without_labels_makes_population_estimates_unavailable():
    rows = [
        {"p": 0.01, "label": 1, "bin": LOW, "weight": 4},
        {"p": 0.9, "label": "", "bin": HIGH, "weight": 12},
    ]
    result = calibration(rows)
    assert all(math.isnan(x) for x in (*result.brier, *result.ece))
    assert "75.0% of the sampled weight" in result.summary()
    assert math.isnan(result.table[1]["mean_p"]) and result.table[1]["n"] == 0
    assert calibration([]).n_labeled == 0 and "No labeled rows" in calibration([]).summary()


def test_labels_and_weights_are_read_strictly():
    row = {"p": 0.5, "bin": LOW, "weight": 1}
    with pytest.raises(ValueError, match="use 1/0"):
        calibration([row | {"label": "maybe"}])
    with pytest.raises(ValueError, match="restore the 'weight' column"):
        calibration([row | {"label": 1, "weight": ""}])
    with pytest.raises(ValueError, match="every row needs a 'label'"):
        calibration([row])


@pytest.mark.parametrize("suffix", [".csv", ".jsonl"])
def test_a_labeled_file_reads_back_to_the_same_calibration(tmp_path, suffix):
    sample = audit_sample(population(), n=40, seed=3)
    rng = random.Random(4)
    for row in sample:
        row["label"] = rng.choice(["1", "0", "yes", "no", ""])
    path = tmp_path / f"sample{suffix}"
    write_sample(path, sample)
    again = read_sample(path)
    assert [r["key"] for r in again] == [r["key"] for r in sample]
    assert calibration(again, n_boot=100).brier == calibration(sample, n_boot=100).brier
    assert "| Brier score |" in calibration(again, n_boot=100).to_markdown()
