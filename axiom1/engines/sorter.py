"""Sorter engine: plan and apply file reorganization moves based on pattern rules."""
import fnmatch
from pathlib import Path, PurePosixPath

from ._base import EngineError, relative_path

SPEC = {
    "name": "sorter",
    "version": 1,
    "summary": "plan and apply file moves based on pattern rules without guessing",
    "functions": {
        "plan_moves": {
            "args": ["files", "rules"],
            "returns": "list of [{from, to}] move operations",
            "io": False,
            "examples": [
                {
                    "args": {
                        "files": ["notes.txt", "doc.pdf", "image.png"],
                        "rules": [
                            {"match": "*.pdf", "folder": "Documents"},
                            {"match": "*.txt", "folder": "Text"},
                        ],
                    },
                    "returns": [
                        {"from": "notes.txt", "to": "Text/notes.txt"},
                        {"from": "doc.pdf", "to": "Documents/doc.pdf"},
                    ],
                },
                {
                    "args": {
                        "files": ["doc.pdf"],
                        "rules": [
                            {"match": "*.pdf", "folder": "Documents"},
                            {"match": "doc.*", "folder": "Archive"},
                        ],
                    },
                    "refuses": "conflicting folders",
                },
                {
                    "args": {
                        "files": ["folderA/doc.pdf", "folderB/doc.pdf"],
                        "rules": [{"match": "*.pdf", "folder": "Documents"}],
                    },
                    "refuses": "collision",
                },
            ],
        },
        "apply_moves": {
            "args": ["moves"],
            "returns": "{moved: count of moved files}",
            "io": True,
            "examples": [],
        },
    },
}


def plan_moves(files, rules):
    """Plan destination moves for a list of files based on pattern matching rules.

    Each rule has 'match' (case-insensitive fnmatch on filename) and 'folder'.
    Files matching no rules remain where they are (not in plan).
    Refuses:
      - a file matched by two rules with different folders (names both in the message).
      - two files that would land on the same destination path (collision).
      - paths that leave the job folder.
    """
    if not isinstance(files, (list, tuple)):
        raise EngineError("files must be a list of relative file paths")
    if not isinstance(rules, (list, tuple)):
        raise EngineError("rules must be a list of rule dicts")

    for i, r in enumerate(rules):
        if not isinstance(r, dict):
            raise EngineError(f"rule {i} must be a dict")
        if "match" not in r or "folder" not in r:
            raise EngineError(f"rule {i} must contain 'match' and 'folder'")
        relative_path(r["folder"])

    for f in files:
        if not isinstance(f, str) or not f.strip():
            raise EngineError("file paths must be non-empty strings")
        relative_path(f)

    plan = []
    seen_destinations = {}

    for f in files:
        norm_f = f.replace("\\", "/")
        fname = PurePosixPath(norm_f).name

        matched_folders = []
        for r in rules:
            pat = r["match"]
            if fnmatch.fnmatch(fname.lower(), pat.lower()):
                folder = r["folder"].replace("\\", "/").rstrip("/")
                matched_folders.append(folder)

        unique_folders = sorted(set(matched_folders))
        if len(unique_folders) > 1:
            folders_str = ", ".join(repr(u) for u in unique_folders)
            raise EngineError(f"file {f!r} matches multiple rules with conflicting folders: {folders_str}")

        if not unique_folders:
            continue

        target_folder = unique_folders[0]
        target_path = f"{target_folder}/{fname}"

        if target_path == norm_f:
            continue

        if target_path in seen_destinations:
            orig = seen_destinations[target_path]
            raise EngineError(f"collision: multiple files ({orig!r} and {f!r}) would land on the same destination {target_path!r}")

        seen_destinations[target_path] = f
        plan.append({"from": f, "to": target_path})

    return plan


def apply_moves(moves):
    """Apply planned file moves safely and atomically (all-or-nothing).

    Refuses before moving anything if:
      - any source file is missing.
      - any destination already exists (never overwrites).
      - any path leaves the job's directory.
    """
    if not isinstance(moves, (list, tuple)):
        raise EngineError("moves must be a list of move dicts")

    to_execute = []
    planned_destinations = set()

    for i, m in enumerate(moves, 1):
        if not isinstance(m, dict):
            raise EngineError(f"move {i} must be a dict with 'from' and 'to'")
        if "from" not in m or "to" not in m:
            raise EngineError(f"move {i} must have 'from' and 'to' fields")

        src_str = relative_path(m["from"])
        dst_str = relative_path(m["to"])

        src = Path(src_str)
        dst = Path(dst_str)

        if not src.is_file():
            raise EngineError(f"source file {m['from']!r} does not exist")
        if dst.exists():
            raise EngineError(f"destination {m['to']!r} already exists; cannot overwrite")
        if dst in planned_destinations:
            raise EngineError(f"duplicate destination {m['to']!r} in moves plan")

        planned_destinations.add(dst)
        to_execute.append((src, dst))

    for src, dst in to_execute:
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)

    return {"moved": len(to_execute)}
