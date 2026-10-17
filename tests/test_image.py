import asyncio
import base64
import io
import json
import pickle

import httpx
import pytest
from PIL import Image as Pil

from jevkit_runtime import (
    PROVIDERS,
    Backend,
    Client,
    Image,
    JevError,
    JevFatal,
    Noul,
    answer_key,
    catalog,
    estimate_tokens,
    fingerprint,
    request_body,
    resolve,
)
from jevkit_runtime.image import HEADER, TOKENS_PER_PIXEL

BACKEND = Backend(
    "typesafe", "https://fixture.invalid/v1", "jev-1.13.0", key="test-key", price_per_mtok=0.042, images=True
)


def picture(size=(800, 600), mode="RGB", color=(200, 30, 30), fmt="PNG", **save) -> bytes:
    out = io.BytesIO()
    Pil.new(mode, size, color).save(out, fmt, **save)
    return out.getvalue()


def opened(prepared):
    return Pil.open(io.BytesIO(prepared.data))


class Fake:
    def __init__(self):
        self.requests = []

    async def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append((request, body))
        answers = {qid: {"noul": 0.9} for qid in body["questions"]}
        return httpx.Response(200, json={"answers": answers, "usage": {"input_tokens": 1500, "cost": 0.0001}})


def run(coroutine):
    return asyncio.run(asyncio.wait_for(coroutine, 10))


def test_an_image_is_its_reference_and_the_same_bytes_give_the_same_reference(tmp_path):
    data = picture()
    path = tmp_path / "door.png"
    path.write_bytes(data)
    a, b, other = Image(path), Image(data), Image(picture(color=(0, 0, 0)))
    assert isinstance(a, str) and str(a).startswith("img-") and len(a) == 36
    assert a == b and a != other
    assert json.loads(json.dumps({"door": a})) == {"door": str(a)}
    assert answer_key(BACKEND, {"door": a}, Noul("q")) == answer_key(BACKEND, {"door": b}, Noul("q"))
    assert answer_key(BACKEND, {"door": a}, Noul("q")) != answer_key(BACKEND, {"door": other}, Noul("q"))
    assert fingerprint([{"door": a}]) == fingerprint([{"door": b}]) != fingerprint([{"door": other}])
    assert type(pickle.loads(pickle.dumps(a))) is str  # a worker process sees only the reference


def test_an_unreadable_path_is_a_clear_error(tmp_path):
    with pytest.raises(JevError, match="cannot read image"):
        Image(tmp_path / "missing.jpg")


def test_large_pictures_shrink_to_1568_and_become_baseline_jpeg():
    prepared = Image(picture((4032, 3024), fmt="JPEG")).prepare()
    assert (prepared.format, prepared.width, prepared.height) == ("jpg", 1568, 1176)
    with opened(prepared) as sent:
        assert sent.format == "JPEG" and not sent.info.get("progressive")
    assert prepared.tokens == pytest.approx(1568 * 1176 * TOKENS_PER_PIXEL, abs=1)


def test_small_and_thin_pictures_are_raised_to_the_minimums():
    for size in [(100, 80), (2000, 40), (256, 256), (30, 30)]:
        prepared = Image(picture(size)).prepare()
        w, h = prepared.width, prepared.height
        assert 256 <= w <= 1568 and 256 <= h <= 1568 and w * h >= 280 * 280, size


def test_exif_rotation_is_applied_to_the_pixels():
    exif = Pil.Exif()
    exif[0x0112] = 6  # rotate 90 degrees clockwise to display upright
    prepared = Image(picture((800, 400), fmt="JPEG", exif=exif)).prepare()
    assert (prepared.width, prepared.height) == (400, 800)


def test_transparency_is_kept_as_png():
    prepared = Image(picture((400, 400), mode="RGBA", color=(0, 0, 0, 0))).prepare()
    assert prepared.format == "png"
    with opened(prepared) as sent:
        assert sent.mode == "RGBA"


def test_animated_and_broken_files_are_refused():
    out = io.BytesIO()
    frames = [Pil.new("RGB", (300, 300), c) for c in ((255, 0, 0), (0, 255, 0))]
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:])
    with pytest.raises(JevError, match="animated"):
        Image(out.getvalue()).prepare()
    with pytest.raises(JevError, match="cannot read"):
        Image(b"not an image").prepare()


