"""Desktop viewing with application-owned temporary ZIP previews."""

import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from .db import Row
from .media import open_photo


class Viewer:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="photo-archive-view-")

    def open(self, photo: Row) -> None:
        path = Path(photo["source_path"])
        if photo["zip_index"] >= 0:
            path = (
                Path(self.temporary.name)
                / f"{photo['id']}{Path(photo['basename']).suffix}"
            )
            with open_photo(photo) as source, path.open("wb") as output:
                shutil.copyfileobj(source, output)
        if not path.is_file():
            raise ValueError(f"Source unavailable: {path}")
        command = "open" if sys.platform == "darwin" else "xdg-open"
        process = subprocess.Popen([command, str(path)], start_new_session=True)
        threading.Thread(target=process.wait, daemon=True).start()
