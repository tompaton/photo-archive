# Photo archive v3 — initial plan

Implemented initial version, 21 October 2026. The CLI and TUI cover configuration,
scan/analyze, six review queues, filtering/selection, skip overrides, viewing and
ready-only original/final sync. See README.md for setup, controls and limitations.
Validation uses temporary synthetic sources/destinations; real collection/NAS trial
remains a user-run step.

Initial conversion defaults: JPEG quality 90, RGB, ICC retained for RGB inputs,
EXIF omitted after applying orientation. No HEIF plugins. Remaining proposals below
are guidance for further development, not hidden implemented requirements.

## Aim

A small local application: scan sources, analyze cached facts, review photos, sync
ready photos to NAS originals and finals. Pending photos can stay pending forever.
Keep useful behavior from previous versions, but rebuild around a flat photos table instead of carrying
forward a complex evidence model, generic JSON interfaces, and defensive copy machinery.

Low complexity is a design requirement, not a refactoring phase. Prefer direct SQL,
ordinary functions, `Path.iterdir()`/`glob()`, `Path.copy()` for loose files, and
standard-library ZIP streams. Accept that unusual filesystem arrangements can fail.
Do not build a general-purpose archiving framework.

## Stack and complexity budget

- Python 3.15, SQLite (`sqlite3`), Textual, ExifRead, uv, Ruff, ty, pytest.
- Start without Pydantic. Retain tree-sitter and tree-sitter-json for EXIF JSON
  syntax highlighting. Use ordinary form fields, small dataclasses
  where useful, and explicit validation of regexes, dates and path templates.
- JSON is for captured EXIF, not analysis records, overrides or command envelopes.
  EXIF conditions can be child rows with tag/operator/value columns and AND semantics.
- CLI and TUI call the same scan/analyze/sync and configuration functions. Use a
  small progress callback; no command bus, plugin system or repository abstraction.
- Start with modules for database/schema, rules/analysis, scan/media, sync, CLI,
  TUI, and viewing. Split only when a responsibility becomes difficult to follow;
  avoid a store/types/service module trio for every feature.
- Adopt a Ruff complexity ceiling of 10, annotations, formatting and ty checks.
  Also review total concepts, tables, indirection and module count: a low McCabe
  score alone does not make the implementation simple.

Use Pillow for final image generation; ExifRead remains the metadata reader. Test
representative formats and Python 3.15 installation. Do not implement a decoder
abstraction in anticipation of multiple backends.

## Configuration

Keep configuration in SQLite, editable through CLI arguments and TUI forms:

| Table | Contents |
| --- | --- |
| settings | Original/final NAS roots, final naming/conversion settings, analysis revision |
| sources | Source directory, enabled flag, last scan start/completion |
| cameras | Stable ID, name, original photo and video path templates |
| exif_rules | Camera ID, enabled flag |
| exif_rule_conditions | Rule ID, EXIF tag, equality/regex operator, value |
| filename_rules | Ordered regex, optional camera, timestamp group/format, nullable skip-original and skip-final defaults |
| photos | Source identity, extracted facts, inferred values, overrides, results and workflow status |

Start with independent source directories and no nested inheritance, bootstrap
scope, scan/ignore routing engine or source-specific defaults. Deduplicate paths
when configured roots overlap. NAS roots and the database must be outside sources.

Configuration tables show matching photo/video counts, min/max known result dates,
last scanned and last analyzed. Compute counts and date ranges from photo rows;
do not persist counters. Label filename-rule counts as *winning matches* initially,
so several regex matches do not imply several rules supplied values. For cameras
and EXIF rules, count their effective assignments. Unknown dates do not affect
ranges. Show last scanned/analyzed as the latest timestamp among those matched
rows, with never/empty represented explicitly; source scan completion is separate.
These statistics describe the saved analysis and may be out of date.

## Flat photos table

One row per loose source file or ZIP entry. Separate rows survive identical hashes.
Use direct columns; do not also persist a second analysis JSON representation.
The following is a candidate column inventory, to be trimmed during the first
vertical slice if fields are not used by a screen or operation.

