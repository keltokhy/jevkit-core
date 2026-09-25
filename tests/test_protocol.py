import pytest

from jevkit_runtime import (
    Backend,
    Choice,
    JevError,
    JevFatal,
    Noul,
    Question,
    Score,
    answer_key,
    answer_keys,
    from_body,
    packed_keys,
    parse_answers,
    parse_usage,
    validate_answer,
)

BACKEND = Backend("typesafe", "https://one.invalid", "m")
YES_NO = Choice("pick", ["yes", "no"])
LEVELS = Score("rate", ["low", "mid", "high"])


def test_answer_key_depends_on_who_answers_what_was_asked_and_the_scope():
    question = Noul("rule")
    key = answer_key(BACKEND, "évidence", question)
    assert key == answer_key(BACKEND, "évidence", Noul("rule"))
    assert key != answer_key(Backend("openrouter", "https://one.invalid", "m"), "évidence", question)
    assert key != answer_key(Backend("typesafe", "https://two.invalid", "m"), "évidence", question)
    assert key != answer_key(Backend("typesafe", "https://one.invalid", "m2"), "évidence", question)
    assert key != answer_key(BACKEND, "évidence ", question)
    assert key != answer_key(BACKEND, "évidence", Noul("rule", {"true": "t", "false": "f"}))
    assert key != answer_key(BACKEND, "évidence", question, scope="tool/v1")


def test_joint_reads_key_every_slot_on_the_whole_batch():
    q = Noul("rule")
    plain = answer_keys(BACKEND, "s", {"a": q, "b": q})
    assert plain == {"a": answer_key(BACKEND, "s", q), "b": answer_key(BACKEND, "s", q)}
    joint = Backend("typesafe", "https://one.invalid", "m", joint_reads=True)
    keys = answer_keys(joint, "s", {"a": q, "b": q})
    assert len(set(keys.values())) == 2 and not set(keys.values()) & set(plain.values())
    assert answer_keys(joint, "s", {"a": q, "b": q}) == keys
    assert answer_keys(joint, "s", {"b": q, "a": q})["a"] != keys["a"]  # order is part of the read
    assert answer_keys(joint, "s", {"a": q})["a"] != keys["a"]  # so is company


def test_packed_items_are_keyed_alone_or_on_their_call():
    q = {"rel": Noul("{slot} is relevant")}
    calls = [[("a", "one"), ("b", "two")], [("c", "three")]]
    shape = dict(prefix="p", context=None, width=2)
    alone = packed_keys(BACKEND, calls, q, reuse="item", **shape)
    moved = packed_keys(BACKEND, [[("a", "one")], [("c", "three"), ("b", "two")]], q, reuse="item", **shape)
    assert alone == moved
    assert alone["a"] != packed_keys(BACKEND, calls, q, reuse="item", **(shape | {"width": 8}))["a"]
    assert alone["a"] != packed_keys(BACKEND, calls, q, reuse="item", **(shape | {"prefix": "r"}))["a"]
    assert alone["a"] != packed_keys(BACKEND, calls, q, reuse="item", **(shape | {"context": "c"}))["a"]
    more = q | {"late": Noul("{slot} is late")}
    assert alone["a"]["rel"] != packed_keys(BACKEND, calls, more, reuse="item", **shape)["a"]["rel"]
    together = packed_keys(BACKEND, calls, q, reuse="call", **shape)
    assert (
        together["c"] != packed_keys(BACKEND, [[("c", "three"), ("b", "two")]], q, reuse="call", **shape)["c"]
    )
    joint = Backend("typesafe", "https://one.invalid", "m", joint_reads=True)
    assert packed_keys(joint, calls, q, reuse="item", **shape) == packed_keys(
        joint, calls, q, reuse="call", **shape
    )


