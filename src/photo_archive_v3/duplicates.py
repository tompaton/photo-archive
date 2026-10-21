"""Explain the same hash and destination comparisons used by the review view."""

from collections import defaultdict

from .db import Row

CRITERIA = {
    "extracted_sha256": "Matching content (SHA-256)",
    "result_original_path": "Original destination clash",
    "result_final_path": "Final destination clash",
}


def keys(photo: Row) -> list[tuple[str, str]]:
    fields = ["extracted_sha256"]
    fields.extend(
        f"result_{kind}_path"
        for kind in ("original", "final")
        if not photo[f"result_skip_{kind}"]
    )
    return [(field, photo[field]) for field in fields if photo[field] is not None]


class DuplicateIndex:
    """Index the full catalog once, independently of the active UI filters."""

    def __init__(self, photos: list[Row]) -> None:
        self.groups: dict[tuple[str, str], dict[int, Row]] = defaultdict(dict)
        for photo in photos:
            if not photo["skipped"] and (photo["completed"] or photo["status_present"]):
                for key in keys(photo):
                    self.groups[key][photo["id"]] = photo

    def criteria(self, photo: Row) -> list[tuple[str, str]]:
        return [
            key
            for key in keys(photo)
            if len(self.groups.get(key, {}))
            > int(photo["id"] in self.groups.get(key, {}))
        ]

    def summary(self, photo: Row) -> str:
        reasons = "; ".join(CRITERIA[field] for field, _ in self.criteria(photo))
        return f"{photo['duplicate_note']} · {reasons}"

    def details(self, photo: Row) -> str:
        matches: dict[int, tuple[Row, list[str]]] = {}
        for key in self.criteria(photo):
            field, value = key
            for other_id, other in self.groups[key].items():
                if other_id != photo["id"]:
                    matches.setdefault(other_id, (other, []))[1].append(
                        f"{CRITERIA[field]}: {value}"
                    )
        sections = [
            f"Duplicates / clashes ({len(matches)} related photos)",
            photo["duplicate_note"],
            "Matches use hashes or destination filenames. Source filenames and "
            "timestamps alone are not criteria. Related photos include those hidden by filters.",
        ]
        for other, reasons in matches.values():
            source = other["source_path"]
            if other["zip_member"]:
                source += f" :: {other['zip_member']} (ZIP entry {other['zip_index']})"
            sections.append(
                f"#{other['id']} [{other['queue']}]\n{source}\n" + "\n".join(reasons)
            )
        return "\n\n".join(sections)
