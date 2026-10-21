import asyncio
from pathlib import Path

from textual.widgets import DataTable, Select, Tabs

from photo_archive_v3.analysis import override
from photo_archive_v3.db import connect, now
from photo_archive_v3.photo_trees import FolderTree, PhotoTrees, in_folder
from photo_archive_v3.tui import ArchiveApp


def seed_ready_catalog(source, database):
    entries = [
        ("A", "one.jpg", "CAM/2020/one.jpg", "ready"),
        ("A/children", "two.jpg", "ALT/2021/two.jpg", "ready"),
        ("B", "three.jpg", "CAM/2020/three.jpg", "ready"),
        ("B", "four.mp4", "MOV/CAM/four.mp4", "ready"),
        ("excluded", "pending.jpg", None, "pending"),
        ("excluded", "skipped.jpg", "CAM/skipped.jpg", "skipped"),
        ("excluded", "archived.jpg", "CAM/archived.jpg", "archived"),
        ("excluded", "error.jpg", "CAM/error.jpg", "errors"),
        ("excluded", "duplicate.jpg", "CAM/duplicate.jpg", "duplicates"),
    ]
    with connect(database) as db:
        with db:
            for index, (folder, name, destination, queue) in enumerate(entries):
                video = name.endswith(".mp4")
                values = {
                    "source_id": 1,
                    "source_path": str(source / folder / name),
                    "source_folder": str(source / folder),
                    "basename": name,
                    "extracted_kind": "video" if video else "photo",
                    "extracted_sha256": "6" if queue == "duplicates" else str(index),
                    "result_camera_id": "ALT" if folder.endswith("children") else "CAM",
                    "result_original_path": destination,
                    "result_final_path": f"2020_01/2020_01_01-00_00_{index:02}.jpg",
                    "result_timestamp": None
                    if queue == "pending"
                    else f"2020-01-01T00:00:{index:02}",
                    "status_analyzed_at": None if queue == "pending" else now(),
                    "result_skip_original": queue == "skipped",
                    "result_skip_final": video or queue == "skipped",
                    "status_original_archived_path": destination
                    if queue == "archived"
                    else None,
                    "status_final_archived_path": "existing.jpg"
                    if queue == "archived"
                    else None,
                    "status_error_message": "read failure"
                    if queue == "errors"
                    else None,
                }
                db.execute(
                    f"INSERT INTO photos({','.join(values)}) VALUES({','.join('?' for _ in values)})",
                    tuple(values.values()),
                )


def test_ready_folder_trees_cross_filter_and_clear_independently(workspace):
    source, _, _, database = workspace
    seed_ready_catalog(source, database)

    async def run():
        app = ArchiveApp(database)
        async with app.run_test(size=(150, 55)) as pilot:
            await app.workers.wait_for_complete()
            assert app.query_one(PhotoTrees).display
            app.query_one("#workflow", Tabs).active = "ready"
            await pilot.pause()
            await app.workers.wait_for_complete()
            folders = app.query_one(PhotoTrees)
            sources = app.query_one("#source-folders", FolderTree)
            targets = app.query_one("#original-folders", FolderTree)
            table = app.query_one("#photos", DataTable)
            assert folders.display
            assert table.row_count == 4
            assert str(sources.root.label) == "All photos (4)"
            assert str(sources.nodes[source / "A"].label) == "A (2)"
            assert str(sources.nodes[source / "A/children"].label) == "children (1)"
            assert source / "excluded" not in sources.nodes
            assert str(targets.nodes[Path("CAM")].label) == "CAM (2)"
            assert sources.region.bottom <= table.region.y
            assert targets.region.x >= sources.region.right

            app.selected = {app.visible_photos[0]["id"]}
            sources.focus()
            sources.move_cursor(sources.nodes[source / "A"])
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert not app.selected
            assert sources.has_focus
            assert sources.cursor_node is not None
            assert sources.cursor_node.data == str(source / "A")
            assert {p["basename"] for p in app.visible_photos} == {"one.jpg", "two.jpg"}
            assert str(targets.root.label) == "All photos (2)"
            assert str(targets.nodes[Path("CAM")].label) == "CAM (1)"
            assert Path("MOV") not in targets.nodes
            assert str(sources.root.label) == "All photos (4)"

            targets.select_node(targets.nodes[Path("CAM")])
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert [p["basename"] for p in app.visible_photos] == ["one.jpg"]
            assert str(sources.nodes[source / "A"].label) == "A (1)"
            assert str(sources.nodes[source / "B"].label) == "B (1)"
            assert source / "A/children" not in sources.nodes

            sources.select_node(sources.root)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert folders.filters["source_folder"] is None
            assert folders.filters["result_original_path"] == "CAM"
            assert {p["basename"] for p in app.visible_photos} == {
                "one.jpg",
                "three.jpg",
            }
            assert str(targets.nodes[Path("CAM")].label) == "CAM (2)"

            targets.select_node(targets.root)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert table.row_count == 4
            app.query_one("#kind", Select).value = "video"
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert table.row_count == 1
            assert str(sources.root.label) == "All photos (1)"
            assert source / "A" not in sources.nodes
            assert str(targets.root.label) == "All photos (1)"
            assert Path("CAM") not in targets.nodes

            app.query_one("#workflow", Tabs).active = "pending"
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert folders.display

    asyncio.run(run())


