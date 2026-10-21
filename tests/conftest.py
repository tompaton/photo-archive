from pathlib import Path

import pytest
from PIL import Image

from photo_archive_v3 import config
from photo_archive_v3.db import connect
from photo_archive_v3.progress import Progress


@pytest.fixture
def workspace(tmp_path):
    source = tmp_path / "source"
    original = tmp_path / "original"
    final = tmp_path / "final"
    for path in (source, original, final):
        path.mkdir()
    database = tmp_path / "state" / "archive.sqlite"
    with connect(database) as db:
        config.source(db, str(source), database)
        config.roots(db, str(original), str(final), database)
        config.camera(
            db, "CAM", "Camera", "{camera}/{filename}", "video/{camera}/{filename}"
        )
        config.filename_rule(
            db,
            {
                "pattern": r"(?P<date>\d{8}_\d{6})",
                "camera_id": "CAM",
                "timestamp_group": "date",
                "timestamp_format": "%Y%m%d_%H%M%S",
            },
        )
    return source, original, final, database


@pytest.fixture
def progress():
    return Progress(lambda message: None)


def image(path: Path, size=(2400, 1600), color="red", exif=None):
    Image.new("RGB", size, color).save(path, exif=exif or Image.Exif())
    return path
