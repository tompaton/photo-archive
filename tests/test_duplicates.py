import asyncio

from conftest import image
from textual.widgets import Static, Tabs

from photo_archive_v3.analysis import analyze, override
from photo_archive_v3.db import connect, photos
from photo_archive_v3.duplicates import DuplicateIndex
from photo_archive_v3.scan import scan
from photo_archive_v3.tui import ArchiveApp


def test_duplicate_reasons_follow_review_classification(workspace, progress):
    source, _, _, database = workspace
    first = image(source / "20260102_030405.jpg")
    other = source / "other" / first.name
    other.parent.mkdir()
    other.write_bytes(first.read_bytes())
    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)
        catalog = photos(db)
        index = DuplicateIndex(catalog)
        assert len(index.criteria(catalog[0])) == 2
        detail = index.details(catalog[0])
        assert "Matching content (SHA-256)" in detail
        assert "Original destination clash" in detail
        assert "Final destination clash" not in detail
        assert catalog[1]["source_path"] in detail
        assert f"#{catalog[1]['id']} [duplicates]" in detail

        image(other, color="blue")
        scan(db, progress)
        analyze(db, progress)
        catalog = photos(db)
        assert len(DuplicateIndex(catalog).criteria(catalog[0])) == 1
        override(db, [catalog[1]["id"]], "final", True)
        catalog = photos(db)
        assert DuplicateIndex(catalog).criteria(catalog[0]) == [
            ("result_original_path", catalog[0]["result_original_path"])
        ]
        override(db, [catalog[1]["id"]], "original", True)
        catalog = photos(db)
        assert not DuplicateIndex(catalog).criteria(catalog[0])


def test_names_and_dates_alone_do_not_match():
    def photo(id):
        return dict(
            id=id,
            skipped=False,
            completed=False,
            status_present=True,
            extracted_sha256=None,
            result_original_path=None,
            result_final_path=None,
            result_skip_original=False,
            result_skip_final=False,
            basename="same.jpg",
            result_timestamp="2026-01-01",
        )

    catalog = [photo(1), photo(2)]
    assert not DuplicateIndex(catalog).criteria(catalog[0])


def test_duplicate_details_include_filtered_out_archived_photo(workspace):
    from test_photo_trees import seed_ready_catalog

    source, _, _, database = workspace
    seed_ready_catalog(source, database)

    async def run():
        app = ArchiveApp(database)
        async with app.run_test(size=(160, 60)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one("#workflow", Tabs).active = "duplicates"
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert len(app.visible_photos) == 1
            details = str(app.query_one("#detail-duplicates", Static).content)
            assert "Already archived — no action needed" in details
            assert "#7 [archived]" in details
            assert str(source / "excluded/archived.jpg") in details
            assert "Matching content (SHA-256): 6" in details

    asyncio.run(run())
