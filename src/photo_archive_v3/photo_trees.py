"""Cross-filtered source, destination and date summaries for every photo workflow tab."""

from collections import Counter
from os.path import commonpath
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from .db import Row


def photo_folder(photo: Row, field: str) -> Path:
    if field == "result_timestamp":
        date = photo[field]
        return Path(date[:4]) / date[5:7] if date else Path("Unknown")
    if not photo[field]:
        return Path("Unknown")
    path = Path(photo[field])
    return path.parent if field == "result_original_path" else path


def in_folder(items: list[Row], field: str, selected: str | None) -> list[Row]:
    if selected is None:
        return items
    return [
        photo for photo in items if photo_folder(photo, field).is_relative_to(selected)
    ]


class FolderTree(Tree[str]):
    def __init__(self, title: str, field: str, widget_id: str) -> None:
        super().__init__(title, id=widget_id)
        self.field = field
        self.border_title = title
        self.auto_expand = False
        self.nodes: dict[Path, TreeNode[str]] = {}

    def rebuild(self, items: list[Row], selected: str | None, common: Path) -> None:
        expanded = {path: node.is_expanded for path, node in self.nodes.items()}
        self.reset(Text(f"All photos ({len(items)})"))
        self.root.expand()
        self.nodes = {}
        self.border_subtitle = f"Filter: {selected or 'All'}"
        if not items:
            self.move_cursor(self.root)
            return
        folders = Counter(photo_folder(photo, self.field) for photo in items)
        counts: Counter[Path] = Counter()
        for folder, count in folders.items():
            for ancestor in (folder, *folder.parents):
                counts[ancestor] += count
        paths = sorted(
            (p for p in counts if p.is_relative_to(common)),
            key=lambda p: (len(p.parts), str(p)),
        )
        for path in paths:
            if path == Path():
                self.nodes[path] = self.root
                continue
            parent = self.nodes.get(path.parent, self.root)
            label = str(path) if path == common else path.name
            self.nodes[path] = parent.add(
                Text(f"{label} ({counts[path]})"),
                data=str(path),
                expand=expanded.get(path, True),
            )
        # Build visible line positions before moving to a newly created node.
        self.get_node_at_line(0)
        self.move_cursor(
            self.nodes.get(Path(selected), self.root)
            if selected is not None
            else self.root
        )


class PhotoTrees(Horizontal):
    DEFAULT_CSS = """
    PhotoTrees { height: 12; display: none; }
    PhotoTrees FolderTree { width: 1fr; border: solid $panel; }
    PhotoTrees FolderTree:focus { border: solid $accent; }
    """

    class Changed(Message):
        """A tree filter changed; redraw the photo list and all summaries."""

    def __init__(self) -> None:
        super().__init__(id="photo-trees")
        self.filters_by_queue: dict[str, dict[str, str | None]] = {}
        self.filters: dict[str, str | None] = {
            "source_folder": None,
            "result_original_path": None,
            "result_timestamp": None,
        }

    def set_queue(self, queue: str) -> None:
        self.filters = self.filters_by_queue.setdefault(
            queue, dict.fromkeys(self.filters)
        )

    def compose(self) -> ComposeResult:
        yield FolderTree("Source folders", "source_folder", "source-folders")
        yield FolderTree(
            "NAS original folders", "result_original_path", "original-folders"
        )
        yield FolderTree("Year → Month", "result_timestamp", "capture-dates")

    def matching_items(
        self, items: list[Row], excluding: str | None = None
    ) -> list[Row]:
        for field, selected in self.filters.items():
            if field != excluding:
                items = in_folder(items, field, selected)
        return items

    def filter_items(self, items: list[Row]) -> list[Row]:
        source_root = (
            Path(commonpath([photo["source_folder"] for photo in items]))
            if items
            else Path("/")
        )
        for tree in self.query(FolderTree):
            tree.rebuild(
                self.matching_items(items, excluding=tree.field),
                self.filters[tree.field],
                source_root if tree.field == "source_folder" else Path(),
            )
        return self.matching_items(items)

    def on_tree_node_selected(self, event: Tree.NodeSelected[str]) -> None:
        event.stop()
        tree = event.control
        assert isinstance(tree, FolderTree)
        if self.filters[tree.field] != event.node.data:
            self.filters[tree.field] = event.node.data
            self.post_message(self.Changed())