| Group | Proposed columns and purpose |
| --- | --- |
| Identity | `id`, `source_id`, `source_path`, `zip_member`, `zip_index`, `source_folder`, `basename` |
| Extracted | `extracted_kind`, `extracted_size`, `extracted_mtime_ns`, `extracted_sha256`, `extracted_exif_json`, `extracted_make`, `extracted_model`, `extracted_timestamp` |
| Inferred | `inferred_camera_id`, `inferred_camera_exif_rule_id`, `inferred_camera_filename_rule_id`, `inferred_timestamp`, `inferred_timestamp_filename_rule_id`, `inferred_skip_original`, `inferred_skip_final`, `inferred_filename_rule_id` |
| Overridden | Nullable `overridden_skip_original`, `overridden_skip_final` |
| Result | `result_camera_id`, `result_timestamp`, `result_timestamp_source`, `result_skip_original`, `result_skip_final`, `result_original_path`, `result_final_path` |
| Status | `status_scanned_at`, `status_analyzed_at`, `status_analysis_revision`, `status_present`, `status_pending_reason`, `status_error_stage`, `status_error_message`, `status_original_copied_path`, `status_final_created_path` |

Dates are ISO text; booleans are SQLite integers. A NULL override means use rules;
false is an explicit unskip. Clear/reset is distinct from unskip. Rule IDs explain
which rule supplied each inferred value; configuration IDs should remain stable.
Disable referenced rules instead of making foreign keys erase their provenance.
For a ZIP entry, size is its uncompressed size; the cached mtime fingerprint is
the container's. `zip_index` distinguishes duplicate member names.

The completed-path columns also record existing files accepted by matching hashes;
prefer naming them `status_original_archived_path` and `status_final_archived_path`.
Store these paths rather than a redundant archived flag: they record what
actually completed even if rules later propose another path. Derive tabs, duplicate
groups and staleness using SQL. Add indexes for source identity, hash and target
paths, plus filters when needed. No history, import, candidate, archive-asset,
verification-evidence, staging-journal or per-operation report tables initially.

## Scan

1. Walk enabled source roots recursively and discover supported photos/videos.
   ZIP files act as containers; members have their own rows and virtual folders.
   Do not expand nested ZIPs initially.
2. Cache SHA-256, raw EXIF and the few normalized fields used by rules. Disable
   ExifRead thumbnail extraction. Videos may have no readable EXIF; filename rules
   can still identify their camera/date.
3. Reuse unchanged observations using size/mtime; offer a force scan. Update changed
   observations and require analysis for changed bytes. Keep user overrides.
4. Commit completed items incrementally. Mark unseen rows absent only after that
   source root finishes successfully. Missing sources remain visible, not deleted.

Use a straightforward file loop and a ZIP loop.
Skip directory symlinks. Read members directly; never extract member paths into
the filesystem. Temporary viewer files use photo IDs and safe suffixes.

Catch expected per-file read/ZIP failures and record them; an unavailable root
gets visible operation feedback. Unexpected programming errors may fail the
operation. No exhaustive race detection, archive-bomb policy engine, automatic
retry system, or content/location history in this version.

## Analyze and rule changes

Analyze uses cached facts only; editing rules never triggers a rescan or silently
runs analysis. Missing facts leave a photo pending.

- Camera: unambiguous EXIF rule → filename rule. Multiple EXIF matches
  for the same camera are fine; different cameras leave assignment pending.
- Timestamp: valid EXIF capture/digitization timestamp → filename extraction →
  cached filesystem modification timestamp (including ZIP container mtime for
  members). Mark the fallback source as `filesystem`; it is approximate but allows
  year-based original templates. Preserve camera-local EXIF times and known offsets.
- Filename rules search basenames in priority/ID order; first match wins.
  Unmatched supported media remain included. A rule can provide camera, timestamp
  and either skip default. ZIP traversal does not depend on the ZIP's basename
  matching a media filename rule.
- Override skip flags take precedence over rule defaults; otherwise default false.
  Skipping original forces final to be skipped, including with an explicit false
  final override. Overrides apply only to skipping, never camera or timestamp. Videos always skip
  final regardless of rules/overrides; disable final-unskip for video rows.
