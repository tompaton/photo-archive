import zipfile
from unittest.mock import patch

import pytest
from conftest import image
from PIL import Image

from photo_archive_v3 import config
from photo_archive_v3.analysis import analyze, matches, override
from photo_archive_v3.db import connect, photos, rows
from photo_archive_v3.progress import Cancelled, Progress
from photo_archive_v3.scan import scan
from photo_archive_v3.sync import StaleConfirmation, sync


def test_complete_workflow_and_cached_analysis(workspace, progress):
    source, original, final, database = workspace
    path = image(source / "20260102_030405.jpg")
    before = path.read_bytes()
    with connect(database) as db:
        scan(db, progress)
        assert photos(db)[0]["queue"] == "pending"
        analyze(db, progress)
        assert photos(db)[0]["queue"] == "ready"
        with patch(
            "photo_archive_v3.scan.observe", side_effect=AssertionError("cached")
        ):
            scan(db, progress)
        sync(db, progress)
        assert photos(db)[0]["queue"] == "archived"
        assert (original / "CAM" / path.name).read_bytes() == before
        output = final / "2026_01/2026_01_02-03_04_05.jpg"
        with Image.open(output) as result:
            assert result.size == (1620, 1080)
        assert path.read_bytes() == before
        assert "0 archived" in sync(db, progress)


@pytest.mark.parametrize(
    "original_state,has_final,expected",
    [
        ("match", True, "archived"),
        ("match", False, "archived"),
        ("absent", True, "errors"),
        ("absent", False, "archived"),
        ("different", True, "errors"),
        ("different", False, "errors"),
    ],
)
def test_existing_nas_matrix(workspace, progress, original_state, has_final, expected):
    source, original, final, database = workspace
    path = image(source / "20260102_030405.jpg")
    target = original / "CAM" / path.name
    target.parent.mkdir()
    if original_state != "absent":
        target.write_bytes(
            path.read_bytes() if original_state == "match" else b"other original"
        )
    output = final / "2026_01/2026_01_02-03_04_05.jpg"
    output.parent.mkdir()
    if has_final:
        output.write_bytes(b"historical final, no decode/hash required")
    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)
        if has_final:
            with patch(
                "photo_archive_v3.sync.generate_final",
                side_effect=AssertionError("must not regenerate"),
            ):
                sync(db, progress)
        else:
            sync(db, progress)
        assert photos(db)[0]["queue"] == expected
    if has_final:
        assert output.read_bytes() == b"historical final, no decode/hash required"
    if original_state == "absent" and has_final:
        assert not target.exists()
    if original_state == "different":
        assert target.read_bytes() == b"other original"


def test_pending_does_not_block_ready_and_video_always_skips_final(workspace, progress):
    source, original, final, database = workspace
    image(source / "unknown.jpg")
    (source / "20260102_030405.mp4").write_bytes(b"video")
    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)
        video = next(p for p in photos(db) if p["extracted_kind"] == "video")
        override(db, [video["id"]], "final", False)
        assert next(p for p in photos(db) if p["id"] == video["id"])[
            "result_skip_final"
        ]
        sync(db, progress)
        assert {p["queue"] for p in photos(db)} == {"pending", "archived"}
        assert not list(final.iterdir())
        assert (original / "video/CAM/20260102_030405.mp4").exists()


def test_stale_keeps_results_requires_confirmation(workspace, progress):
    source, _, _, database = workspace
    image(source / "20260102_030405.jpg")
    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)
        prior = photos(db)[0]
        config.filename_rule(db, {"pattern": "never", "skip_final": 1})
        current = photos(db)[0]
        assert current["queue"] == "ready" and current["stale"]
        assert current["result_original_path"] == prior["result_original_path"]
        with pytest.raises(StaleConfirmation):
            sync(db, progress)
        sync(db, progress, allow_stale=True)
        assert photos(db)[0]["queue"] == "archived"


