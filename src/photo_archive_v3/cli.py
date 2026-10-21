"""Small argparse CLI; human progress goes to stderr."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from rich.console import Console

from . import config
from .analysis import analyze, override
from .db import connect, photos, rows
from .progress import Cancelled, Progress
from .scan import scan
from .sync import StaleConfirmation, sync


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Photo archive v3")
    root.add_argument("--db", type=Path, default=Path("state/archive.sqlite"))
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("initialize", "analyze", "tui"):
        commands.add_parser(name)
    scan_parser = commands.add_parser("scan")
    scan_parser.add_argument("--force", action="store_true")
    sync_parser = commands.add_parser("sync")
    sync_parser.add_argument("--allow-stale", action="store_true")
    sync_parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="Retry photos with sync errors as well as ready photos",
    )
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check ready photos and report planned actions without copying or updating statuses",
    )
    listing = commands.add_parser("list")
    listing.add_argument(
        "--queue",
        choices=("pending", "ready", "skipped", "archived", "errors", "duplicates"),
    )
    for field in ("camera", "folder", "kind", "year", "month"):
        listing.add_argument(f"--{field}")
    for name in ("view", "exif"):
        photo = commands.add_parser(name)
        photo.add_argument("id", type=int)
    roots = commands.add_parser("configure")
    roots.add_argument("--original-root", required=True)
    roots.add_argument("--final-root", required=True)
    source = commands.add_parser("source")
    source.add_argument("path")
    source.add_argument("--disable", action="store_true")
    camera = commands.add_parser("camera")
    camera.add_argument("id")
    camera.add_argument("--name", required=True)
    camera.add_argument("--photo-template", default="{camera}/{filename}")
    camera.add_argument("--video-template", default="MOV/{camera}/{filename}")
    exif = commands.add_parser("exif-rule")
    exif.add_argument("--id", type=int)
    exif.add_argument("--camera", required=True)
    exif.add_argument(
        "--condition",
        nargs=3,
        action="append",
        metavar=("TAG", "OPERATOR", "VALUE"),
        required=True,
    )
    exif.add_argument("--disable", action="store_true")
    filename = commands.add_parser("filename-rule")
    filename.add_argument("--id", type=int)
    filename.add_argument("--pattern", required=True)
    filename.add_argument("--priority", type=int, default=100)
    filename.add_argument("--camera")
    filename.add_argument("--timestamp-group")
    filename.add_argument("--timestamp-format")
    for output in ("original", "final"):
        filename.add_argument(
            f"--skip-{output}", action=argparse.BooleanOptionalAction, default=None
        )
    filename.add_argument("--disable", action="store_true")
    stats = commands.add_parser("config")
    stats.add_argument("table", choices=config.TABLES)
    skip = commands.add_parser("skip")
    skip.add_argument("output", choices=("original", "final"))
    skip.add_argument("value", choices=("yes", "no", "reset"))
    skip.add_argument("ids", nargs="+", type=int)
    return root


def filter_photos(items: list[dict], filters: dict) -> list[dict]:
    fields = {
        "camera": "result_camera_id",
        "folder": "source_folder",
        "kind": "extracted_kind",
    }
    for field, column in fields.items():
        value = filters.get(field)
        if value:
            items = [p for p in items if (p[column] or "Unknown") == value]
    for field, start, end in (("year", 0, 4), ("month", 5, 7)):
        value = filters.get(field)
        if value:
            items = [
                p
                for p in items
                if (p["result_timestamp"] or "")[start:end] == value
                or (value == "Unknown" and not p["result_timestamp"])
            ]
    return items


def configure_command(db: sqlite3.Connection, args: argparse.Namespace) -> str:
    match args.command:
        case "configure":
            config.roots(db, args.original_root, args.final_root, args.db)
        case "source":
            config.source(db, args.path, args.db, not args.disable)
        case "camera":
            config.camera(
                db, args.id, args.name, args.photo_template, args.video_template
            )
        case "exif-rule":
            config.exif_rule(db, args.camera, args.condition, args.id, not args.disable)
        case "filename-rule":
            config.filename_rule(
                db,
                {
                    "pattern": args.pattern,
                    "priority": args.priority,
                    "camera_id": args.camera,
                    "timestamp_group": args.timestamp_group,
                    "timestamp_format": args.timestamp_format,
                    "skip_original": args.skip_original,
                    "skip_final": args.skip_final,
                    "enabled": not args.disable,
                },
                args.id,
            )
        case _:
            raise ValueError("Unknown configuration command")
    return "Configuration saved; Analyze applies rule changes"


def show_photo(db: sqlite3.Connection, args: argparse.Namespace) -> str:
    items = rows(db, "SELECT * FROM photos WHERE id=?", (args.id,))
    if not items:
        raise ValueError(f"Unknown photo ID: {args.id}")
    if args.command == "exif":
        return json.dumps(json.loads(items[0]["extracted_exif_json"]), indent=2)
    from .viewing import Viewer

    viewer = Viewer()
    viewer.open(items[0])
    if items[0]["zip_index"] >= 0:
        input("Press Enter after closing the viewer…")
    return "Opened in desktop viewer"


def execute(args: argparse.Namespace, progress: Progress) -> str:
    with connect(args.db) as db:
        match args.command:
            case "initialize":
                return f"Initialized {args.db}"
            case "scan":
                return scan(db, progress, args.force)
            case "analyze":
                return analyze(db, progress)
            case "sync":
                return sync(
                    db,
                    progress,
                    args.allow_stale,
                    dry_run=args.dry_run,
                    retry_errors=args.retry_errors,
                )
            case "skip":
                return override(
                    db,
                    args.ids,
                    args.output,
                    {"yes": True, "no": False, "reset": None}[args.value],
                )
            case "list":
                items = filter_photos(photos(db, args.queue), vars(args))
                return (
                    "\n".join(
                        f"{p['id']}\t{p['queue']}\t{p['basename']}\t{p['result_camera_id'] or '?'}\t{p['result_timestamp'] or '?'}\t{'OUT OF DATE' if p['stale'] else ''}\t{p['status_error_message'] or p['status_pending_reason'] or (p['duplicate_note'] if p['queue'] == 'duplicates' else '')}"
                        for p in items
                    )
                    or "No matching photos"
                )
            case "view" | "exif":
                return show_photo(db, args)
            case "config":
                return (
                    "\n".join(str(row) for row in config.statistics(db, args.table))
                    or "No configuration rows"
                )
            case _:
                return configure_command(db, args)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "tui":
        from .tui import ArchiveApp

        ArchiveApp(args.db).run()
        return 0
    console = Console(stderr=True)
    try:
        with console.status(f"{args.command}…") as status:

            def report(message: str) -> None:
                status.update(message)
                console.print(message, markup=False)

            progress = Progress(report)
            try:
                result = execute(args, progress)
            except StaleConfirmation as exc:
                if not sys.stdin.isatty():
                    raise
                status.stop()
                print(f"{exc} Proceed? [y/N] ", file=sys.stderr, end="", flush=True)
                if input().strip().lower() != "y":
                    return 1
                args.allow_stale = True
                result = execute(args, progress)
        print(result)
        return 0
    except (OSError, ValueError, sqlite3.Error, Cancelled) as exc:
        console.print(f"Error: {exc}", markup=False)
        return 1
    except KeyboardInterrupt:
        console.print("Cancelled; completed work kept")
        return 130