- Generate original/final paths from a small documented template vocabulary.
  Use an explicit video template requirement; never put videos into the photo
  original layout by silently falling back.

Increment a global analysis revision when matching/naming/default settings change.
Each analyzed row records its revision. A mismatch shows **Analysis out of date**
while retaining every existing inferred/result value and its queue membership.
Display-name changes and adding an unused camera do not need a revision bump.
This deliberately overmarks some rows instead of building dependency tracking.

Stale rows remain eligible for sync using their displayed saved results, but sync
requires explicit confirmation when the ready batch contains stale rows. Show the
stale count in a TUI confirmation dialog or CLI prompt. Noninteractive CLI use
requires an explicit `--allow-stale` flag. Confirmation applies to that batch only.
Analyze is optional; sync never silently reanalyzes.

Skip/unskip should take effect immediately: save overrides and recompute that
photo's effective skip flags/readiness from saved inferred values. Do not apply
new rules as a side effect or claim its analysis is now current.

## Workflow tabs and filters

Six photo tabs, plus a small Configuration area and persistent Scan/Analyze/Sync
controls. All photo tabs share one table implementation and detail/actions panel.

| Tab | Meaning |
| --- | --- |
| New · Pending | Unsynced work missing required data or a present source |
| New · Ready | Every requested output can be produced; no unresolved duplicate/conflict or error |
| Skipped | Original is skipped, which also skips final |
| Archived | All requested outputs completed; skipped outputs count as intentionally omitted |
| Errors | Read/analyze/copy/conversion failure or source/original mismatch |
| Duplicates | New photos matching archived hashes (no attention needed), plus new/new duplicates and planned target clashes (attention needed) |

Primary-tab precedence: Skipped → Errors → Duplicates → Archived →
Ready → Pending. A completed archived keeper stays Archived when a new duplicate
appears. A new photo matching an archived source hash moves directly to Duplicates
and links the archived row, labeled **Already archived — no action needed**. It
does not need a skip or further analysis and is excluded from sync. Classify it as
soon as scanning supplies the hash.

New/new hash duplicates and planned target filename clashes are labeled **Needs
attention**. Resolve by skipping relevant outputs or adding rules to separate
target paths for different content. Do not automatically choose a new keeper.
Compute groups across the full inventory, including archived rows. A clashing
original folder means files would overwrite the same target file; sharing a folder
with different filenames is fine. Check planned final paths too. A mismatch against
an existing NAS file is an Error, even though its resolution resembles a clash.

Filters: camera, source folder, photo/video, year and optionally month. Include
All and Unknown, combine filters with AND, and show visible/total counts. A ZIP
folder is displayed as `container.zip/member-parent`. Keep selection predictable:
All/None/Invert act on filtered rows; clear selection on filter/tab changes so
hidden photos cannot receive bulk edits. Show the affected count before bulk work.

Photo actions:

- View in the desktop application, with temporary extraction for ZIP entries.
- View captured EXIF in a readable panel/dialog, with tree-sitter JSON highlighting.
- Add camera EXIF rule, prefilled from make/model, with optional serial refinement.
- Add filename rule, prefilled with an escaped exact basename for editing.
- Skip/unskip original; skip/unskip final; reset either override to rule defaults.
- Select all/none/invert and bulk skip/unskip either output; video final-unskip is
  never applied.

Archived duplicates need no resolution. Other duplicates are resolved by manual
skips (immediate) or filename/routing rule edits followed by Analyze. Do not
automatically merge rows or pick a keeper. Pending is ordinary
unfinished classification; Errors holds failed operations. Error-repair UI and
retry policy remain deferred; retain the failing stage/message and completed paths
so they can be designed later without hiding partial success.

## Sync

One operation selects New · Ready rows. Filters and selection do not silently
limit sync; show the batch count. Pending rows and unrelated errors do not block it.

For each row, copy the original unless skipped, generate the final unless skipped,
and persist each completed output separately. A final failure must retain the
successful original checkpoint. Re-running the eventual retry path should reuse
completed outputs. Sources remain untouched.

