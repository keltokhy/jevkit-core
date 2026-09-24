"""The GLiNER2.5-Decide adapter turns System One questions into classification tasks and back."""

import importlib.util
from pathlib import Path

import pytest

from jevkit_runtime.protocol import parse_answers

spec = importlib.util.spec_from_file_location(
    "gliner_server", Path(__file__).parents[1] / "scripts/gliner_server.py"
)
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class FakeModel:
    """Returns a fixed distribution per label set, as classify_text does with include_confidence."""

    def __init__(self, distributions):
        self.distributions = distributions
        self.calls = []

    def classify_text(self, text, tasks, include_confidence=False):
        assert include_confidence
        self.calls.append((text, tasks))
        ((name, config),) = tasks.items()
        dist = self.distributions[tuple(config["labels"])]
        return {name: [{"label": label, "confidence": p} for label, p in dist.items()]}


QUESTIONS = {
    "spam": {"type": "noul", "instructions": "The message is spam."},
    "topic": {
        "type": "choice",
        "instructions": "Choose the topic.",
        "criteria": {"billing": "About money", "shipping": "About delivery"},
    },
    "urgency": {"type": "score", "instructions": "Rate urgency.", "criteria": ["low", "medium", "high"]},
}


def test_each_question_is_its_own_task_and_answers_validate():
    model = FakeModel(
        {
            ("yes", "no"): {"yes": 0.8, "no": 0.2},
            ("billing", "shipping"): {"billing": 0.3, "shipping": 0.7},
            ("low", "medium", "high"): {"low": 0.1, "medium": 0.2, "high": 0.7},
        }
    )
    answers = server.predict(model, {"subject": "Where is my parcel", "id": 7}, QUESTIONS)
    parse_answers({"answers": answers}, QUESTIONS, provider="gliner")
    assert answers["spam"] == {"noul": 0.8}
    assert answers["topic"]["choice"] == "shipping"
    assert answers["topic"]["confidence"] == 0.7
    assert answers["urgency"]["score"] == pytest.approx(1.6)
    assert len(model.calls) == 3
    text, tasks = model.calls[1]
    assert text == "subject: Where is my parcel\nid: 7"
    assert tasks["q"]["prompt"] == "Choose the topic."
    assert tasks["q"]["labels"] == {"billing": "About money", "shipping": "About delivery"}
    # The whole softmax distribution comes back only as a zero-threshold multi-label read.
    assert (tasks["q"]["multi_label"], tasks["q"]["class_act"], tasks["q"]["cls_threshold"]) == (
        True,
        "softmax",
        0.0,
    )


def test_noul_criteria_describe_the_yes_and_no_labels():
    config, order = server.task(
        {"type": "noul", "instructions": "Relevant?", "criteria": {"true": "Useful", "false": "Unrelated"}}
    )
    assert order == ["yes", "no"]
    assert config["labels"] == {"yes": "Useful", "no": "Unrelated"}


@pytest.mark.parametrize(
    "question",
    [
        {"type": "extract", "instructions": "x"},
        {"type": "noul"},
        {"type": "choice", "instructions": "x"},
        {"type": "score", "instructions": "x", "criteria": ["only"]},
    ],
)
def test_questions_it_cannot_answer_are_refused(question):
    with pytest.raises(ValueError):
        server.task(question)
