"""Images as inputs: a picture goes anywhere text goes, and the client sends it as an asset.

    from jevkit_runtime import Image, Noul
    photo = Image("door.jpg")
    answers = await client.ask({"door": photo}, {"q": Noul("Is there a person in `door`?")})

An `Image` is a string: its reference, `img-` and a digest of the source bytes and of how they are
prepared. It serializes, keys answers and fingerprints runs as that reference, so the same picture
always has the same identity and a changed one never reuses an answer. The client finds references in
a request when it sends it and adds the prepared files under the request's `assets`, with the header
TypeSafe asks for (https://docs.typesafe.ai/images). A reference written into a longer string, such as
an f-string question, is found the same way.

Preparing follows TypeSafe's limits: pixels rotated upright from EXIF, the longest side at most 1568,
each side at least 256 and the area at least 280 x 280 (scaled up, then padded), sent as baseline JPEG,
or PNG when the picture has transparency. Animated and unreadable files are refused. Pillow is needed,
from the `images` extra: `pip install 'jevkit-runtime[images]'`.

Images are experimental: TypeSafe calls them "Dreamer-ready", and the format may change.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import io
import math
import os
import re
from dataclasses import dataclass

from .errors import JevError

PREPARE_VERSION = "jevkit/image/v1"  # bump when preparation changes the pixels sent
HEADER = {"TypeSafe-Danger-Zone-Images": "true"}
LONGEST = 1568
SHORTEST = 256
MIN_AREA = 280 * 280
MAX_FILE_BYTES = 16 * 2**20
MAX_BODY_BYTES = 32 * 2**20
# Input tokens an image bills, per pixel. Measured on Jev 1.13.0 on 2026-10-16 with scripts/image_probe.py:
# 339 pixels a token at 280 x 280, rising to 390 at 1568 x 1568. 1/340 keeps reservations at or above the
# charge. Only reservations and limit checks use it; the budget settles what the API reports.
TOKENS_PER_PIXEL = 1 / 340

REFERENCE = re.compile(r"img-[0-9a-f]{32}")
_known: dict[str, Image] = {}


@dataclass(frozen=True)
class Prepared:
    data: bytes
    format: str  # "jpg" or "png"
    width: int
    height: int

    @property
    def tokens(self) -> int:
        return math.ceil(self.width * self.height * TOKENS_PER_PIXEL)


class Image(str):
    """A picture, by path or bytes, standing in a state, a question or criteria as its reference."""

    source: str | bytes

    def __new__(cls, source: str | os.PathLike | bytes):
        if isinstance(source, (bytes, bytearray, memoryview)):
            kept: str | bytes = bytes(source)
            data = kept
        elif isinstance(source, (str, os.PathLike)):
            kept = os.fspath(source)
            try:
                with open(kept, "rb") as f:
                    data = f.read()
            except OSError as exc:
                raise JevError(f"cannot read image {kept}: {exc.strerror or exc}") from None
        else:
            raise TypeError("an Image is made from a path or bytes")
        digest = hashlib.sha256(PREPARE_VERSION.encode() + b"\0" + data).hexdigest()
        self = super().__new__(cls, f"img-{digest[:32]}")
        self.source = kept
        _known.setdefault(str(self), self)
        return self

    def __reduce__(self):
        return str, (str(self),)  # across processes it is only its reference

    def __repr__(self) -> str:
        where = self.source if isinstance(self.source, str) else f"{len(self.source):,} bytes"
        return f"Image({where!r} as {str(self)})"

    def prepare(self) -> Prepared:
        """The file as it is sent. Raises JevError if it cannot be."""
        return _prepare(str(self))

    @property
    def tokens(self) -> int:
        return self.prepare().tokens


def _read(image: Image) -> bytes:
    if isinstance(image.source, bytes):
        return image.source
    try:
        with open(image.source, "rb") as f:
            data = f.read()
    except OSError as exc:
        raise JevError(f"cannot read image {image.source}: {exc.strerror or exc}") from None
    if hashlib.sha256(PREPARE_VERSION.encode() + b"\0" + data).hexdigest()[:32] != str(image)[4:]:
        raise JevError(f"image {image.source} changed after it was read; read it again")
    return data


@functools.lru_cache(maxsize=64)
def _prepare(reference: str) -> Prepared:
    image = _known[reference]
    name = image.source if isinstance(image.source, str) else "image"
    try:
        from PIL import Image as Pil
        from PIL import ImageOps, UnidentifiedImageError
    except ImportError:
        raise JevError("images need Pillow: pip install 'jevkit-runtime[images]'") from None
    try:
        with Pil.open(io.BytesIO(_read(image))) as opened:
            if getattr(opened, "n_frames", 1) > 1:
                raise JevError(f"{name} is animated; send one frame as a PNG or JPEG")
            picture = ImageOps.exif_transpose(opened)
            picture.load()
    except (UnidentifiedImageError, OSError, ValueError, Pil.DecompressionBombError) as exc:
        raise JevError(f"cannot read {name} as an image: {exc}") from None
    alpha = picture.mode in ("RGBA", "LA", "PA") or (picture.mode == "P" and "transparency" in picture.info)
    picture = picture.convert("RGBA" if alpha else "RGB")
    width, height = picture.size
    scale = min(
        LONGEST / max(width, height),
        max(1.0, SHORTEST / width, SHORTEST / height, math.sqrt(MIN_AREA / (width * height))),
    )
    if scale != 1:
        size = (max(1, min(LONGEST, round(width * scale))), max(1, min(LONGEST, round(height * scale))))
        picture = picture.resize(size, Pil.Resampling.LANCZOS)
    width, height = picture.size
    if width < SHORTEST or height < SHORTEST or width * height < MIN_AREA:
        padded = (max(width, SHORTEST), max(height, SHORTEST))
        if padded[0] * padded[1] < MIN_AREA:
            side = math.ceil(math.sqrt(MIN_AREA))
            padded = (max(padded[0], side), max(padded[1], side))
        canvas = Pil.new(picture.mode, padded, (0, 0, 0, 0) if alpha else (255, 255, 255))
        canvas.paste(picture, ((padded[0] - width) // 2, (padded[1] - height) // 2))
        picture = canvas
    out = io.BytesIO()
    if alpha:
        picture.save(out, "PNG", optimize=False)
    else:
        picture.save(out, "JPEG", quality=90, progressive=False)
    data = out.getvalue()
    if len(data) > MAX_FILE_BYTES:
        raise JevError(f"{name} is {len(data) / 2**20:.1f} MiB once prepared, over the 16 MiB limit")
    return Prepared(data, "png" if alpha else "jpg", *picture.size)


def references(text: str) -> list[str]:
    """The known images written in `text`, once per use: each use is another copy the model reads."""
    return [found for found in REFERENCE.findall(text) if found in _known]


def image_tokens(text: str) -> int:
    """Input tokens the images written in `text` are estimated to bill."""
    return sum(_prepare(reference).tokens for reference in references(text))


def assets(text: str) -> dict[str, dict]:
    """The `assets` a request whose JSON is `text` must carry: every image it names, once."""
    return {
        reference: {"type": "image", "format": prepared.format, "data_base64": _b64(prepared.data)}
        for reference in dict.fromkeys(references(text))
        for prepared in (_prepare(reference),)
    }


def part(reference: str) -> dict:
    """One image as the `image_url` part OpenRouter reads in place of its reference."""
    prepared = _prepare(reference)
    kind = "png" if prepared.format == "png" else "jpeg"
    return {"type": "image_url", "image_url": {"url": f"data:image/{kind};base64,{_b64(prepared.data)}"}}


def inline(value):
    """`value` with each image written in it replaced by its part, for providers that read images in place.

    A string that is exactly a reference becomes the part; a longer string that names images becomes a
    list of its text and parts, in order. Object keys stay text, as they do for TypeSafe's references.
    """
    if isinstance(value, dict):
        return {key: inline(item) for key, item in value.items()}
    if isinstance(value, list):
        return [inline(item) for item in value]
    if not isinstance(value, str) or not references(value):
        return value
    if value in _known:
        return part(value)
    pieces: list = []
    at = 0
    for found in REFERENCE.finditer(value):
        if found.group() not in _known:
            continue
        if found.start() > at:
            pieces.append(value[at : found.start()])
        pieces.append(part(found.group()))
        at = found.end()
    if at < len(value):
        pieces.append(value[at:])
    return pieces


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