def test_archived_duplicate_needs_no_analysis_or_attention(workspace, progress):
    source, _, _, database = workspace
    path = image(source / "20260102_030405.jpg")
    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)
        sync(db, progress)
        (source / "new-name.jpg").write_bytes(path.read_bytes())
        scan(db, progress)
        duplicate = next(p for p in photos(db) if p["basename"] == "new-name.jpg")
        assert duplicate["queue"] == "duplicates"
        assert duplicate["status_analyzed_at"] is None
        assert duplicate["duplicate_note"] == "Already archived — no action needed"
        assert len(photos(db, "archived")) == 1
        assert not photos(db, "ready")


def test_new_duplicates_and_target_clashes_need_attention(workspace, progress):
    source, _, _, database = workspace
    path = image(source / "20260102_030405.jpg")
    sub = source / "sub"
    sub.mkdir()
    (sub / path.name).write_bytes(path.read_bytes())
    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)
        assert len(photos(db, "duplicates")) == 2
        ids = [p["id"] for p in photos(db)]
        override(db, [ids[0]], "original", True)
        override(db, [ids[0]], "final", True)
        assert len(photos(db, "ready")) == 1
        # Different bytes, same target filename, also clash.
        override(db, [ids[0]], "original", False)
        override(db, [ids[0]], "final", False)
        image(sub / path.name, color="blue")
        scan(db, progress)
        analyze(db, progress)
        assert len(photos(db, "duplicates")) == 2


def test_zip_member_identity_view_hash_and_sync(workspace, progress):
    source, original, _, database = workspace
    path = image(source / "20260102_030405.jpg")
    data = path.read_bytes()
    path.unlink()
    with zipfile.ZipFile(source / "photos.zip", "w") as archive:
        archive.writestr("trip/20260102_030405.jpg", data)
        archive.writestr("trip/20260102_030406.jpg", data)
    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)
        items = photos(db)
        assert len(items) == 2 and len({p["extracted_sha256"] for p in items}) == 1
        override(db, [items[1]["id"]], "original", True)
        override(db, [items[1]["id"]], "final", True)
        sync(db, progress)
        assert (original / "CAM/20260102_030405.jpg").read_bytes() == data


def test_source_mismatch_and_unsupported_format_do_not_copy(workspace, progress):
    source, original, _, database = workspace
    path = image(source / "20260102_030405.jpg")
    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)
        image(path, color="blue")
        sync(db, progress)
        assert "no longer matches" in photos(db)[0]["status_error_message"]
        assert not list(original.iterdir())
    path.unlink()
    (source / "20260102_030406.heic").write_bytes(b"unsupported HEIC sample")
    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)
        sync(db, progress)
        assert all(p["queue"] == "errors" for p in photos(db))
        assert not list(original.iterdir())


def test_final_failure_keeps_successful_original(workspace, progress):
    source, original, _, database = workspace
    path = image(source / "20260102_030405.jpg")
    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)
        with patch(
            "photo_archive_v3.sync.generate_final", side_effect=OSError("decode failed")
        ):
            sync(db, progress)
        item = photos(db)[0]
        assert item["queue"] == "errors"
        assert item["status_original_archived_path"]
        assert not item["status_final_archived_path"]
        assert (original / "CAM" / path.name).exists()


def test_skipping_original_also_skips_final_and_reset_restores_work(
    workspace, progress
):
    source, original, final, database = workspace
    image(source / "20260102_030405.jpg")
    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)
        item = photos(db)[0]
        override(db, [item["id"]], "original", True)
        sync(db, progress)
        assert photos(db)[0]["queue"] == "skipped"
        assert photos(db)[0]["result_skip_final"] == 1
        assert not list(original.iterdir())
        assert not list(final.iterdir())
        override(db, [item["id"]], "final", False)
        analyze(db, progress)
        assert photos(db)[0]["queue"] == "skipped"
        override(db, [item["id"]], "original", None)
        assert photos(db)[0]["queue"] == "ready"
        sync(db, progress)
        assert photos(db)[0]["queue"] == "archived"
        assert list(original.rglob("*.jpg"))
        assert list(final.rglob("*.jpg"))


