"""Workflow tabs using one shared photo table and direct configuration forms."""

import asyncio
import json
import re
import sqlite3
import threading
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.containers import (
    Grid,
    Horizontal,
    HorizontalScroll,
    Vertical,
    VerticalScroll,
)
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Label,
    LoadingIndicator,
    Select,
    Static,
    Tab,
    Tabs,
    Tree,
)

from . import config
from .analysis import analyze, override
from .cli import filter_photos
from .db import Row, connect, photos, rows, settings
from .duplicates import DuplicateIndex
from .forms import Confirm, Exif, Form
from .photo_trees import PhotoTrees
from .progress import Cancelled, Progress, background
from .scan import scan
from .sync import regenerate, retry, sync
from .viewing import Viewer

QUEUES = [
    ("New · Pending", "pending"),
    ("New · Ready", "ready"),
    ("Skipped", "skipped"),
    ("Archived", "archived"),
    ("Errors", "errors"),
    ("Duplicates", "duplicates"),
]
Operation = Callable[[sqlite3.Connection, Progress], str]


class ArchiveApp(App):
    TITLE = "Photo archive v3"
    CSS = """
    Screen { layout: vertical; }
    .toolbar { height: auto; min-height: 3; layout: horizontal; }
    .toolbar Button { min-width: 10; width: auto; }
    #filters { height: 1; width: auto; }
    #filters Select { height: 1; width: 18; margin-left: 1; }
    #filters #kind { width: 14; }
    #operation-bar { height: 3; }
    #operation-buttons { width: auto; max-width: 65%; height: 3; }
    #operation-status { width: 1fr; height: 3; padding-left: 1; }
    #catalog-summary { height: 2; overflow-y: hidden; }
    #progress-line { height: 1; }
    #busy { width: 4; height: 1; display: none; }
    #status { width: 1fr; height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    #photos { height: 1fr; min-height: 5; }
    #photo-browser { height: 1fr; min-height: 8; }
    #photo-list { width: 3fr; }
    #selection-controls { height: 1; }
    #selection-count { width: auto; height: 1; margin-right: 2; }
    #selection-controls Button { width: auto; min-width: 0; height: 1; padding: 0; margin-right: 1; }
    #photo-inspector { width: 2fr; }
    #details { height: 1fr; padding: 0 1; border: solid $panel; }
    #detail-duplicates { margin-top: 1; }
    #details .detail-label { height: 1; margin-top: 1; text-style: bold; }
    #details Static { height: auto; }
    #actions { height: 9; }
    #configuration { height: 1fr; display: none; }
    #config-panes { height: 1fr; grid-size: 2 2; grid-columns: 1fr 1fr; grid-rows: 1fr 1fr; }
    .config-pane { border: solid $panel; }
    .config-pane:focus-within { border: solid $accent; }
    .config-pane DataTable { height: 1fr; min-height: 1; }
    .config-pane Button { min-width: 6; }
    #config-note { height: 1; }
    """
    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+r", "refresh", "Refresh"),
        ("space", "toggle_photo", "Select row"),
        ("v", "view", "View"),
    ]

    def __init__(self, database: Path) -> None:
        super().__init__()
        self.database = database
        self.items: list[Row] = []
        self.duplicates = DuplicateIndex([])
        self.visible_photos: list[Row] = []
        self.selected: set[int] = set()
        self.config_items: dict[str, list[Row]] = {}
        self.busy = False
        self.cancel_event = threading.Event()
        self.viewer = Viewer()
        self._ui_ready = False
        self.filter_state: tuple = ()
        self.loading_filters = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="operation-bar"):
            with HorizontalScroll(id="operation-buttons", classes="toolbar"):
                for title in ("Scan", "Analyze", "Sync", "Cancel", "Refresh"):
                    yield Button(title, id=title.lower())
            with Vertical(id="operation-status"):
                yield Static("Loading catalog…", id="catalog-summary", markup=False)
                with Horizontal(id="progress-line"):
                    yield LoadingIndicator(id="busy")
                    yield Static("Opening catalog…", id="status", markup=False)
        yield Tabs(
            *(Tab(f"{label} (0)", id=queue) for label, queue in QUEUES),
            Tab("Configuration", id="config"),
            id="workflow",
        )
        yield PhotoTrees()
        with Horizontal(id="photo-browser"):
            with Vertical(id="photo-list"):
                yield DataTable(id="photos", cursor_type="row")
                with HorizontalScroll(id="selection-controls"):
                    yield Static("Selected: 0", id="selection-count", markup=False)
                    for label, key in (
                        ("All", "all"),
                        ("None", "none"),
                        ("Invert", "invert"),
                    ):
                        yield Button(label, id=key, compact=True)
                    with Horizontal(id="filters"):
                        for field, label in (
                            ("camera", "All cameras"),
                            ("kind", "All kinds"),
                        ):
                            yield Select(
                                [(label, "")],
                                value="",
                                allow_blank=False,
                                compact=True,
                                id=field,
                            )

            with Vertical(id="photo-inspector"):
                with VerticalScroll(id="details"):
                    for field in ("source", "original", "timestamp", "final", "rules"):
                        yield Label(field.title(), classes="detail-label")
                        yield Static("—", id=f"detail-{field}", markup=False)
                    yield Static("", id="detail-duplicates", markup=False)
                with VerticalScroll(id="actions"):
                    with HorizontalScroll(classes="toolbar"):
                        yield Button("Retry sync", id="retry")
                        yield Button("Regenerate final", id="regenerate")
                        for label, key in (
                            ("View", "view"),
                            ("View EXIF", "exif"),
                            ("Add EXIF rule", "map"),
                            ("Add filename rule", "filename"),
                        ):
                            yield Button(label, id=key)
                    for output in ("original", "final"):
                        with HorizontalScroll(classes="toolbar"):
                            for label, value in (
                                ("Skip", "yes"),
                                ("Unskip", "no"),
                                ("Reset", "reset"),
                            ):
                                yield Button(
                                    f"{label} {output}", id=f"{output}-{value}"
                                )
        with Vertical(id="configuration"):
            with Grid(id="config-panes"):
                for table in config.TABLES:
                    with Vertical(id=f"{table}-pane", classes="config-pane"):
                        with HorizontalScroll(classes="toolbar"):
                            yield Button("Add", id=f"config-add-{table}")
                            yield Button("Edit", id=f"config-edit-{table}")
                            if table == "sources":
                                yield Button("NAS roots", id="roots")
                        yield DataTable(id=f"config-{table}", cursor_type="row")
            yield Label(
                "Saved analysis stats · Filename counts are winning matches",
                id="config-note",
            )
        yield Footer()

    def on_mount(self) -> None:
        self._ui_ready = True
        self.query_one("#details").border_title = "Photo details"
        self.query_one("#photos", DataTable).add_columns(
            "✓",
            "ID",
            "Filename",
            "Camera",
            "Date",
            "Kind",
            "Analysis",
            "Reason / status",
        )
        for table in config.TABLES:
            self.query_one(f"#{table}-pane").border_title = (
                table.replace("_", " ").title().replace("Exif", "EXIF")
            )
            self.query_one(f"#config-{table}", DataTable).add_columns(
                "ID",
                "Name / pattern / path",
                "Photos",
                "Videos",
                "Min",
                "Max",
                "Scanned",
                "Analyzed",
                "Stale",
            )
        self.load()

    def feedback(self, text: str) -> None:
        status = self.query_one("#status", Static)
        status.update(text)
        status.tooltip = text

    def working(self, active: bool) -> None:
        self.busy = active
        self.query_one("#busy").display = active
        for button in self.query(Button):
            button.disabled = active and button.id != "cancel"
        for select in self.query(Select):
            select.disabled = active
        for tree in self.query(Tree):
            tree.disabled = active

    @work(group="load")
    async def load(self) -> None:
        if self.busy:
            return
        self.working(True)
        self.feedback("Loading photos and matching statistics…")
        try:
            self.items, self.config_items = await background(self.read_catalog)
            self.duplicates = DuplicateIndex(self.items)
            self.populate_tab_counts()
            self.selected.clear()
            self.populate_filters()
            await self.populate()
            self.populate_config()
            self.feedback("Catalog refreshed")
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.feedback(f"Load failed: {exc}")
        finally:
            self.working(False)

    def populate_tab_counts(self) -> None:
        counts = Counter(photo["queue"] for photo in self.items)
        for label, queue in QUEUES:
            self.query_one(f"#{queue}", Tab).label = f"{label} ({counts[queue]})"

    def read_catalog(self) -> tuple[list[Row], dict[str, list[Row]]]:
        with connect(self.database) as db:
            return photos(db), {
                table: config.statistics(db, table) for table in config.TABLES
            }

    def populate_filters(self) -> None:
        self.loading_filters = True
        columns = {
            "camera": "result_camera_id",
            "kind": "extracted_kind",
        }
        for field in ("camera", "kind"):
            values = {(p[columns[field]] or "Unknown") for p in self.items} | {
                "Unknown"
            }
            if field == "kind":
                values.update({"photo", "video"})
            widget = self.query_one(f"#{field}", Select)
            old = widget.value
            widget.set_options(
                [
                    ("All cameras" if field == "camera" else "All kinds", ""),
                    *((v, v) for v in sorted(values)),
                ]
            )
            widget.value = old if old in values else ""
        self.filter_state = self.current_filters()
        self.loading_filters = False

    def current_filters(self) -> tuple:
        return tuple(
            self.query_one(f"#{key}", Select).value for key in ("camera", "kind")
        )

    async def populate(self) -> None:
        queue = self.query_one("#workflow", Tabs).active
        filters = {
            key: self.query_one(f"#{key}", Select).value for key in ("camera", "kind")
        }
        self.visible_photos = filter_photos(
            [p for p in self.items if p["queue"] == queue], filters
        )
        trees = self.query_one(PhotoTrees)
        trees.display = queue != "config"
        if queue != "config":
            trees.set_queue(queue)
            self.visible_photos = trees.filter_items(self.visible_photos)
        table = self.query_one("#photos", DataTable)
        table.clear()
        for index, photo in enumerate(self.visible_photos):
            reason = (
                photo["status_error_message"] or photo["status_pending_reason"] or ""
            )
            if photo["queue"] == "duplicates":
                reason = self.duplicates.summary(photo)
            table.add_row(
                "✓" if photo["id"] in self.selected else "",
                str(photo["id"]),
                photo["basename"],
                photo["result_camera_id"] or "?",
                photo["result_timestamp"] or "?",
                photo["extracted_kind"],
                "Out of date"
                if photo["stale"]
                else ("Current" if photo["status_analyzed_at"] else "Not analyzed"),
                reason,
                key=str(photo["id"]),
            )
            if index % 100 == 0:
                await asyncio.sleep(0)
        self.update_details()
        self.update_catalog_summary()

    def update_catalog_summary(self) -> None:
        text = f"{len(self.visible_photos)}/{len(self.items)} photos · {sum(p['stale'] for p in self.items)} analysis out of date"
        summary = self.query_one("#catalog-summary", Static)
        summary.update(text)
        summary.tooltip = text

    def populate_config(self) -> None:
        for kind, items in self.config_items.items():
            self.populate_config_table(kind, items)

    def populate_config_table(self, kind: str, items: list[Row]) -> None:
        table = self.query_one(f"#config-{kind}", DataTable)
        table.clear()
        for item in items:
            title = (
                item.get("name")
                or item.get("path")
                or item.get("pattern")
                or item.get("camera_id")
            )
            table.add_row(
                str(item["id"]),
                str(title),
                *(
                    str(item.get(k) or "—")
                    for k in (
                        "photo_count",
                        "video_count",
                        "min_date",
                        "max_date",
                        "last_scanned",
                        "last_analyzed",
                        "stale_count",
                    )
                ),
                key=str(item["id"]),
            )

    def current(self) -> Row | None:
        table = self.query_one("#photos", DataTable)
        if self.visible_photos and table.cursor_row < len(self.visible_photos):
            return self.visible_photos[table.cursor_row]
        self.feedback("Choose a photo first")
        return None

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "photos":
            return
        self.update_details()

    def update_details(self) -> None:
        table = self.query_one("#photos", DataTable)
        photo = (
            self.visible_photos[table.cursor_row]
            if 0 <= table.cursor_row < len(self.visible_photos)
            else None
        )
        values = {}
        if photo:
            source = photo["source_path"]
            if photo["zip_member"]:
                source += f"\nZIP member: {photo['zip_member']}"
            values = {
                "source": source,
                "original": f"{photo['result_original_path'] or '—'} [Skip: {bool(photo['result_skip_original'])}]",
                "timestamp": f"{photo['result_timestamp'] or '—'} [From: {photo['result_timestamp_source'] or 'unknown'}]",
                "final": f"{photo['result_final_path'] or '—'} [Skip: {bool(photo['result_skip_final'])}]",
                "rules": f"EXIF rule: {photo['inferred_camera_exif_rule_id'] or '—'}\nFilename rule: {photo['inferred_filename_rule_id'] or '—'}",
            }
        for field in ("source", "original", "timestamp", "final", "rules"):
            self.query_one(f"#detail-{field}", Static).update(values.get(field, "—"))
        self.query_one("#retry", Button).display = (
            self.query_one("#workflow", Tabs).active == "errors"
        )
        self.query_one("#regenerate", Button).display = self.query_one(
            "#workflow", Tabs
        ).active in ("archived", "errors")
        duplicates = self.query_one("#detail-duplicates", Static)
        duplicates.display = bool(photo and photo["queue"] == "duplicates")
        duplicates.update(
            self.duplicates.details(photo) if duplicates.display and photo else ""
        )
        self.update_selected_count()

    def update_selected_count(self) -> None:
        self.query_one("#selection-count", Static).update(
            f"Selected: {len(self.selected)}"
        )

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        if not self._ui_ready:
            return
        is_config = event.tab.id == "config"
        self.query_one("#photo-browser").display = not is_config
        self.query_one("#configuration").display = is_config
        self.query_one(PhotoTrees).display = not is_config
        self.selected.clear()
        self.load()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id not in ("camera", "kind"):
            return
        if (
            self._ui_ready
            and not self.loading_filters
            and not self.busy
            and self.current_filters() != self.filter_state
        ):
            self.selected.clear()
            self.load()

    @work(group="folder-filter")
    async def on_photo_trees_changed(self, event: PhotoTrees.Changed) -> None:
        if self.busy:
            return
        focused = self.focused
        self.working(True)
        self.selected.clear()
        self.feedback("Filtering photos…")
        try:
            await self.populate()
            self.feedback(
                f"{len(self.visible_photos)} photos/videos match the tree filters"
            )
        finally:
            self.working(False)
            if focused is not None:
                focused.focus()

    @work(group="operation")
    async def operate(self, function: Operation, *, refresh: bool = True) -> None:
        if self.busy:
            return
        self.working(True)
        self.cancel_event.clear()
        self.feedback("Working…")

        def run() -> str:
            with connect(self.database) as db:
                return function(
                    db,
                    Progress(
                        lambda text: self.call_from_thread(self.feedback, text),
                        self.cancel_event.is_set,
                    ),
                )

        try:
            message = await background(run)
        except (OSError, ValueError, sqlite3.Error, Cancelled) as exc:
            message = f"Operation stopped: {exc}"
        finally:
            self.working(False)
        if refresh:
            await self.load().wait()
        self.feedback(message)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        key = event.button.id or ""
        event.stop()
        if key in ("scan", "analyze", "sync"):
            self.start_operation(key)
        elif key == "cancel":
            self.cancel_event.set()
        elif key == "refresh":
            self.action_refresh()
        elif key in ("all", "none", "invert"):
            self.selection(key)
        elif "-" in key and key.split("-")[0] in ("original", "final"):
            self.skip_action(key)
        else:
            self.edit_action(key)

    def start_operation(self, name: str) -> None:
        functions: dict[str, Operation] = {
            "scan": scan,
            "analyze": analyze,
            "sync": sync,
        }
        if name == "sync":
            stale = sum(p["stale"] for p in self.items if p["queue"] == "ready")
            if stale:

                def confirmed(yes: bool | None) -> None:
                    if yes:
                        self.operate(lambda db, p: sync(db, p, True))

                self.push_screen(
                    Confirm(
                        f"{stale} ready photos have out-of-date analysis. Proceed using saved results?"
                    ),
                    confirmed,
                )
                return
        self.operate(functions[name])

    @work(group="selection", exclusive=True)
    async def selection(self, mode: str) -> None:
        ids = {p["id"] for p in self.visible_photos}
        self.selected = (
            ids if mode == "all" else (set() if mode == "none" else ids - self.selected)
        )
        self.working(True)
        try:
            await self.populate()
            self.feedback(f"{len(self.selected)} photos selected")
        finally:
            self.working(False)

    def action_toggle_photo(self) -> None:
        photo = self.current()
        if photo:
            self.selected.symmetric_difference_update({photo["id"]})
            self.query_one("#photos", DataTable).update_cell(
                str(photo["id"]),
                self.query_one("#photos", DataTable)
                .columns.keys()
                .__iter__()
                .__next__(),
                "✓" if photo["id"] in self.selected else "",
            )
            self.update_selected_count()
            self.feedback(f"{len(self.selected)} photos selected")

    def skip_action(self, key: str) -> None:
        output, mode = key.split("-")
        photo = self.current()
        ids = sorted(self.selected) or ([photo["id"]] if photo else [])
        if ids:
            self.feedback(f"Updating {len(ids)} photos…")
            self.operate(
                lambda db, p: override(
                    db, ids, output, {"yes": True, "no": False, "reset": None}[mode]
                )
            )

    def retry_action(self) -> None:
        photo = self.current()
        ids = sorted(self.selected) or ([photo["id"]] if photo else [])
        if ids:
            self.operate(lambda db, p: retry(db, ids))

    def regenerate_action(self) -> None:
        photo = self.current()
        ids = sorted(self.selected) or ([photo["id"]] if photo else [])
        if ids:
            self.operate(lambda db, p: regenerate(db, ids, p))

    def edit_action(self, key: str) -> None:
        if key.startswith("config-"):
            _, action, table = key.split("-", 2)
            self.config_form(table, action == "edit")
            return
        actions = {
            "view": self.action_view,
            "exif": self.show_exif,
            "map": self.photo_exif,
            "filename": self.photo_filename,
            "roots": self.roots_form,
            "retry": self.retry_action,
            "regenerate": self.regenerate_action,
        }
        action = actions.get(key)
        if action:
            action()

    def action_refresh(self) -> None:
        if not self.busy:
            self.load()

    def action_view(self) -> None:
        photo = self.current()
        if photo:
            self.operate(lambda db, p: self.open_viewer(photo, p), refresh=False)

    def open_viewer(self, photo: Row, progress: Progress) -> str:
        progress.tick(f"Preparing viewer: {photo['basename']}")
        self.viewer.open(photo)
        return "Opened in desktop viewer"

    def show_exif(self) -> None:
        photo = self.current()
        if photo:
            self.push_screen(
                Exif(json.dumps(json.loads(photo["extracted_exif_json"]), indent=2))
            )

    def show_form(
        self,
        title: str,
        fields: dict[str, str],
        save: Callable[[sqlite3.Connection, dict[str, str]], None],
        choices: dict[str, list[tuple[str, str]]] | None = None,
    ) -> None:
        def save_values(values: dict[str, str]) -> None:
            with connect(self.database) as db:
                save(db, values)

        def done(values: dict[str, str] | None) -> None:
            if values is not None:
                self.load()

        self.push_screen(Form(title, fields, save_values, choices), done)

    def roots_form(self) -> None:
        with connect(self.database) as db:
            values = settings(db)
        self.show_form(
            "NAS destination roots",
            {k: values[k] or "" for k in ("original_root", "final_root")},
            lambda db, v: config.roots(
                db, v["original_root"], v["final_root"], self.database
            ),
        )

    def photo_exif(self) -> None:
        photo = self.current()
        if not photo:
            return
        conditions = []
        for tag, column in [
            ("Image Make", "extracted_make"),
            ("Image Model", "extracted_model"),
        ]:
            if photo[column]:
                conditions.append(f"{tag}|eq|{photo[column]}")
        self.rule_form(
            "exif_rules",
            {"camera_id": "", "conditions": ";".join(conditions), "enabled": "true"},
        )

    def photo_filename(self) -> None:
        photo = self.current()
        if photo:
            self.rule_form(
                "filename_rules",
                {
                    "pattern": "^" + re.escape(photo["basename"]) + "$",
                    "camera_id": "",
                    "priority": "100",
                    "timestamp_group": "",
                    "timestamp_format": "",
                    "skip_original": "",
                    "skip_final": "",
                    "enabled": "true",
                },
            )

    def config_form(self, table: str, edit: bool) -> None:
        item: Row = {}
        if edit:
            index = self.query_one(f"#config-{table}", DataTable).cursor_row
            if not self.config_items[table]:
                return
            item = self.config_items[table][index]
        if table == "sources":
            fields = {
                "path": item.get("path", ""),
                "enabled": str(bool(item.get("enabled", 1))).lower(),
            }
            self.show_form(
                "Source directory",
                fields,
                lambda db, v: config.source(
                    db, v["path"], self.database, boolean(v["enabled"]) is True
                ),
            )
        elif table == "cameras":
            fields = {
                k: item.get(k) or ""
                for k in ("id", "name", "photo_template", "video_template")
            }
            if not edit:
                fields["photo_template"] = "{camera}/{filename}"
                fields["video_template"] = "MOV/{camera}/{filename}"
            self.show_form(
                "Camera (keep ID stable when editing)",
                fields,
                lambda db, v: config.camera(
                    db, v["id"], v["name"], v["photo_template"], v["video_template"]
                ),
            )
        else:
            fields = rule_fields(table, item)
            if table == "exif_rules" and item:
                with connect(self.database) as db:
                    conditions = rows(
                        db,
                        "SELECT * FROM exif_conditions WHERE rule_id=?",
                        (item["id"],),
                    )
                fields["conditions"] = ";".join(
                    f"{c['tag']}|{c['operator']}|{c['value']}" for c in conditions
                )
            self.rule_form(table, fields, item.get("id"))

    def rule_form(
        self, table: str, fields: dict[str, str], rule_id: int | None = None
    ) -> None:
        def save(db: sqlite3.Connection, values: dict[str, str]) -> None:
            if table == "exif_rules":
                if not values["camera_id"]:
                    raise ValueError("Choose a camera for this EXIF rule")
                parts = [
                    tuple(c.strip().split("|", 2))
                    for c in values["conditions"].split(";")
                    if c.strip()
                ]
                if any(len(c) != 3 for c in parts):
                    raise ValueError(
                        "Conditions use tag|eq/regex|value separated by semicolons"
                    )
                config.exif_rule(
                    db,
                    values["camera_id"],
                    [(c[0], c[1], c[2]) for c in parts],
                    rule_id,
                    boolean(values["enabled"]) is True,
                )
            else:
                data: Row = {k: v or None for k, v in values.items()}
                data["priority"] = int(values["priority"])
                for key in ("skip_original", "skip_final", "enabled"):
                    data[key] = boolean(values[key])
                config.filename_rule(db, data, rule_id)

        self.show_form(
            "EXIF: tag|eq/regex|value; additional conditions separated by ;"
            if table == "exif_rules"
            else "Filename rule (Null skips inherit; True/False override)",
            fields,
            save,
            choices={
                "camera_id": [
                    (
                        "Choose a camera"
                        if table == "exif_rules"
                        else "No camera default",
                        "",
                    ),
                    *(
                        (f"{camera['id']} — {camera['name']}", camera["id"])
                        for camera in self.config_items.get("cameras", [])
                    ),
                ]
            },
        )


def boolean(value: str) -> bool | None:
    if value.lower() not in ("true", "false", ""):
        raise ValueError("Boolean fields accept true, false, or blank for inheritance")
    return {"true": True, "false": False, "": None}[value.lower()]


def rule_fields(table: str, item: Row) -> dict[str, str]:
    if table == "exif_rules":
        return {
            "camera_id": item.get("camera_id", ""),
            "conditions": "",
            "enabled": str(bool(item.get("enabled", 1))).lower(),
        }
    fields = (
        "pattern",
        "camera_id",
        "priority",
        "timestamp_group",
        "timestamp_format",
        "skip_original",
        "skip_final",
        "enabled",
    )
    result = {key: "" if item.get(key) is None else str(item[key]) for key in fields}
    result["priority"] = str(item.get("priority", 100))
    for key in ("skip_original", "skip_final", "enabled"):
        value = item.get(key, 1 if key == "enabled" else None)
        result[key] = "" if value is None else str(bool(value)).lower()
    return result
