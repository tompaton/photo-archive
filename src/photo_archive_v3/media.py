"""Loose files and indexed ZIP members; metadata, hashing and image conversion."""

import hashlib
import json
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, cast

import exifread
from PIL import Image, ImageOps

from .db import Row

PHOTOS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".tif",
    ".tiff",
    ".gif",
    ".bmp",
    ".webp",
    ".heic",
    ".heif",
    ".avif",
    ".dng",
    ".cr2",
    ".cr3",
    ".nef",
    ".arw",
    ".orf",
    ".rw2",
}
VIDEOS = {".mp4", ".mov", ".avi", ".mkv", ".mts", ".m4v", ".3gp", ".mpg", ".mpeg"}


def kind(name: str) -> str | None:
    suffix = Path(name).suffix.lower()
    if suffix in PHOTOS:
        return "photo"
    return "video" if suffix in VIDEOS else None


@contextmanager
def open_photo(photo: Row):  # noqa: ANN201
    path = Path(photo["source_path"])
    if photo["zip_index"] < 0:
        with path.open("rb") as stream:
            yield stream
    else:
        with zipfile.ZipFile(path) as archive:
            info = archive.infolist()[photo["zip_index"]]
            if info.filename != photo["zip_member"]:
                raise ValueError("ZIP entry changed; rescan required")
            with archive.open(info) as stream:
                yield stream


def hash_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    while block := stream.read(1024 * 1024):
        digest.update(block)
    return digest.hexdigest()


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hash_stream(stream)


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        text = value.strip()
        if len(text) > 10 and text[4] == ":":
            text = text[:10].replace(":", "-") + text[10:]
        return datetime.fromisoformat(text).isoformat()
    except ValueError:
        return None


def observe(photo: Row) -> Row:
    with open_photo(photo) as stream:
        digest = hash_stream(stream)
        stream.seek(0)
        tags = (
            exifread.process_file(
                cast(BinaryIO, stream), details=False, extract_thumbnail=False
            )
            if photo["extracted_kind"] == "photo"
            else {}
        )
    raw = {key: str(value) for key, value in tags.items()}
    date = parse_date(raw.get("EXIF DateTimeOriginal")) or parse_date(
        raw.get("EXIF DateTimeDigitized")
    )
    offset = raw.get("EXIF OffsetTimeOriginal")
    if date and offset and datetime.fromisoformat(date).tzinfo is None:
        date = parse_date(date + offset) or date
    return {
        "extracted_sha256": digest,
        "extracted_exif_json": json.dumps(raw),
        "extracted_make": raw.get("Image Make"),
        "extracted_model": raw.get("Image Model"),
        "extracted_timestamp": date,
    }


def generate_final(photo: Row, destination: Path) -> None:
    with open_photo(photo) as stream, Image.open(stream) as image:
        has_exif = bool(image.getexif())
        image = ImageOps.exif_transpose(image)
        image.thumbnail((1920, 1080), Image.Resampling.LANCZOS)
        exif = image.getexif()
        if (
            not has_exif
            and photo.get("result_timestamp_source") in {"filename", "filesystem"}
            and photo.get("result_timestamp")
        ):
            date = datetime.fromisoformat(photo["result_timestamp"])
            stamp = date.strftime("%Y:%m:%d %H:%M:%S")
            exif[306] = stamp  # DateTime
            capture = {36867: stamp, 36868: stamp}  # Original and digitized times
            if date.microsecond:
                fraction = f"{date.microsecond:06d}"
                capture.update({37520: fraction, 37521: fraction, 37522: fraction})
            if date.utcoffset() is not None:
                offset = date.strftime("%z")
                offset = offset[:3] + ":" + offset[3:]
                capture.update({36880: offset, 36881: offset, 36882: offset})
            exif[34665] = capture
        image.convert("RGB").save(
            destination,
            "JPEG",
            quality=90,
            exif=exif,
            icc_profile=image.info.get("icc_profile") if image.mode == "RGB" else None,
        )


def check_format(photo: Row) -> None:
    with open_photo(photo) as stream, Image.open(stream) as image:
        image.verify()