def test_exif_precedence_and_rule_stats(workspace, progress):
    source, _, _, database = workspace
    exif = Image.Exif()
    exif[271] = "Maker   "
    exif[272] = "Model"
    image(source / "20260102_030405.jpg", exif=exif)
    with connect(database) as db:
        config.camera(db, "EXIF", "EXIF camera", "{camera}/{filename}", None)
        rule = config.exif_rule(
            db,
            "EXIF",
            [("Image Make", "eq", "Maker"), ("Image Model", "regex", "^Model$")],
        )
        scan(db, progress)
        analyze(db, progress)
        item = photos(db)[0]
        assert item["result_camera_id"] == "EXIF"
        assert item["inferred_camera_exif_rule_id"] == rule
        assert config.statistics(db, "exif_rules")[0]["matches"] == 1
        config.camera(db, "EXIF", "New display name", "{camera}/{filename}", None)
        assert not photos(db)[0]["stale"]
        config.exif_rule(db, "CAM", [("Image Make", "eq", "Maker")])
        analyze(db, progress)
        assert photos(db)[0]["queue"] == "pending"


def test_cancellation_keeps_completed_items(workspace):
    source, _, _, database = workspace
    image(source / "20260102_030405.jpg")
    image(source / "20260102_030406.jpg", color="blue")
    messages = []
    progress = Progress(messages.append, lambda: len(messages) >= 2)
    with connect(database) as db:
        with pytest.raises(Cancelled):
            scan(db, progress)
        assert len(rows(db, "SELECT * FROM photos")) == 1
        assert not rows(db, "SELECT * FROM sources")[0]["last_scan_completed"]


@pytest.mark.parametrize("orientation", [1, 6])
def test_orientation_and_small_images(workspace, progress, orientation):
    source, _, final, database = workspace
    exif = Image.Exif()
    exif[274] = orientation
    exif[271] = "Camera maker"
    exif[272] = "Camera model"
    exif[34665] = {36867: "2026:01:02 03:04:05", 37521: "123", 42036: "Test lens"}
    exif[34853] = {1: "N", 2: (51, 30, 0), 3: "W", 4: (0, 7, 0)}
    image(source / "20260102_030405.jpg", size=(400, 200), exif=exif)
    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)
        sync(db, progress)
    with Image.open(next(final.rglob("*.jpg"))) as result:
        assert result.size == ((200, 400) if orientation == 6 else (400, 200))
        metadata = result.getexif()
        assert metadata.get(274) == (None if orientation == 6 else 1)
        assert metadata[271] == "Camera maker"
        assert metadata[272] == "Camera model"
        assert metadata.get_ifd(34665) == exif[34665]
        assert metadata.get_ifd(34853) == exif[34853]


def test_overlap_cached_content_and_missing_source_retention(workspace, progress):
    source, _, _, database = workspace
    nested = source / "nested"
    nested.mkdir()
    image(nested / "20260102_030405.jpg")
    with connect(database) as db:
        config.source(db, str(nested), database)
        assert "Scanned 1 entries" in scan(db, progress)
        analyze(db, progress)
        sync(db, progress)
        (nested / "20260102_030405.jpg").touch()
        scan(db, progress)
        assert photos(db)[0]["queue"] == "archived"
        source.rename(source.with_name("offline"))
        assert "source errors" in scan(db, progress)
        assert photos(db)[0]["status_present"]


def test_zip_duplicate_names_are_distinct_entries(workspace, progress):
    import warnings

    source, _, _, database = workspace
    first = image(source / "first.jpg").read_bytes()
    second = image(source / "second.jpg", color="blue").read_bytes()
    (source / "first.jpg").unlink()
    (source / "second.jpg").unlink()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(source / "entries.zip", "w") as archive:
            archive.writestr("20260102_030405.jpg", first)
            archive.writestr("20260102_030405.jpg", second)
    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)
        items = photos(db)
        assert len(items) == 2
        assert len({p["zip_index"] for p in items}) == 2
        assert len({p["extracted_sha256"] for p in items}) == 2
        assert all(p["queue"] == "duplicates" for p in items)


