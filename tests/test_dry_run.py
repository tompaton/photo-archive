from unittest.mock import patch

from conftest import image

from photo_archive_v3.analysis import analyze, override
from photo_archive_v3.cli import main
from photo_archive_v3.db import connect, photos
from photo_archive_v3.progress import Progress
from photo_archive_v3.scan import scan
from photo_archive_v3.sync import sync


def test_cli_dry_run_reports_actions_without_writes(workspace, progress, capsys):
    source, original, final, database = workspace
    image(source / "20260102_030405.jpg")
    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)
        before = photos(db)
    with (
        patch("photo_archive_v3.sync.copy_original") as copy,
        patch("photo_archive_v3.sync.generate_final") as generate,
    ):
        assert main(["--db", str(database), "sync", "--dry-run"]) == 0
        copy.assert_not_called()
        generate.assert_not_called()
    output = capsys.readouterr()
    assert "1 would archive, 0 errors" in output.out
    assert "Sync 1/1" in output.err
    assert "Checking source and destinations" in output.err
    assert "Would copy original" in output.err
    assert "Would generate final" in output.err
    assert not list(original.iterdir())
    assert not list(final.iterdir())
    with connect(database) as db:
        assert photos(db) == before
        sync(db, progress)
        assert len(photos(db, "archived")) == 1


def test_dry_run_reports_existing_files_and_errors_without_status_updates(
    workspace, progress
):
    source, original, final, database = workspace
    path = image(source / "20260102_030405.jpg")
    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)
        photo = photos(db)[0]
        destination = original / photo["result_original_path"]
        destination.parent.mkdir(parents=True)
        path.copy(destination)
        rendered = final / photo["result_final_path"]
        rendered.parent.mkdir(parents=True)
        rendered.write_bytes(b"existing final")
        messages = []
        assert "1 would archive" in sync(db, Progress(messages.append), dry_run=True)
        assert any("Accept matching original" in m for m in messages)
        assert any("Accept existing final" in m for m in messages)
        assert photos(db)[0] == photo
        destination.unlink()
        messages.clear()
        assert "1 errors" in sync(db, Progress(messages.append), dry_run=True)
        assert any("Final clash without matching original" in m for m in messages)
        assert photos(db)[0] == photo
        assert not destination.exists()
        assert rendered.read_bytes() == b"existing final"
        rendered.unlink()
        override(db, [photo["id"]], "final", True)
        messages.clear()
        sync(db, Progress(messages.append), dry_run=True)
        assert "Sync 1/1: Skip final: configured to skip" in messages
