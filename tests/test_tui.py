import asyncio

from conftest import image
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Label,
    RadioButton,
    Select,
    Static,
    Tab,
    Tabs,
)

from photo_archive_v3.analysis import analyze, override
from photo_archive_v3.db import connect
from photo_archive_v3.forms import BooleanField, Confirm, Exif, Form
from photo_archive_v3.scan import scan
from photo_archive_v3.tui import ArchiveApp


def test_tui_startup_filters_selection_exif_and_forms(workspace, progress):
    source, _, _, database = workspace
    image(source / "20260102_030405.jpg")
    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)

    async def run():
        app = ArchiveApp(database)
        async with app.run_test(size=(100, 40)) as pilot:
            await app.workers.wait_for_complete()
            assert not app.busy
            assert {
                widget.id for widget in app.query_one("#filters").query(Select)
            } == {"camera", "kind"}
            assert str(app.query_one("#pending", Tab).label) == "New · Pending (0)"
            assert str(app.query_one("#ready", Tab).label) == "New · Ready (1)"
            assert str(app.query_one("#duplicates", Tab).label) == "Duplicates (0)"
            app.query_one("#workflow", Tabs).active = "ready"
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app.query_one("#photos", DataTable).row_count == 1
            app.action_toggle_photo()
            assert len(app.selected) == 1
            app.show_exif()
            await pilot.pause()
            assert isinstance(app.screen, Exif)
            await pilot.press("escape")
            await pilot.pause()
            app.photo_filename()
            await pilot.pause()
            assert isinstance(app.screen, Form)
            assert app.screen.query_one("#pattern", Input).value.startswith("^")
            await pilot.press("escape")
            await pilot.pause()
            app.query_one("#kind", Select).value = "video"
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert not app.selected
            assert app.query_one("#photos", DataTable).row_count == 0
            assert str(app.query_one("#ready", Tab).label) == "New · Ready (1)"
            with connect(database) as db:
                override(db, [app.items[0]["id"]], "original", True)
                override(db, [app.items[0]["id"]], "final", True)
            app.action_refresh()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert str(app.query_one("#ready", Tab).label) == "New · Ready (0)"
            assert str(app.query_one("#skipped", Tab).label) == "Skipped (1)"
            app.query_one("#workflow", Tabs).active = "config"
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app.query_one("#config-sources", DataTable).row_count == 1

    asyncio.run(run())


def test_narrow_tui_scan_failure_and_stale_confirmation(workspace, progress):
    source, _, _, database = workspace
    image(source / "20260102_030405.jpg")
    from photo_archive_v3.config import bump

    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)
        with db:
            bump(db)

    async def run():
        app = ArchiveApp(database)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            app.start_operation("sync")
            await pilot.pause()
            assert isinstance(app.screen, Confirm)
            await pilot.press("escape")
            await pilot.pause()

            def fail(db, p):
                raise OSError("test failure")

            app.operate(fail)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert not app.busy
            assert not app.query_one("#busy").display

    asyncio.run(run())


def test_rule_save_validation_keeps_draft_and_valid_save(workspace):
    _, _, _, database = workspace

    async def run():
        app = ArchiveApp(database)
        async with app.run_test(size=(100, 40)) as pilot:
            await app.workers.wait_for_complete()
            app.rule_form(
                "filename_rules",
                {
                    "pattern": "[",
                    "camera_id": "CAM",
                    "priority": "100",
                    "timestamp_group": "",
                    "timestamp_format": "",
                    "skip_original": "",
                    "skip_final": "",
                    "enabled": "true",
                },
            )
            await pilot.pause()
            form = app.screen
            assert isinstance(form, Form)
            form.save_draft()
            await app.workers.wait_for_complete()
            assert app.screen is form
            assert form.query_one("#pattern", Input).value == "["
            form.query_one("#pattern", Input).value = "^example.jpg$"
            form.save_draft()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert not isinstance(app.screen, Form)

    asyncio.run(run())