def test_estimates_count_each_use_of_an_image_and_not_its_base64():
    photo = Image(picture((1000, 1000)))
    text_only = estimate_tokens(request_body("m", {"door": "x" * 36}, {"q": Noul("q")}))
    once = estimate_tokens(request_body("m", {"door": photo}, {"q": Noul("q")}))
    twice = estimate_tokens(request_body("m", {"door": photo}, {"q": Noul(f"same as {photo}?")}))
    assert once - text_only == photo.tokens
    assert photo.tokens < twice - once < photo.tokens + 20  # another copy, and the question's words
    body = request_body("m", {"door": photo}, {"q": Noul("q")}) | {"assets": {"big": "A" * 10**6}}
    assert estimate_tokens(body) == once


def test_a_request_carries_its_images_as_assets_with_the_header():
    fake = Fake()
    photo, badge = Image(picture()), Image(picture(color=(0, 0, 255)))
    questions = {"person": Noul("Is there a person in `door`?"), "same": Noul(f"Same person as {badge}?")}

    async def exercise():
        async with Client(BACKEND, transport=httpx.MockTransport(fake)) as client:
            answers = await client.ask({"door": photo}, questions)
            assert answers["person"] == {"noul": 0.9}
            await client.ask("plain text", {"q": Noul("q")})

    run(exercise())
    (first, body), (second, plain) = fake.requests
    assert body["state"] == {"door": str(photo)}
    assert set(body["assets"]) == {str(photo), str(badge)}
    asset = body["assets"][str(photo)]
    assert asset["type"] == "image" and asset["format"] == "jpg"
    assert base64.b64decode(asset["data_base64"]) == photo.prepare().data
    assert first.headers[next(iter(HEADER))] == "true"
    assert "assets" not in plain and next(iter(HEADER)) not in second.headers


def test_a_provider_that_cannot_read_images_stops_before_sending():
    fake = Fake()
    local = Backend("laya", "http://127.0.0.1:8081/v1/systemone", "laya-421m", price_per_mtok=0.0)

    async def exercise():
        async with Client(local, transport=httpx.MockTransport(fake)) as client:
            with pytest.raises(
                JevFatal, match="laya does not read images; use typesafe, openrouter, gateway"
            ):
                await client.ask({"door": Image(picture())}, {"q": Noul("q")})

    run(exercise())
    assert fake.requests == []


def test_which_providers_read_images():
    readers = {name: p.images for name, p in PROVIDERS.items() if p.images}
    assert readers == {"typesafe": "assets", "openrouter": "parts", "gateway": "assets"}
    backend = resolve(catalog("openrouter"), "openrouter", require_key=False)
    assert backend.images == "parts"


def test_openrouter_reads_each_image_in_place_as_an_image_url_part(tmp_path):
    fake = Fake()
    router = Backend(
        "openrouter", "https://fixture.invalid/decisions", "typesafe/jev-1.14", key="k", images="parts"
    )
    photo, badge = Image(picture()), Image(picture(color=(0, 0, 255)))
    questions = {"same": Noul(f"Is `door` the person in {badge}?"), "plain": Noul("Is it day?")}

    async def exercise():
        store = tmp_path / "answers.sqlite"
        async with Client(router, transport=httpx.MockTransport(fake), store=store) as client:
            await client.ask({"door": photo, "note": "front"}, questions)
            plan = client.plan({"door": photo, "note": "front"}, questions)
            return plan

    plan = run(exercise())
    request, body = fake.requests[0]
    assert "assets" not in body and next(iter(HEADER)) not in request.headers
    door = body["state"]["door"]
    assert door["type"] == "image_url" and door["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert base64.b64decode(door["image_url"]["url"].split(",", 1)[1]) == photo.prepare().data
    assert body["state"]["note"] == "front"
    before, part, after = body["questions"]["same"]["instructions"]
    assert (before, after) == ("Is `door` the person in ", "?") and part["type"] == "image_url"
    assert body["questions"]["plain"]["instructions"] == "Is it day?"
    assert plan.complete  # answers are keyed on the references, not on the parts sent
