import os
from datetime import datetime

import pytest
from conftest import image
from PIL import Image

from photo_archive_v3 import config
from photo_archive_v3.analysis import analyze
from photo_archive_v3.db import connect, photos
from photo_archive_v3.media import generate_final
from photo_archive_v3.scan import scan
from photo_archive_v3.sync import regenerate, sync


@pytest.mark.parametrize("date_source", ["filename", "filesystem"])
def test_sync_and_regenerate_synthesize_exif(workspace, progress, date_source):
    source, original, final, database = workspace
    name = "20260102_030405.jpg" if date_source == "filename" else "undated.jpg"
    path = image(source / name)
    modified = datetime(2012, 6, 15, 12, 34, 56).timestamp()
    os.utime(path, (modified, modified))
    expected = (
        "2026:01:02 03:04:05" if date_source == "filename" else "2012:06:15 12:34:56"
    )
    before = path.read_bytes()
    with connect(database) as db:
        if date_source == "filesystem":
            config.filename_rule(db, {"pattern": "undated", "camera_id": "CAM"})
        scan(db, progress)
        analyze(db, progress)
        assert "1 archived" in sync(db, progress)
        photo = photos(db)[0]
        assert photo["result_timestamp_source"] == date_source
        destination = final / photo["result_final_path"]
        for regenerate_final in (False, True):
            if regenerate_final:
                path.unlink()
                assert "Regenerated 1" in regenerate(db, [photo["id"]], progress)
            with Image.open(destination) as result:
                exif = result.getexif()
                assert exif[306] == expected
                assert exif.get_ifd(34665)[36867] == expected
                assert exif.get_ifd(34665)[36868] == expected
                if date_source == "filename":
                    assert 36881 not in exif.get_ifd(34665)
                else:
                    offset = datetime.fromisoformat(photo["result_timestamp"]).strftime(
                        "%z"
                    )
                    assert exif.get_ifd(34665)[36881] == offset[:3] + ":" + offset[3:]
        assert (original / photo["result_original_path"]).read_bytes() == before


def test_synthetic_exif_keeps_fraction_and_known_offset(tmp_path):
    source = image(tmp_path / "source.jpg")
    destination = tmp_path / "final.jpg"
    generate_final(
        {
            "source_path": str(source),
            "zip_index": -1,
            "result_timestamp_source": "filename",
            "result_timestamp": "2026-01-02T03:04:05.123456+10:00",
        },
        destination,
    )
    with Image.open(destination) as result:
        capture = result.getexif().get_ifd(34665)
        assert capture[36867] == "2026:01:02 03:04:05"
        assert capture[37521] == "123456"
        assert capture[36881] == "+10:00"