def test_configuration_four_panes_and_independent_edit_controls(workspace):
    from photo_archive_v3 import config

    _, _, _, database = workspace
    with connect(database) as db:
        config.exif_rule(db, "CAM", [("Image Model", "eq", "Example")])

    async def run(size):
        app = ArchiveApp(database)
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            app.query_one("#workflow", Tabs).active = "config"
            await pilot.pause()
            await app.workers.wait_for_complete()
            expected = {
                "sources": ("path", str(workspace[0])),
                "cameras": ("id", "CAM"),
                "exif_rules": ("conditions", "Image Model|eq|Example"),
                "filename_rules": ("timestamp_group", "date"),
            }
            for kind, (field, value) in expected.items():
                table = app.query_one(f"#config-{kind}", DataTable)
                assert table.row_count == 1
                assert table.region.width > 0 and table.region.height >= 2
                assert table.region.right <= size[0]
                assert table.region.bottom <= size[1] - 1
                await pilot.click(f"#config-edit-{kind}")
                await pilot.pause()
                assert isinstance(app.screen, Form)
                assert app.screen.query_one(f"#{field}", Input).value == value
                await pilot.press("escape")
                await pilot.pause()
                await pilot.click(f"#config-add-{kind}")
                await pilot.pause()
                assert isinstance(app.screen, Form)
                assert app.screen.query_one(f"#{field}", Input).value == ""
                await pilot.press("escape")
                await pilot.pause()

    for size in ((80, 24), (120, 40)):
        asyncio.run(run(size))


def test_rule_camera_dropdown_saves_ids_and_preserves_optional_default(workspace):
    from photo_archive_v3 import config
    from photo_archive_v3.db import rows
    from photo_archive_v3.tui import rule_fields

    _, _, _, database = workspace
    with connect(database) as db:
        config.camera(db, "ALT", "Another camera", "{camera}/{filename}", None)

    async def run():
        app = ArchiveApp(database)
        async with app.run_test(size=(100, 40)) as pilot:
            await app.workers.wait_for_complete()
            for table in ("exif_rules", "filename_rules"):
                fields = rule_fields(table, {})
                fields.update(
                    {"conditions": "Image Model|eq|Example"}
                    if table == "exif_rules"
                    else {"pattern": "^example.jpg$"}
                )
                app.rule_form(table, fields)
                await pilot.pause()
                form = app.screen
                assert isinstance(form, Form)
                dropdown = form.query_one("#camera_id", Select)
                assert dropdown.value == ""
                enabled = form.query_one("#enabled", BooleanField)
                assert enabled.value == "true"
                assert len(enabled.query(RadioButton)) == 2
                if table == "exif_rules":
                    form.save_draft()
                    await app.workers.wait_for_complete()
                    assert app.screen is form
                dropdown.value = "ALT"
                enabled.query(RadioButton)[1].value = True
                if table == "filename_rules":
                    assert form.query_one("#skip_original", BooleanField).value == ""
                    assert form.query_one("#skip_final", BooleanField).value == ""
                    form.query_one("#skip_original", BooleanField).query(RadioButton)[
                        0
                    ].value = True
                    form.query_one("#skip_final", BooleanField).query(RadioButton)[
                        1
                    ].value = True
                await pilot.pause()
                form.save_draft()
                await pilot.pause()
                await app.workers.wait_for_complete()
                with connect(database) as db:
                    saved = rows(db, f"SELECT * FROM {table} ORDER BY id DESC")[0]
                assert saved["camera_id"] == "ALT"
                assert saved["enabled"] == 0
                if table == "filename_rules":
                    assert saved["skip_original"] == 1
                    assert saved["skip_final"] == 0
                edit_fields = rule_fields(table, saved)
                if table == "exif_rules":
                    edit_fields["conditions"] = "Image Model|eq|Example"
                app.rule_form(table, edit_fields, saved["id"])
                await pilot.pause()
                form = app.screen
                assert isinstance(form, Form)
                assert form.query_one("#camera_id", Select).value == "ALT"
                if table == "filename_rules":
                    assert (
                        form.query_one("#skip_original", BooleanField).value == "true"
                    )
                    assert form.query_one("#skip_final", BooleanField).value == "false"
                    form.query_one("#skip_original", BooleanField).query(RadioButton)[
                        2
                    ].value = True
                    form.query_one("#skip_final", BooleanField).query(RadioButton)[
                        2
                    ].value = True
                    form.query_one("#camera_id", Select).value = ""
                    await pilot.pause()
                    form.save_draft()
                    await pilot.pause()
                    await app.workers.wait_for_complete()
                    with connect(database) as db:
                        saved = rows(
                            db,
                            "SELECT * FROM filename_rules WHERE id=?",
                            (saved["id"],),
                        )[0]
                        assert saved["skip_original"] is None
                        assert saved["skip_final"] is None
                        assert (
                            rows(
                                db,
                                "SELECT camera_id FROM filename_rules WHERE id=?",
                                (saved["id"],),
                            )[0]["camera_id"]
                            is None
                        )
                else:
                    await pilot.press("escape")
                    await pilot.pause()

    asyncio.run(run())