def test_ready_trees_refresh_after_skip_and_recover_from_empty_filters(workspace):
    source, _, _, database = workspace
    seed_ready_catalog(source, database)

    async def run():
        app = ArchiveApp(database)
        async with app.run_test(size=(150, 55)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one("#workflow", Tabs).active = "ready"
            await pilot.pause()
            await app.workers.wait_for_complete()
            sources = app.query_one("#source-folders", FolderTree)
            targets = app.query_one("#original-folders", FolderTree)
            targets.select_node(targets.nodes[Path("ALT")])
            await pilot.pause()
            await app.workers.wait_for_complete()
            photo = app.visible_photos[0]
            with connect(database) as db:
                override(db, [photo["id"]], "original", True)
            app.action_refresh()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert not app.visible_photos
            assert str(sources.root.label) == "All photos (0)"
            assert not sources.root.children
            assert Path("ALT") not in targets.nodes
            targets.select_node(targets.root)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert len(app.visible_photos) == 3
            assert not app.busy

    asyncio.run(run())


def test_folder_filter_matches_descendants_not_name_prefixes():
    items = [
        {"source_folder": "/photos/trip"},
        {"source_folder": "/photos/trip/day"},
        {"source_folder": "/photos/trip-extra"},
    ]
    assert in_folder(items, "source_folder", "/photos/trip") == items[:2]


def test_ready_date_tree_cross_filters_with_both_folder_trees(workspace):
    source, _, _, database = workspace
    seed_ready_catalog(source, database)
    with connect(database) as db:
        with db:
            db.execute(
                "UPDATE photos SET result_timestamp='2020-02-15T12:00:00' WHERE basename='two.jpg'"
            )
            db.execute(
                "UPDATE photos SET result_timestamp='2021-01-15T12:00:00' WHERE basename='three.jpg'"
            )
            db.execute(
                "UPDATE photos SET result_timestamp=NULL WHERE basename='four.mp4'"
            )

    async def run():
        app = ArchiveApp(database)
        async with app.run_test(size=(180, 55)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one("#workflow", Tabs).active = "ready"
            await pilot.pause()
            await app.workers.wait_for_complete()
            sources = app.query_one("#source-folders", FolderTree)
            targets = app.query_one("#original-folders", FolderTree)
            dates = app.query_one("#capture-dates", FolderTree)
            filters = app.query_one(PhotoTrees).filters

            async def choose(tree, path):
                tree.select_node(tree.root if path is None else tree.nodes[Path(path)])
                await pilot.pause()
                await app.workers.wait_for_complete()

            assert dates.region.x >= targets.region.right
            assert dates.region.right <= 180
            assert str(dates.root.label) == "All photos (4)"
            assert str(dates.nodes[Path("2020")].label) == "2020 (2)"
            assert str(dates.nodes[Path("2020/01")].label) == "01 (1)"
            assert str(dates.nodes[Path("2020/02")].label) == "02 (1)"
            assert str(dates.nodes[Path("2021")].label) == "2021 (1)"
            assert str(dates.nodes[Path("Unknown")].label) == "Unknown (1)"

            dates.focus()
            dates.move_cursor(dates.nodes[Path("2020")])
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert dates.has_focus
            assert {p["basename"] for p in app.visible_photos} == {"one.jpg", "two.jpg"}
            assert str(sources.root.label) == "All photos (2)"
            assert source / "B" not in sources.nodes
            assert Path("MOV") not in targets.nodes
            assert Path("2021") in dates.nodes  # Own filter does not hide alternatives.

            await choose(sources, str(source / "A"))
            assert Path("2021") not in dates.nodes
            assert Path("Unknown") not in dates.nodes
            await choose(targets, "CAM")
            assert [p["basename"] for p in app.visible_photos] == ["one.jpg"]
            assert str(dates.root.label) == "All photos (1)"
            assert Path("2020/02") not in dates.nodes

            await choose(dates, None)
            assert filters["result_timestamp"] is None
            assert filters["source_folder"] == str(source / "A")
            assert filters["result_original_path"] == "CAM"
            assert [p["basename"] for p in app.visible_photos] == ["one.jpg"]

            await choose(sources, None)
            assert {p["basename"] for p in app.visible_photos} == {
                "one.jpg",
                "three.jpg",
            }
            await choose(dates, "2021/01")
            assert [p["basename"] for p in app.visible_photos] == ["three.jpg"]
            assert source / "A" not in sources.nodes
            await choose(targets, None)
            assert filters["result_timestamp"] == "2021/01"
            assert [p["basename"] for p in app.visible_photos] == ["three.jpg"]
            await choose(dates, None)
            assert len(app.visible_photos) == 4

            await choose(dates, "Unknown")
            assert [p["basename"] for p in app.visible_photos] == ["four.mp4"]
            assert Path("CAM") not in targets.nodes
            assert source / "A" not in sources.nodes
            await choose(dates, None)
            app.query_one("#kind", Select).value = "photo"
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert str(dates.root.label) == "All photos (3)"
            assert Path("Unknown") not in dates.nodes

    asyncio.run(run())


def test_all_photo_tabs_have_trees_with_pending_unknowns_and_separate_filters(
    workspace,
):
    source, _, _, database = workspace
    seed_ready_catalog(source, database)
    with connect(database) as db:
        with db:
            db.execute(
                """INSERT INTO photos(source_id,source_path,source_folder,basename,
                extracted_kind,result_original_path,result_timestamp) VALUES(1,?,?,?,
                'photo','CAM/2022/known.jpg','2022-03-04T12:00:00')""",
                (str(source / "known/known.jpg"), str(source / "known"), "known.jpg"),
            )

    async def run():
        app = ArchiveApp(database)
        async with app.run_test(size=(180, 55)) as pilot:
            await app.workers.wait_for_complete()
            trees = app.query_one(PhotoTrees)
            sources = app.query_one("#source-folders", FolderTree)
            targets = app.query_one("#original-folders", FolderTree)
            dates = app.query_one("#capture-dates", FolderTree)

            async def tab(queue):
                app.query_one("#workflow", Tabs).active = queue
                await pilot.pause()
                await app.workers.wait_for_complete()

            async def choose(tree, path):
                tree.select_node(tree.root if path is None else tree.nodes[Path(path)])
                await pilot.pause()
                await app.workers.wait_for_complete()

            assert trees.display
            assert len(app.visible_photos) == 2
            assert str(targets.root.label) == "All photos (2)"
            assert str(targets.nodes[Path("Unknown")].label) == "Unknown (1)"
            assert str(targets.nodes[Path("CAM")].label) == "CAM (1)"
            assert str(dates.nodes[Path("Unknown")].label) == "Unknown (1)"
            assert str(dates.nodes[Path("2022/03")].label) == "03 (1)"

            await choose(dates, "Unknown")
            assert [p["basename"] for p in app.visible_photos] == ["pending.jpg"]
            assert Path("CAM") not in targets.nodes
            await choose(targets, "Unknown")
            await choose(dates, None)
            assert trees.filters["result_original_path"] == "Unknown"
            assert [p["basename"] for p in app.visible_photos] == ["pending.jpg"]

            await tab("ready")
            assert len(app.visible_photos) == 4
            assert all(value is None for value in trees.filters.values())
            await choose(sources, str(source / "A"))
            assert len(app.visible_photos) == 2
            await tab("pending")
            assert trees.filters["result_original_path"] == "Unknown"
            assert [p["basename"] for p in app.visible_photos] == ["pending.jpg"]
            await choose(targets, None)
            await choose(dates, "2022/03")
            assert [p["basename"] for p in app.visible_photos] == ["known.jpg"]
            assert Path("Unknown") not in targets.nodes
            await tab("ready")
            assert trees.filters["source_folder"] == str(source / "A")
            assert len(app.visible_photos) == 2

            for queue in ("skipped", "archived", "errors", "duplicates"):
                await tab(queue)
                assert trees.display
                assert len(app.visible_photos) == 1
                assert app.visible_photos[0]["queue"] == queue
                assert str(sources.root.label) == "All photos (1)"
                assert str(targets.root.label) == "All photos (1)"
                assert str(dates.root.label) == "All photos (1)"
            await tab("config")
            assert not trees.display

    asyncio.run(run())


def test_unknown_tree_filters_include_none_and_blank_but_not_known_values():
    for field, known in (
        ("result_original_path", "CAM/2020/photo.jpg"),
        ("result_timestamp", "2020-01-01T00:00:00"),
    ):
        items = [{field: None}, {field: ""}, {field: known}]
        assert in_folder(items, field, "Unknown") == items[:2]
        assert in_folder(items, field, None) == items
