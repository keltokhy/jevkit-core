"""Probe a provider's image support: does it accept the body, and how many input tokens does an image bill?

    TYPESAFE_API_KEY=... uv run python scripts/image_probe.py [--api typesafe] [--model jev-1.13.0]

Sends a text-only baseline, then one request per image size with the same question, and prints each
request's reported input tokens beside the image's pixels, so `TOKENS_PER_PIXEL` can be checked. Also
asks two questions whose answers are obvious from the pictures, as a sanity check that the model sees
them. Costs a few cents at most; the store is not used.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import io
import json

from PIL import Image as Pil
from PIL import ImageDraw

from jevkit_runtime import Client, Image, Noul, catalog, resolve
from jevkit_runtime.image import TOKENS_PER_PIXEL

SIZES = [(280, 280), (512, 512), (1024, 768), (1568, 1176), (1568, 1568)]


def drawing(size, shape: str) -> bytes:
    picture = Pil.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(picture)
    w, h = size
    box = (w // 4, h // 4, 3 * w // 4, 3 * h // 4)
    if shape == "circle":
        draw.ellipse(box, fill=(220, 20, 20))
    else:
        draw.rectangle(box, fill=(20, 40, 220))
    out = io.BytesIO()
    picture.save(out, "PNG")
    return out.getvalue()


async def main(api: str, model: str | None) -> None:
    # Probed whether or not the catalog marks the provider as reading images: that is what is being checked.
    backend = resolve(catalog(api), api, model=model)
    backend = dataclasses.replace(backend, images=backend.images or "assets")
    question = {"q": Noul("Does `picture` show a red circle?")}
    rows = []
    async with Client(backend, timeout=60) as client:
        base = await client.ask({"picture": "a blank white square"}, question)
        text_tokens = client.meter.input_tokens
        rows.append({"what": "text only", "tokens": text_tokens, "p": base["q"]["noul"]})
        for size in SIZES:
            before = client.meter.input_tokens
            answers = await client.ask({"picture": Image(drawing(size, "circle"))}, question)
            tokens = client.meter.input_tokens - before
            prepared = Image(drawing(size, "circle")).prepare()
            rows.append(
                {
                    "what": f"circle {prepared.width}x{prepared.height}",
                    "tokens": tokens,
                    "image_tokens": tokens - text_tokens,
                    "estimated": prepared.tokens,
                    "pixels_per_token": round(
                        prepared.width * prepared.height / max(1, tokens - text_tokens), 1
                    ),
                    "p": answers["q"]["noul"],
                }
            )
        square = await client.ask({"picture": Image(drawing((512, 512), "square"))}, question)
        rows.append({"what": "blue square 512x512 (expect low p)", "p": square["q"]["noul"]})
        reference = Image(drawing((640, 640), "circle"))  # a different file, the same shape
        inline = await client.ask(
            {"picture": Image(drawing((512, 512), "circle"))},
            {"q": Noul(f"Does `picture` show the same shape as this picture: {reference}?")},
        )
        rows.append({"what": "image in instructions, same shape", "p": inline["q"]["noul"]})
        rows.append({"what": "summary", "meter": client.meter.summary(), "model": client.meter.model})
    print(f"TOKENS_PER_PIXEL in use: {TOKENS_PER_PIXEL:.6f}")
    for row in rows:
        print(json.dumps(row))
    sized = [row for row in rows if "image_tokens" in row]
    if sized and max(row["image_tokens"] for row in sized) < min(row["estimated"] for row in sized) / 4:
        print(f"WARNING: {api} bills almost nothing for images of any size; it is probably dropping them")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="typesafe")
    parser.add_argument("--model")
    args = parser.parse_args()
    asyncio.run(main(args.api, args.model))