Skipping original always skips final, even if the final override/default says
false. There is no final-only processing without an archived original. Generate
finals from the copied/verified NAS original or an existing hash-matching original.
Unskipping original restores the independently stored final preference; explicitly
skipping final alone still permits original-only archiving. An unresolved requested
final leaves the photo Pending. Videos always skip final.

Use `Path.copy()` and ordinary parent-directory creation. Keep only practical
checks: configured NAS roots must already exist, destination paths must stay
relative to them, source bytes must still match their scanned hash before copying,
and existing destinations must not be overwritten. Inspect both relevant planned
NAS paths before copying an original or generating a final:

| Existing original | Existing final | Action |
| --- | --- | --- |
| Hash matches source | Present | Accept original and reuse final without regenerating or hashing final |
| Hash matches source | Absent | Accept original; generate final unless skipped |
| Absent | Present | Clash error; do not copy original or change final |
| Absent | Absent | Copy original unless skipped; generate final unless skipped |
| Hash differs from source | Either | Clash error; do not copy or generate either output |

An existing final is accepted on the basis of a matching original and its presence,
not a generated-final hash comparison. This also permits historical finals made
with different conversion settings. An existing final without its original is
treated as belonging to another original; detecting this before any copy is
essential. Skipping original omits both outputs without accessing NAS.
If final is skipped, ignore the final target and its presence; process only the
requested original. Both outputs skipped means no NAS work.

Record accepted existing paths as completed outputs. When all requested outputs
are satisfied, the photo is Archived. Errors identify the output and target path;
leave existing files untouched and retain any previously completed outputs.
Compare newly copied original bytes against source bytes before recording success.
Hashing and generation report progress. NAS comparison is explicit sync work, not
an offline Analyze side effect. No full NAS inventory or arbitrary-path historical
matching is planned initially.

Do not port mount registration, directory-descriptor traversal, hard-link
publication, fsync protocols, staging journals or race-proof no-replace behavior.
Assume one application writer and user-managed mounted NAS storage. Require
existing roots rather than silently creating a missing NAS root. Partial writes
are reported as errors; detailed repair can be decided later.

Finals are resized and named using the resolved capture date/time. Use Pillow:
`ImageOps.exif_transpose()` handles EXIF orientation, `thumbnail()` preserves aspect
ratio within a bounding box without upscaling, and `save()` writes JPEG. Application
code constructs the timestamp filename. This keeps conversion in Python without
a subprocess protocol. Do not include HEIF/HEIC plugins initially.

Confirmed final policy: JPEG, maximum width 1920 pixels and height 1080 pixels,
preserving aspect ratio without upscaling. Use the filename template
`YYYY_MM/YYYY_MM_DD-HH_MM_SS.jpg` from the resolved capture date/time.
JPEG quality and metadata/color policy remain to be settled.
Test portrait orientation on real samples. Preserve collision visibility; do not
silently append suffixes. Videos never generate finals.

If Pillow cannot process a photo format, record a blocking format-processing error
with the filename and reason in Errors; do not silently skip its requested final
or mark it Archived. Known unsupported formats should be identified before copying
the original. A failure discovered during conversion retains any completed original
checkpoint. Final-skipped photos do not require decoding for final generation.

