from conftest import image

from photo_archive_v3 import config
from photo_archive_v3.analysis import analyze, override
from photo_archive_v3.db import connect, photos
from photo_archive_v3.scan import scan
from photo_archive_v3.sync import sync


def test_same_second_names_follow_filename_order_and_skips(workspace, progress):
    source, _, _, database = workspace
    # Different fractions of the same second must also receive distinct names.
    for name, color in [("c", "blue"), ("a", "red"), ("b", "green")]:
        image(source / f"{name}.jpg", color=color)
    with connect(database) as db:
        config.filename_rule(db, {"pattern": ".*", "camera_id": "CAM"})
        scan(db, progress)
        with db:
            db.execute(
                "UPDATE photos SET extracted_timestamp='2026-01-02T03:04:05.' || id"
            )
        analyze(db, progress)
        catalog = photos(db)
        base = "2026_01/2026_01_02-03_04_05"
        assert [p["result_final_path"] for p in catalog] == [
            f"{base}.jpg",
            f"{base}1.jpg",
            f"{base}2.jpg",
        ]
        assert all(p["queue"] == "ready" for p in catalog)
        override(db, [catalog[0]["id"]], "original", True)
        assert [
            p["result_final_path"] for p in photos(db) if not p["result_skip_final"]
        ] == [f"{base}.jpg", f"{base}1.jpg"]
        override(db, [catalog[0]["id"]], "original", False)
        assert [p["result_final_path"] for p in photos(db)] == [
            f"{base}.jpg",
            f"{base}1.jpg",
            f"{base}2.jpg",
        ]
        analyze(db, progress)
        assert [p["result_final_path"] for p in photos(db)] == [
            f"{base}.jpg",
            f"{base}1.jpg",
            f"{base}2.jpg",
        ]
        sync(db, progress)
        assert len(photos(db, "archived")) == 3


def test_new_earlier_filename_preserves_archived_final(workspace, progress):
    source, _, final, database = workspace
    image(source / "b.jpg", color="red")
    with connect(database) as db:
        config.filename_rule(db, {"pattern": ".*", "camera_id": "CAM"})
        scan(db, progress)
        with db:
            db.execute("UPDATE photos SET extracted_timestamp='2026-01-02T03:04:05'")
        analyze(db, progress)
        sync(db, progress)
        original = photos(db)[0]
        name = original["result_final_path"]
        content = (final / name).read_bytes()
        image(source / "a.jpg", color="blue")
        scan(db, progress)
        with db:
            db.execute("UPDATE photos SET extracted_timestamp='2026-01-02T03:04:05'")
        analyze(db, progress)
        catalog = photos(db)
        assert catalog[0]["result_final_path"] == "2026_01/2026_01_02-03_04_051.jpg"
        assert catalog[1]["result_final_path"] == name
        sync(db, progress)
        assert len(photos(db, "archived")) == 2
        assert (final / name).read_bytes() == content
