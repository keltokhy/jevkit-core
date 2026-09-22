import pytest

from jevkit_runtime import (
    Backend,
    JevError,
    JevFatal,
    answer_key,
    answer_keys,
    parse_answers,
    parse_usage,
    validate_answer,
)

BACKEND = Backend("typesafe", "https://one.invalid", "m")


def test_answer_key_depends_on_who_answers_and_what_was_asked():
    question = {"type": "noul", "instructions": "rule"}
    key = answer_key(BACKEND, "évidence", question)
    assert key == answer_key(BACKEND, "évidence", dict(reversed(question.items())))
    assert key != answer_key(Backend("openrouter", "https://one.invalid", "m"), "évidence", question)
    assert key != answer_key(Backend("typesafe", "https://two.invalid", "m"), "évidence", question)
    assert key != answer_key(Backend("typesafe", "https://one.invalid", "m2"), "évidence", question)
    assert key != answer_key(BACKEND, "évidence ", question)


def test_joint_reads_key_every_slot_on_the_whole_batch():
    q = {"type": "noul", "instructions": "rule"}
    plain = answer_keys(BACKEND, "s", {"a": q, "b": q})
    assert plain == {"a": answer_key(BACKEND, "s", q), "b": answer_key(BACKEND, "s", q)}
    joint = Backend("typesafe", "https://one.invalid", "m", joint_reads=True)
    keys = answer_keys(joint, "s", {"a": q, "b": q})
    assert len(set(keys.values())) == 2 and not set(keys.values()) & set(plain.values())
    assert answer_keys(joint, "s", {"a": q, "b": q}) == keys
    assert answer_keys(joint, "s", {"b": q, "a": q})["a"] != keys["a"]  # order is part of the read
    assert answer_keys(joint, "s", {"a": q})["a"] != keys["a"]  # so is company


@pytest.mark.parametrize(
    "question,answer",
    [
        ({"type": "noul"}, {"noul": 0.5}),
        ({"type": "noul"}, {"type": "noul", "noul": 1}),
        ({"type": "choice"}, {"choice": "yes"}),
        ({"type": "choice"}, {"choice": "yes", "probabilities": {"yes": 0.7, "no": 0.3}, "confidence": 0.9}),
        ({"type": "score"}, {"score": 2, "confidence": 0.4}),
        ({"type": "score"}, {"score": 0.5}),
        ({"type": "future"}, {"anything": True}),
    ],
)
def test_well_formed_answers_pass(question, answer):
    validate_answer("q", question, answer)


@pytest.mark.parametrize(
    "question,answer",
    [
        ({"type": "noul"}, 0.5),
        ({"type": "noul"}, {}),
        ({"type": "noul"}, {"noul": True}),
        ({"type": "noul"}, {"noul": 2}),
        ({"type": "noul"}, {"noul": float("nan")}),
        ({"type": "choice"}, {"choice": 3}),
        ({"type": "choice"}, {"choice": "yes", "probabilities": {"yes": 1.5}}),
        ({"type": "choice"}, {"choice": "yes", "confidence": -1}),
        ({"type": "score"}, {"score": -1}),
        ({"type": "score"}, {"score": "2"}),
        ({"type": "score"}, {"score": 1, "confidence": 2}),
    ],
)
def test_malformed_answers_name_the_question(question, answer):
    with pytest.raises(JevError, match="question 'q'"):
        validate_answer("q", question, answer)


def test_partial_or_absent_answers_are_errors():
    questions = {"a": {"type": "noul"}, "b": {"type": "noul"}}
    with pytest.raises(JevError, match="fake returned no answers: rate limited"):
        parse_answers({"error": {"message": "rate limited"}}, questions, provider="fake")
    with pytest.raises(JevError, match="no answer returned for question 'b'"):
        parse_answers({"answers": {"a": {"noul": 0.1}}}, questions, provider="fake")
    answers = parse_answers(
        {"answers": {"b": {"noul": 0.2}, "a": {"noul": 0.1}, "extra": 1}}, questions, provider="fake"
    )
    assert list(answers) == ["a", "b"]


@pytest.mark.parametrize(
    "usage",
    [
        [],
        False,
        {"input_tokens": True},
        {"input_tokens": -1},
        {"input_tokens": 2.5},
        {"cost": True},
        {"cost": float("nan")},
        {"cost": float("inf")},
        {"cost": -1},
        {"cost": "0.5"},
        {"input_tokens": 10**400},
        {"input_tokens": 10**400, "cost": 0.1},
        {"cost": 10**400},
    ],
)
def test_invalid_metering_stops_the_run(usage):
    with pytest.raises(JevFatal, match="invalid API usage"):
        parse_usage(usage, price_per_mtok=0.042)


def test_usage_distinguishes_reported_and_estimated_cost():
    reported = parse_usage({"input_tokens": 100, "cost": 0}, price_per_mtok=0.042)
    assert (reported.tokens, reported.cost, reported.source) == (100, 0.0, "reported_by_api")
    estimated = parse_usage({"input_tokens": 100}, price_per_mtok=0.042)
    assert estimated.cost == pytest.approx(0.0000042) and estimated.source == "estimated_from_tokens"
    assert parse_usage(None, price_per_mtok=0.042) == parse_usage({}, price_per_mtok=0.042)
    assert parse_usage({}, price_per_mtok=0.042).tokens == 0