def test_external_viewer_preserves_photo_cursor_selection_and_scroll(
    workspace, progress
):
    from unittest.mock import patch

    source, _, _, database = workspace
    for index in range(20):
        image(source / f"{index:02}.jpg", size=(20, 20), color=(index * 12, 0, 0))
    with connect(database) as db:
        scan(db, progress)

    async def run():
        app = ArchiveApp(database)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            table = app.query_one("#photos", DataTable)
            table.move_cursor(row=15)
            table.focus()
            app.action_toggle_photo()
            await pilot.pause()
            photo = app.current()
            assert photo is not None
            selected = set(app.selected)
            scroll = table.scroll_y
            assert scroll > 0
            for failure in (None, OSError("viewer unavailable")):
                with patch.object(app.viewer, "open", side_effect=failure) as viewer:
                    with patch.object(
                        app,
                        "read_catalog",
                        side_effect=AssertionError(
                            "Viewing must not reload the catalog"
                        ),
                    ):
                        app.action_view()
                        await pilot.pause()
                        await app.workers.wait_for_complete()
                viewer.assert_called_once_with(photo)
                current = app.current()
                assert current is not None
                assert current["id"] == photo["id"]
                assert table.cursor_row == 15
                assert table.scroll_y == scroll
                assert app.selected == selected
                assert not app.busy
                assert not app.query_one("#busy").display

    asyncio.run(run())


