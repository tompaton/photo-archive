import asyncio

from conftest import image
from textual.widgets import Static, Tabs

from photo_archive_v3.analysis import analyze
from photo_archive_v3.db import connect, photos, update_photo
from photo_archive_v3.scan import scan
from photo_archive_v3.sync import retry, sync
from photo_archive_v3.tui import ArchiveApp


def test_retry_preserves_checkpoints_and_only_clears_selected_sync_errors(
    workspace, progress
):
    source, _, _, database = workspace
    for second, color in [(1, "red"), (2, "blue"), (3, "green")]:
        image(source / f"20260102_03040{second}.jpg", color=color)
    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)
        catalog = photos(db)
        with db:
            for photo, stage in zip(catalog, ["sync", "sync", "scan"], strict=True):
                update_photo(
                    db,
                    photo["id"],
                    {
                        "status_error_stage": stage,
                        "status_error_message": "permission denied",
                    },
                )
            update_photo(
                db,
                catalog[0]["id"],
                {"status_original_archived_path": "/saved/original.jpg"},
            )
        assert "Cleared 1" in retry(db, [catalog[0]["id"], catalog[2]["id"]])
        current = photos(db)
        assert current[0]["queue"] == "ready"
        assert current[0]["status_original_archived_path"] == "/saved/original.jpg"
        assert current[0]["result_final_path"] == catalog[0]["result_final_path"]
        assert current[1]["queue"] == current[2]["queue"] == "errors"
        sync(db, progress)
        assert photos(db)[0]["queue"] == "archived"


def test_tui_retry_selected_errors_returns_them_to_ready(workspace, progress):
    source, _, _, database = workspace
    image(source / "20260102_030405.jpg")
    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)
        with db:
            update_photo(
                db,
                photos(db)[0]["id"],
                {
                    "status_error_stage": "sync",
                    "status_error_message": "permission denied",
                },
            )

    async def run():
        app = ArchiveApp(database)
        async with app.run_test(size=(180, 60)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one("#workflow", Tabs).active = "errors"
            await pilot.pause()
            await app.workers.wait_for_complete()
            app.selection("all")
            await pilot.click("#retry")
            await app.workers.wait_for_complete()
            assert not app.visible_photos
            assert app.items[0]["queue"] == "ready"
            assert "Cleared 1 sync errors" in str(
                app.query_one("#status", Static).content
            )

    asyncio.run(run())


def test_cli_retry_errors_preserves_dry_run_and_other_errors(
    workspace, progress, capsys
):
    from photo_archive_v3.cli import main

    source, _, _, database = workspace
    for second, color in [(1, "red"), (2, "blue"), (3, "green")]:
        image(source / f"20260102_03040{second}.jpg", color=color)
    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)
        catalog = photos(db)
        with db:
            for photo, stage in zip(catalog, ["sync", "scan", None], strict=True):
                if stage:
                    update_photo(
                        db,
                        photo["id"],
                        {
                            "status_error_stage": stage,
                            "status_error_message": "permission denied",
                        },
                    )
        before = photos(db)
    prefix = ["--db", str(database), "sync"]
    assert main(prefix + ["--retry-errors", "--dry-run"]) == 0
    assert "2 would archive" in capsys.readouterr().out
    with connect(database) as db:
        assert photos(db) == before
    assert main(prefix) == 0
    assert "1 archived" in capsys.readouterr().out
    assert main(prefix + ["--retry-errors"]) == 0
    assert "1 archived" in capsys.readouterr().out
    with connect(database) as db:
        current = photos(db)
        assert [p["queue"] for p in current] == ["archived", "errors", "archived"]
        assert current[1]["status_error_stage"] == "scan"