def test_analysis_batches_progress_and_commits_with_partial_final_batch(
    workspace, progress
):
    source, _, _, database = workspace
    image(source / "20260102_030405.jpg")
    with connect(database) as db:
        scan(db, progress)
        with db:
            db.executemany(
                """INSERT INTO photos(source_id,source_path,source_folder,basename,extracted_kind,extracted_sha256)
                VALUES(1,?,?,'20260102_030405.jpg','photo',?)""",
                [(str(source / f"{i}.jpg"), str(source), str(i)) for i in range(204)],
            )
        commits, messages = [], []
        db.set_trace_callback(
            lambda sql: commits.append(sql) if sql == "COMMIT" else None
        )
        analyze(db, Progress(messages.append))
        assert len(commits) == 4  # Three inference batches, then final name allocation.
        assert [
            message.split(":")[0]
            for message in messages
            if message.startswith("Analyze ")
        ] == ["Analyze 0/205", "Analyze 100/205", "Analyze 200/205", "Analyze 205/205"]
        assert (
            len(rows(db, "SELECT id FROM photos WHERE status_analyzed_at IS NOT NULL"))
            == 205
        )

        from photo_archive_v3.analysis import infer

        with db:
            db.execute("UPDATE photos SET status_analyzed_at=NULL")
        with patch("photo_archive_v3.analysis.infer", wraps=infer) as inference:
            cancellation = Progress(
                lambda message: None, lambda: inference.call_count >= 125
            )
            with pytest.raises(Cancelled):
                analyze(db, cancellation)
            assert (
                inference.call_count == 125
            )  # Checked between photos, not just batches.
        assert (
            len(rows(db, "SELECT id FROM photos WHERE status_analyzed_at IS NOT NULL"))
            == 100
        )


@pytest.mark.parametrize(
    ("actual", "expected", "match"),
    [
        ("Model   ", "Model", True),
        (" \tModel\r\n", "Model", True),
        ("Model", " Model ", True),
        ("Model  Name ", "Model Name", False),
        ("model ", "Model", False),
    ],
)
def test_exif_equality_ignores_surrounding_whitespace(actual, expected, match):
    raw = {"Image Model": actual}
    assert (
        matches(raw, [{"tag": "Image Model", "operator": "eq", "value": expected}])
        is match
    )
    assert raw["Image Model"] == actual  # Captured EXIF is kept verbatim.


def test_exif_regex_still_matches_raw_whitespace():
    raw = {"Image Model": "Model "}
    assert matches(
        raw, [{"tag": "Image Model", "operator": "regex", "value": r"^Model\s$"}]
    )
    assert not matches(
        raw, [{"tag": "Image Model", "operator": "regex", "value": "^Model$"}]
    )


@pytest.mark.parametrize("zipped", [False, True])
def test_filesystem_date_fallback_generates_year_original_from_cached_facts(
    workspace, progress, zipped
):
    import os
    from datetime import datetime

    source, original, _, database = workspace
    path = image(source / "undated.jpg")
    if zipped:
        container = source / "old.zip"
        with zipfile.ZipFile(container, "w") as archive:
            archive.write(path, "undated.jpg")
        path.unlink()
        path = container
    modified = datetime(2012, 6, 15, 12, 34, 56).timestamp()
    os.utime(path, (modified, modified))
    with connect(database) as db:
        config.camera(
            db, "CAM", "Camera", "{camera}/{year}/{filename}", "MOV/{camera}/{filename}"
        )
        config.filename_rule(
            db, {"pattern": "^undated.jpg$", "camera_id": "CAM", "skip_final": True}
        )
        scan(db, progress)
        analyze(db, progress)
        item = photos(db)[0]
        assert item["extracted_timestamp"] is None
        assert item["inferred_timestamp"] is None
        assert item["result_timestamp"].startswith("2012-06-15T12:34:56")
        assert item["result_timestamp_source"] == "filesystem"
        assert item["result_original_path"] == "CAM/2012/undated.jpg"
        assert item["queue"] == "ready"
        sync(db, progress)
        assert (original / "CAM/2012/undated.jpg").is_file()


