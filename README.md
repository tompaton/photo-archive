# Photo archive v3

A local SQLite catalog and Textual UI for reviewing source photos/videos and
copying ready work to NAS originals and resized JPEG finals. Sources are never deleted.

## Run

With uv and Python 3.15 installed, from this directory:

```sh
uv sync --locked
uv run photo-archive-v3 initialize
uv run photo-archive-v3 tui
```

The default database is `state/archive.sqlite`. Put `--db PATH` before a command
to choose another local database. Git commits remain under your control.

Configuration shows sources, cameras, EXIF rules and filename rules together in
four panes, each with its own Add/Edit controls. NAS roots are in the Sources pane. Both NAS roots must already exist and be separate directories,
outside sources; the database must also be outside sources. Mount the NAS yourself.

For example (replace paths and camera identity before running):

```sh
uv run photo-archive-v3 source /local/downloads/photos
uv run photo-archive-v3 configure --original-root /mnt/nas/original --final-root /mnt/nas/final
uv run photo-archive-v3 camera CAM --name 'My camera' --photo-template '{camera}/{source_parent}/{filename}' --video-template 'videos/{camera}/{source_parent}/{filename}'
uv run photo-archive-v3 exif-rule --camera CAM --condition 'Image Make' eq 'Camera maker' --condition 'Image Model' eq 'Camera model'
uv run photo-archive-v3 filename-rule --pattern '(?P<date>\d{8}_\d{6})' --camera CAM --timestamp-group date --timestamp-format '%Y%m%d_%H%M%S'
uv run photo-archive-v3 scan
uv run photo-archive-v3 analyze
uv run photo-archive-v3 list --queue ready
uv run photo-archive-v3 sync
```

To retry photos with sync errors alongside ready photos, run
`uv run photo-archive-v3 sync --retry-errors`. Completed output checkpoints are
preserved; scan and analysis errors are left alone. Add `--dry-run` to preview
the retry without copying files or clearing errors. Stale analysis still requires
confirmation or `--allow-stale`.

## Workflow

Scan reads recursive folders and ZIP members, caching SHA-256 and EXIF. Unchanged
files reuse their observations. `scan --force` rereads them. Analyze commits and
updates progress every 100 photos, with a final update for the remaining photos.
Cancellation is checked between photos; completed batches remain saved.
Scan and Analyze are
separate; rule edits need Analyze, not another scan. Missing sources are retained.

Analyze selects camera from unambiguous EXIF rules, then filename rules. Dates
come from valid EXIF capture/digitization tags, then configured filename extraction.
If neither provides a date, the cached filesystem modification timestamp is used,
marked as `filesystem`, so `{year}` original templates can still be generated.
This is an approximate date; it also supplies the final filename unless final is
skipped. Filesystem timestamps use the system's local timezone. ZIP members use
the ZIP container's filesystem modification timestamp as this fallback.
When an original has no EXIF metadata, newly generated finals receive EXIF
DateTime, DateTimeOriginal and DateTimeDigitized from this saved filename or
filesystem date. Known offsets and fractional seconds are preserved; no timezone
is invented for dates without one. Originals remain unchanged. Use Regenerate
Final in the TUI to apply this to existing finals.
Existing scanned files need only Analyze, not another scan, to apply the fallback.
Camera-local times and known offsets are preserved. Ambiguous EXIF camera matches stay Pending. Filename rules
search basenames in priority/ID order, first match wins; unmatched media stay included.

The six photo tabs show Pending, Ready, Skipped, Archived, Errors and Duplicates.
New matches to archived source hashes go straight to Duplicates with no action
needed. Other duplicates and planned target clashes need skips or rule changes.
The reason column identifies matching SHA-256 content, original destination
clashes and final destination clashes. Selecting a duplicate lists every related
photo in the details panel, including photos hidden by filters or in other tabs,
with its ID, state, source path and exact matching values. Source filenames and
timestamps alone do not classify a photo as a duplicate.
Photos sharing a destination folder with different filenames do not clash.

Every photo tab shows three trees above its photo table: source folders, original
NAS destination folders (relative to the configured original root), and Year → Month.
Counts include the current tab's photos and videos in each branch and its
descendants; empty branches are omitted. Dates use the saved result timestamp.
Missing original destinations and undated files appear under Unknown in their
respective trees. Select a branch with a click or Enter to filter the table and
the other two trees. Each tree's counts reflect the other two selections.
All three tree filters combine with the Camera and Photo/Video dropdowns. Select **All
photos** at the top of a tree to clear only that filter, keeping the other two in
effect. Each photo tab remembers its own tree selections. Borders show active
filters. Changing a tree filter clears bulk photo selection. Filtering uses cached
catalog rows without scanning sources or NAS. Configuration keeps its four rule panes.

Photo details appear in a scrollable pane to the right of the photo table, with
labelled Source, Original, Timestamp, Final and Rules fields. View, rule and
skip/unskip actions sit below that pane. The selected-photo count and compact
All/None/Invert buttons and single-line Camera/Kind selects share a row below the
table. Their option lists open as dropdown menus.

All/None/Invert apply to filtered rows; Space toggles the focused row. Bulk skip
buttons affect selected rows, or the focused photo if nothing is selected.
Changing a filter/tab clears selection. Skip overrides take effect immediately;
Reset restores the saved rule default. Camera/date changes are made through rules,
not photo overrides. Skipping an original also skips its final and moves the photo
to Skipped, even if final was explicitly unskipped. Unskipping/resetting original
restores the saved final preference. Videos always skip final. Existing catalogs
apply this correction automatically when opened; no re-analysis is required.