def test_photo_details_are_labelled_beside_table_and_selected_count_is_below(
    workspace, progress
):
    source, _, _, database = workspace
    first = image(source / "20260102_030405.jpg", size=(20, 20))
    second = image(source / "20260102_030406.jpg", size=(20, 20), color="blue")
    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)

    async def run():
        app = ArchiveApp(database)
        async with app.run_test(size=(160, 60)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one("#workflow", Tabs).active = "ready"
            await pilot.pause()
            await app.workers.wait_for_complete()
            table = app.query_one("#photos", DataTable)
            details = app.query_one("#details")
            selected = app.query_one("#selection-count", Static)
            assert details.region.x >= table.region.right
            assert details.region.y == table.region.y
            assert details.region.height > 7
            assert selected.region.y >= table.region.bottom
            assert selected.region.right <= details.region.x
            actions = app.query_one("#actions")
            assert actions.region.x == details.region.x
            assert actions.region.y >= details.region.bottom
            for key in (
                "view",
                "exif",
                "map",
                "filename",
                "original-yes",
                "original-no",
                "original-reset",
                "final-yes",
                "final-no",
                "final-reset",
            ):
                assert actions in app.query_one(f"#{key}", Button).ancestors
            for key in ("all", "none", "invert"):
                button = app.query_one(f"#{key}", Button)
                assert button.region.y == selected.region.y
                assert button.region.height == 1
                assert button.region.width < 10
            for key in ("camera", "kind"):
                select = app.query_one(f"#{key}", Select)
                assert select.compact
                assert select.region.y == selected.region.y
                assert select.region.height == 1
                assert app.query_one("#selection-controls") in select.ancestors
            assert [str(label.content) for label in details.query(Label)] == [
                "Source",
                "Original",
                "Timestamp",
                "Final",
                "Rules",
            ]
            assert app.query_one("#detail-source", Static).content == str(first)
            assert "CAM/20260102_030405.jpg" in str(
                app.query_one("#detail-original", Static).content
            )
            assert "2026-01-02T03:04:05" in str(
                app.query_one("#detail-timestamp", Static).content
            )
            assert "From: filename" in str(
                app.query_one("#detail-timestamp", Static).content
            )
            assert "2026_01/2026_01_02-03_04_05.jpg" in str(
                app.query_one("#detail-final", Static).content
            )
            assert "Filename rule: 1" in str(
                app.query_one("#detail-rules", Static).content
            )
            assert selected.content == "Selected: 0"
            table.move_cursor(row=1)
            await pilot.pause()
            assert app.query_one("#detail-source", Static).content == str(second)
            app.action_toggle_photo()
            assert selected.content == "Selected: 1"
            await pilot.click("#all")
            await app.workers.wait_for_complete()
            assert selected.content == "Selected: 2"
            await pilot.click("#none")
            await app.workers.wait_for_complete()
            assert selected.content == "Selected: 0"
            await pilot.click("#invert")
            await app.workers.wait_for_complete()
            assert selected.content == "Selected: 2"
            kind = app.query_one("#kind", Select)
            kind.focus()
            await pilot.press("enter")
            assert kind.expanded
            overlay = kind.query_one("SelectOverlay")
            assert overlay.region.height > 1
            assert 0 <= overlay.region.y < overlay.region.bottom <= 60
            await pilot.press("end", "enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert kind.value == "video"
            assert not kind.expanded
            assert selected.content == "Selected: 0"
            assert all(
                app.query_one(f"#detail-{field}", Static).content == "—"
                for field in ("source", "original", "timestamp", "final", "rules")
            )
            app.query_one("#workflow", Tabs).active = "config"
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert not app.query_one("#photo-browser").display

    asyncio.run(run())


def test_operation_status_is_beside_buttons_and_does_not_shift_photo_layout(
    workspace, progress
):
    source, _, _, database = workspace
    image(source / "20260102_030405.jpg", size=(20, 20))
    with connect(database) as db:
        scan(db, progress)
        analyze(db, progress)
        with db:
            db.execute("UPDATE settings SET revision=revision+1")

    async def run(size):
        app = ArchiveApp(database)
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            app.query_one("#workflow", Tabs).active = "ready"
            await pilot.pause()
            await app.workers.wait_for_complete()
            bar = app.query_one("#operation-bar")
            buttons = app.query_one("#operation-buttons")
            area = app.query_one("#operation-status")
            status = app.query_one("#status", Static)
            summary = app.query_one("#catalog-summary", Static)
            assert area.region.x >= buttons.region.right
            assert area.region.y == buttons.region.y
            assert bar.region.height == 3
            assert summary.content == "1/1 photos · 1 analysis out of date"
            table = app.query_one("#photos", DataTable)
            workflow = app.query_one("#workflow", Tabs)
            original_table, original_tabs = table.region, workflow.region
            for active, message in (
                (True, "Analyzing…"),
                (True, "Very long path: " + "/photos" * 100),
                (True, "First line\nSecond line\nThird line\nFourth line"),
                (False, "Completed"),
                (False, ""),
            ):
                app.working(active)
                app.feedback(message)
                await pilot.pause()
                assert table.region == original_table
                assert workflow.region == original_tabs
                assert bar.region.height == 3
                assert status.region.height == 1
                assert summary.content == "1/1 photos · 1 analysis out of date"
                assert status.tooltip == message
                if active:
                    assert app.query_one("#busy").region.x >= buttons.region.right
                    assert app.query_one("#busy").region.y == status.region.y

    for size in ((80, 24), (160, 55)):
        asyncio.run(run(size))