@pytest.mark.parametrize(
    ("exif", "filename", "mtime", "expected_source"),
    [
        ("2001-02-03T04:05:06+10:00", "2002-03-04T05:06:07", 0, "exif"),
        (None, "2002-03-04T05:06:07", 0, "filename"),
        (None, None, 0, "filesystem"),
        (None, None, None, None),
    ],
)
def test_date_precedence_includes_filesystem_last(
    exif, filename, mtime, expected_source
):
    from photo_archive_v3.analysis import timestamp

    date, source = timestamp(
        {"extracted_timestamp": exif, "extracted_mtime_ns": mtime},
        {"timestamp": filename},
    )
    assert source == expected_source
    if exif or filename:
        assert date == (exif or filename)
    elif mtime is None:
        assert date is None


def test_original_rule_skip_overrides_explicit_final_unskip(workspace, progress):
    source, original, final, database = workspace
    image(source / "20260102_030405.jpg")
    with connect(database) as db:
        config.filename_rule(
            db,
            {
                "pattern": ".jpg$",
                "priority": 1,
                "camera_id": "CAM",
                "skip_original": True,
                "skip_final": False,
            },
        )
        scan(db, progress)
        analyze(db, progress)
        photo = photos(db)[0]
        override(db, [photo["id"]], "final", False)
        assert photos(db)[0]["queue"] == "skipped"
        assert photos(db)[0]["result_skip_final"] == 1
        sync(db, progress)
        assert not list(original.iterdir()) and not list(final.iterdir())
        override(db, [photo["id"]], "original", False)
        assert photos(db)[0]["queue"] == "ready"
        assert photos(db)[0]["result_skip_final"] == 0
        override(db, [photo["id"]], "original", None)
        assert photos(db)[0]["queue"] == "skipped"


def test_existing_catalog_original_skips_are_corrected_without_analysis(
    workspace, progress
):
    source, original, final, database = workspace
    image(source / "20260102_030405.jpg")
    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)
        analyzed_at = photos(db)[0]["status_analyzed_at"]
        # Reproduce the previous release's independent flags, as seen in Ready.
        with db:
            db.execute(
                "UPDATE photos SET overridden_skip_original=1, overridden_skip_final=0, result_skip_original=1, result_skip_final=0"
            )
            db.execute("PRAGMA user_version=0")
    with connect(database) as db:
        photo = photos(db)[0]
        assert photo["queue"] == "skipped"
        assert photo["result_skip_final"] == 1
        assert (
            photo["overridden_skip_final"] == 0
        )  # Preserve preference for a later original unskip.
        assert photo["status_analyzed_at"] == analyzed_at
        sync(db, progress)
        assert not list(original.iterdir()) and not list(final.iterdir())


def test_final_is_generated_from_verified_nas_original(workspace, progress):
    from photo_archive_v3.media import generate_final

    source, original, _, database = workspace
    path = image(source / "20260102_030405.jpg")

    def generate(photo, destination):
        assert photo["source_path"] == str(original / "CAM" / path.name)
        assert photo["zip_index"] == -1
        assert (original / "CAM" / path.name).read_bytes() == path.read_bytes()
        generate_final(photo, destination)

    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)
        with patch(
            "photo_archive_v3.sync.generate_final", side_effect=generate
        ) as converter:
            sync(db, progress)
        converter.assert_called_once()
        assert photos(db)[0]["queue"] == "archived"


def test_failed_original_copy_never_generates_final(workspace, progress):
    source, _, final, database = workspace
    image(source / "20260102_030405.jpg")
    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)
        with patch(
            "photo_archive_v3.sync.copy_original", side_effect=OSError("copy failed")
        ):
            with patch("photo_archive_v3.sync.generate_final") as converter:
                sync(db, progress)
                converter.assert_not_called()
        assert photos(db)[0]["queue"] == "errors"
        assert not list(final.iterdir())


def test_sync_keeps_photo_count_on_every_stage(workspace, progress):
    source, _, _, database = workspace
    image(source / "20260102_030405.jpg", color="red")
    image(source / "20260102_030406.jpg", color="blue")
    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)
        messages = []
        sync(db, Progress(messages.append))
        for index in (1, 2):
            stages = [m for m in messages if m.startswith(f"Sync {index}/2:")]
            assert len(stages) == 4
            assert "Checking source and destinations" in stages[1]
            assert "Copy original" in stages[2]
            assert "Generate final" in stages[3]
        assert len(messages) == 8
