import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import image
from PIL import Image
from textual.widgets import Tabs

from photo_archive_v3.analysis import analyze, override
from photo_archive_v3.db import connect, photos
from photo_archive_v3.media import generate_final
from photo_archive_v3.progress import Progress
from photo_archive_v3.scan import scan
from photo_archive_v3.sync import regenerate, sync
from photo_archive_v3.tui import ArchiveApp


def archive_photo(source, db, progress):
    exif = Image.Exif()
    exif[271] = "Test camera"
    path = image(source / "20260102_030405.jpg", exif=exif)
    scan(db, progress)
    analyze(db, progress)
    sync(db, progress)
    return path, photos(db)[0]


def test_regenerate_uses_verified_archived_original_without_source(workspace, progress):
    source, _, final, database = workspace
    with connect(database) as db:
        path, photo = archive_photo(source, db, progress)
        path.unlink()
        destination = final / photo["result_final_path"]
        image(destination, size=(20, 20), color="blue")
        messages = []
        assert "Regenerated 1 finals" in regenerate(
            db, [photo["id"]], Progress(messages.append)
        )
        assert all(m.startswith("Regenerate 1/1:") for m in messages)
        with Image.open(destination) as result:
            assert result.size == (1620, 1080)
            assert result.getexif()[271] == "Test camera"
        assert photos(db)[0] == photo
        original = Path(photo["status_original_archived_path"])
        original.write_bytes(b"changed original")
        content = destination.read_bytes()
        assert "1 errors" in regenerate(db, [photo["id"]], progress)
        assert destination.read_bytes() == content
        assert photos(db)[0]["status_error_stage"] == "regenerate"
        assert "hash mismatch" in photos(db)[0]["status_error_message"]


def test_regenerate_failure_preserves_final_and_can_be_retried(workspace, progress):
    source, _, final, database = workspace
    with connect(database) as db:
        _, photo = archive_photo(source, db, progress)
        destination = final / photo["result_final_path"]
        content = destination.read_bytes()
        with patch(
            "photo_archive_v3.sync.generate_final",
            side_effect=OSError("conversion failed"),
        ):
            assert "1 errors" in regenerate(db, [photo["id"]], progress)
        assert destination.read_bytes() == content
        assert photos(db)[0]["queue"] == "errors"
        assert "Regenerated 1 finals" in regenerate(db, [photo["id"]], progress)
        assert photos(db)[0]["queue"] == "archived"
        override(db, [photo["id"]], "final", True)
        with patch("photo_archive_v3.sync.generate_final") as converter:
            assert "1 ineligible" in regenerate(db, [photo["id"]], progress)
            converter.assert_not_called()


@pytest.mark.parametrize("bulk", [False, True])
def test_tui_regenerate_focused_or_selected_archived_photo(workspace, progress, bulk):
    source, _, _, database = workspace
    with connect(database) as db:
        archive_photo(source, db, progress)

    async def run():
        app = ArchiveApp(database)
        async with app.run_test(size=(180, 60)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one("#workflow", Tabs).active = "archived"
            await pilot.pause()
            await app.workers.wait_for_complete()
            if bulk:
                app.selection("all")
            with patch(
                "photo_archive_v3.sync.generate_final", wraps=generate_final
            ) as converter:
                await pilot.click("#regenerate")
                await app.workers.wait_for_complete()
                converter.assert_called_once()
            assert app.visible_photos[0]["queue"] == "archived"

    asyncio.run(run())