Tool references: [Pillow tutorial](https://pillow.readthedocs.io/en/stable/handbook/tutorial.html),
[orientation](https://pillow.readthedocs.io/en/stable/reference/ImageOps.html),
[thumbnail/save](https://pillow.readthedocs.io/en/stable/reference/Image.html).

## Progress is part of each operation

Implement this in the first slice, in both interfaces:

- Startup, tab/filter loads, statistics refresh, rule saves, bulk changes, viewing
  preparation, scan, analyze and sync all provide immediate working feedback.
- Use a spinner while discovering/counting; show processed/total when known,
  current file/stage and success/error/skipped counts. Do not prewalk sources just
  to obtain a percentage. Large copies show byte progress if readily available;
  `Path.copy()` may instead show an active spinner and current filename.
- TUI I/O runs in a worker, with UI updates on Textual's main thread. Load large
  tables in batches; keep the spinner until results have actually rendered.
- CLI human-readable progress goes to stderr and flushes promptly. Default output
  can be simple text summaries; machine-readable JSON is optional, not the API.
- Every exit path clears busy state and gives completion, failure or cancellation
  feedback. Prevent overlapping mutations. Cancellation is cooperative between
  files/batches; completed work remains committed. A single copy may finish first.

## V2 references and lessons to retain

These are local implementation/test references, not modules v3 should import:

| V2 reference | Lesson to carry into v3 |
| --- | --- |
| `processing.py`, `tests/test_review_workflow.py` | Cached hash/EXIF; scan and Analyze are distinct; rules apply without rereading sources |
| `rules.py`, `inference.py`, `tests/test_rules.py` | Unmatched filenames remain included; deterministic rules; validate edits before saving |
| `tests/test_workflow.py` | Explicit video paths, ambiguous camera matches, timestamps preserve source meaning |
| `tests/test_review_workflow.py`, `tests/test_synchronization.py` | Pending does not block independent ready work; check collisions across the inventory |
| `viewing.py`, `tests/test_viewing.py` | ZIP entries need index identity; safe temporary filenames; desktop opening must not freeze TUI |
| `tui_camera_rule.py`, `tui_filename_rule.py` | Create rules from the selected photo instead of requiring manual transcription |
| `tests/test_tui.py` | Startup/table loading feedback, spinner cleanup after failure, responsive large lists, narrow terminals |
| `cli_progress.py`, `tests/test_cli.py` | Flushed progress with current paths/errors; visible no-op and failure summaries |
| `COMPLEXITY.md` | Function metrics help, but splitting a complicated model into many files does not simplify the product |

All paths in this table are relative to `../photo-archive-v2/` (production modules
under `src/photo_archive_v2/`). Port selected behavioral tests in simplified form;
do not port the entire v2 suite and thereby restore its requirements.

## Delivery plan

1. **Agree the small workflow.** Resolve the decisions below, sketch the six tabs,
   and finalize a minimal schema.
2. **First usable CLI slice.** Configure one source/camera, scan loose files with
   progress, analyze, list pending/ready and skip/unskip. Add representative tests
   for facts → rule → result → queue. Review schema and complexity here.
3. **Inventory and review.** ZIPs, cached rescans, duplicate/conflict SQL, rule CRUD,
   stats, revision indicator, filters and bulk actions. Test unchanged cached reads,
   changed sources, ZIP identity, rule precedence, automatic archived-duplicate
   classification, and retained stale results with sync confirmation.
4. **TUI slice.** Shared table in six tabs, forms, photo actions and progress for
   startup/filtering/loading as well as operations. Exercise narrow terminals,
   selection behavior and failures that must clear busy indicators.
5. **Sync end to end.** Implement simple original copy and the agreed final converter,
   partial-success checkpoints and errors. Test ready-only batches, original-skip
   propagation, skipping final alone, mandatory video final skips, source mismatch,
   hash-matched
   existing originals with reused finals, missing-original/existing-final clashes
   detected before copying, mismatching originals and final failure
   after original success, using temporary local directories rather than the NAS.
6. **Small real-data trial.** Review representative photos/videos/ZIPs and generated
   finals before a full run. Ruff format/lint, ty and the focused pytest suite pass.
   Reassess complexity before adding error-repair tools or historical imports.

## Decisions to return to

- Confirm JPEG quality and color/metadata policy; trial Pillow on Python 3.15 with
  representative inputs and verify blocking errors for unsupported formats.
- Decide how filename/routing rules express distinct paths when names clash.
  Camera/date corrections use rules, not overrides. The final filename template
  is confirmed above; clashes must remain visible rather than gain silent suffixes.
- Existing originals at planned NAS paths are hash-compared during sync; finals
  are reused by presence only when the original matches. Discovering arbitrary
  NAS paths or importing historical associations remains separate scope.
- Design error resolution only after ordinary scan → analyze → review → sync works well and as it becomes necessary. 