def test_order_is_part_of_what_was_asked():
    assert answer_key(BACKEND, "s", Choice("pick", ["yes", "no"])) != answer_key(
        BACKEND, "s", Choice("pick", ["no", "yes"])
    )
    assert Choice("pick", ["yes", "no"]) != Choice("pick", ["no", "yes"])
    assert answer_key(BACKEND, {"a": 1, "b": 2}, Noul("x")) != answer_key(
        BACKEND, {"b": 2, "a": 1}, Noul("x")
    )


@pytest.mark.parametrize(
    "question,answer",
    [
        (Noul("x"), {"noul": 0.5}),
        (Noul("x"), {"type": "noul", "noul": 1}),
        (YES_NO, {"choice": "yes"}),
        (YES_NO, {"choice": "yes", "probabilities": {"yes": 0.7, "no": 0.3}, "confidence": 0.9}),
        (LEVELS, {"score": 2, "confidence": 0.4}),
        (LEVELS, {"score": 0.5}),
    ],
)
def test_well_formed_answers_pass(question, answer):
    validate_answer("q", question, answer)


@pytest.mark.parametrize(
    "question,answer",
    [
        (Noul("x"), 0.5),
        (Noul("x"), {}),
        (Noul("x"), {"noul": True}),
        (Noul("x"), {"noul": 2}),
        (Noul("x"), {"noul": float("nan")}),
        (YES_NO, {"choice": 3}),
        (YES_NO, {"choice": "maybe"}),
        (YES_NO, {"choice": "yes", "probabilities": {"yes": 1.5}}),
        (YES_NO, {"choice": "yes", "probabilities": {"perhaps": 0.5}}),
        (YES_NO, {"choice": "yes", "confidence": -1}),
        (LEVELS, {"score": -1}),
        (LEVELS, {"score": 3}),
        (LEVELS, {"score": "2"}),
        (LEVELS, {"score": 1, "confidence": 2}),
    ],
)
def test_malformed_answers_name_the_question(question, answer):
    with pytest.raises(JevError, match="question 'q'"):
        validate_answer("q", question, answer)


def test_questions_read_their_own_answers_and_round_trip_through_their_bodies():
    assert Noul("x").value({"noul": 0.2}) == 0.2 and Noul("x").confidence({"noul": 0.2}) == 0.8
    answer = {"choice": "no", "probabilities": {"yes": 0.4, "no": 0.6}}
    assert (YES_NO.value(answer), YES_NO.confidence(answer)) == ("no", 0.6)
    assert LEVELS.value({"score": 1}) == 1.0 and LEVELS.confidence({"score": 1}) is None
    for question in (Noul("x", {"true": "t", "false": "f"}), YES_NO, LEVELS):
        assert from_body(question.body()) == question and hash(from_body(question.body())) == hash(question)
    assert Choice("pick", {"yes": "yes", "no": "no"}) == YES_NO
    assert Noul("{slot} fits").at("p2").text == "p2 fits"
    bad = (
        lambda: Noul(" "),
        lambda: Choice("pick", ["only"]),
        lambda: Choice("pick", "ab"),
        lambda: Choice("pick", {"a", "b"}),
        lambda: Score("rate", ["one"]),
        lambda: Score("rate", "abc"),
    )
    for make in bad:
        with pytest.raises(ValueError):
            make()
    with pytest.raises(TypeError):
        Question("x")
    with pytest.raises(ValueError, match="unknown question type"):
        from_body({"type": "future", "instructions": "x"})
    with pytest.raises(ValueError, match="unknown question fields: extra"):
        from_body({"type": "noul", "instructions": "x", "extra": 1})
    options = {"yes": "it fits", "no": "it does not"}
    choice = Choice("pick", options)
    options["maybe"] = "unsure"
    assert list(choice.options) == ["yes", "no"]
    assert Noul("{slot} fits", {"true": "{slot} fits"}).at("p1").body()["criteria"] == {"true": "p1 fits"}


def test_partial_or_absent_answers_are_errors():
    questions = {"a": Noul("x"), "b": Noul("y")}
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