View opens the source in the desktop application (`xdg-open` on Linux or `open` on
macOS); ZIP previews use temporary files. View EXIF shows highlighted JSON. Add
EXIF rule and Add filename rule prefill forms from the focused photo. EXIF form
conditions use `tag|eq|value` or `tag|regex|pattern`, separated by semicolons; conditions
within a rule are ANDed. Skip fields use True/False/Null radio buttons; Null means
inherit. Enabled uses True/False radio buttons.
Invalid rule drafts remain open with an error message. Edit rule rows through the
Configuration table and Edit button. Disable rules rather than deleting provenance.

Rule changes preserve saved results and show **Out of date**. Sync needs explicit
confirmation to use stale ready results; noninteractive CLI calls must pass
`sync --allow-stale`. Sync always processes the complete ready batch, independently
of visible filters or selection. An unresolved requested final keeps a photo Pending.

Final filenames are allocated during analysis in source filename order. The first
photo in a second uses `YYYY_MM/YYYY_MM_DD-HH_MM_SS.jpg`; subsequent photos use
`..._SS1.jpg`, `..._SS2.jpg`, and so on. Photos skipping final (including skipped
originals and videos) do not reserve names. Skip/unskip updates the allocation
immediately. Archived final names stay fixed when more photos are added. Existing
NAS files still undergo the normal sync checks below. Run Analyze once to update
previously generated plans that clash within a second.

To retry failed copies after fixing a sync problem, open Errors and click
**Retry sync**. It applies to the selected photos, or the focused photo when none
are selected. Use All to retry the filtered set. This clears sync errors and
preserves successful copy checkpoints; eligible photos return to Ready for the
next Sync. Scan errors require a rescan instead.

On Archived, **Regenerate final** replaces finals for the selected photos (or the
focused photo with no selection). It checks the archived original against the
scanned hash, then regenerates at the existing archived filename with current
resize and EXIF handling. Source files are not needed. Skipped finals and videos
are ignored. Conversion failures preserve the old final and appear in Errors;
use **Regenerate final** there to retry.

## NAS and final images

Use `photo-archive-v3 sync --dry-run` to check all Ready photos and preview
copy, final generation, existing-file acceptance, skips and errors with normal
progress output. It reads sources and NAS files for hash and format checks, but
does not copy files, generate images or update photo statuses (including errors).
It does not test image conversion or destination write permissions. Stale results
require the usual confirmation or `--allow-stale`.

Sync checks sources against their scanned hashes. Before any copy it inspects the
planned original and requested final:

- Matching original hash and existing final: accept both; do not regenerate final.
- Matching original and no final: generate the requested final.
- No original but an existing requested final: clash error; do not copy original.
- Different original hash: clash error; do not overwrite existing files.
- Neither exists: copy original and generate final, honoring skips (skipping original also skips final).

Finals are generated from the NAS original only after it is copied and verified
or an existing matching original is accepted. A skipped final is ignored.
Each successful output is checkpointed; later failure retains earlier success.
Errors record the stage/message. Error-repair/retry UI is intentionally deferred.

New finals retain the original EXIF, including capture dates, camera and GPS
metadata. The orientation tag is removed after rotating pixels when necessary,
so viewers do not rotate the final a second time. Existing finals are still reused
and are not rewritten to add metadata.

Pillow applies EXIF orientation and fits the image within **1920×1080**, preserving
aspect ratio without upscaling. Finals are JPEGs named
`YYYY_MM/YYYY_MM_DD-HH_MM_SS.jpg`. Initial defaults are quality 90, RGB output,
retained ICC profile for RGB inputs, and omitted EXIF (including the already-applied orientation).
There are no HEIF plugins; formats Pillow cannot process produce a blocking error
when a new final is required. Existing finals with a matching original need no decode.

Copying uses `Path.copy()` and ZIP streams, with hash checks on new copies. There
is no mount registration, staging journal, fsync protocol or multi-writer protection.
Run one app against a catalog/NAS at a time. Cancellation happens between files or
stages and preserves completed items; an active copy can finish first. Unexpected
filesystem/programming situations may stop the operation visibly.

## CLI and checks

`photo-archive-v3 --help` lists commands; each command has `--help`. Useful examples:

```sh
uv run photo-archive-v3 config cameras
uv run photo-archive-v3 list --queue pending --camera Unknown --kind photo
uv run photo-archive-v3 skip final yes 1 2 3
uv run photo-archive-v3 skip original reset 1
uv run photo-archive-v3 exif 1
uv run photo-archive-v3 view 1
uv run ruff check .
uv run ruff format --check .
uv run ty check --error-on-warning
uv run pytest
```

CLI progress goes to stderr; summaries go to stdout. TUI operations show loading
feedback and run blocking work in background threads; large photo tables populate
in batches. Tests use synthetic images and temporary directories, never the real NAS.
The fixed-height top operation bar keeps progress and its spinner to the right of
the operation buttons, alongside persistent photo and out-of-date analysis counts.
Status changes do not move the photo table. Long status messages have full-text tooltips.
See [PLAN.md](PLAN.md) for design decisions.

## Copying terminal text

In most Linux terminals, hold **Shift** while dragging to select text using the
terminal's own selection, then press **Ctrl+Shift+C** to copy (or use the terminal's
Copy menu). This also works for table cells. Ordinary dragging is handled by the
TUI instead; its **Ctrl+C** copy uses terminal clipboard support, which varies
between terminals. See the [Textual FAQ](https://textual.textualize.io/FAQ/#how-can-i-select-and-copy-text-in-a-textual-app).
