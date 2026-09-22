import pytest

from jevkit_core import Meter, answer_provenance, parse_usage, record_usage


def test_mapping_and_meter_accumulate_identical_charges_without_changing_report_fields():
    totals = {"calls": 0, "input_tokens": 0, "cost": 0.0, "scored_passages": 8}
    meter = Meter(model="keep-this-model")
    for raw in ({"input_tokens": 3, "cost": 0}, {"input_tokens": 10}):
        usage = parse_usage(raw)
        record_usage(totals, usage)
        meter.record(usage, 0.25)
    assert (meter.calls, meter.input_tokens, meter.cost) == (
        totals["calls"],
        totals["input_tokens"],
        totals["cost"],
    )
    assert meter.calls == 2 and meter.input_tokens == 13
    assert meter.cost == pytest.approx(10 * 0.042 / 1e6)
    assert totals["scored_passages"] == 8
    assert meter.model == "keep-this-model" and meter.latencies == [0.25, 0.25]


def test_charge_callback_sees_updated_totals_before_latency_and_model_change():
    meter = Meter(model="old")
    seen = []

    def charge(cost):
        seen.append((cost, meter.calls, meter.cost, list(meter.latencies), meter.model))

    meter.record(parse_usage({"cost": 0.2}), 0.5, model="resolved-v1", on_cost=charge)
    assert seen == [(0.2, 1, 0.2, [], "old")]
    assert meter.latencies == [0.5] and meter.model == "resolved-v1"


def test_rejected_charge_keeps_billable_usage_without_recording_a_completed_response():
    meter = Meter(model="old")

    def charge(cost):
        raise RuntimeError("budget callback")

    with pytest.raises(RuntimeError, match="budget callback"):
        meter.record(parse_usage({"input_tokens": 2, "cost": 0.2}), 0.5, model="new", on_cost=charge)
    assert (meter.calls, meter.input_tokens, meter.cost) == (1, 2, 0.2)
    assert meter.latencies == [] and meter.model == "old"


@pytest.mark.parametrize("resolved", [None, "", "  ", 42, False])
def test_provenance_never_substitutes_requested_alias_for_unknown_responder(resolved, monkeypatch):
    monkeypatch.setattr("jevkit_core.provenance.time.time", lambda: 123.0)
    assert answer_provenance(provider="gateway", requested_model="latest", resolved_model=resolved) == {
        "version": 1,
        "provider": "gateway",
        "requested_model": "latest",
        "resolved_model": None,
        "answered_at": 123.0,
    }


def test_provenance_keeps_the_literal_resolved_model():
    origin = answer_provenance(provider="gateway", requested_model="latest", resolved_model=" v1 ")
    assert origin["resolved_model"] == " v1 "
