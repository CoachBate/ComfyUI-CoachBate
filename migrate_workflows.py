"""
Migrate ComfyUI workflow JSON files from WhatDreamsCost node IDs to CoachBate node IDs.

Usage:
    # Overwrite in place (backs up originals as .json.bak):
    python migrate_workflows.py <directory>

    # Write migrated files to a subdirectory instead:
    python migrate_workflows.py <directory> --out <output-directory>

    # Include subdirectories:
    python migrate_workflows.py <directory> --recursive
    python migrate_workflows.py <directory> --out <output-directory> --recursive

Preserves the original file's created date on the output file.
"""

import sys
import json
import shutil
import os
import argparse
from pathlib import Path

NODE_ID_MAP = {
    "LTXDirector":      "CoachBateLTXDirector",
    "LTXDirectorGuide": "CoachBateLTXDirectorGuide",
    "WDCLTXTrimLatent": "CoachBateLTXTrimLatent",
}


def get_created_time(path: Path) -> float:
    stat = path.stat()
    # st_birthtime on Windows/Mac; fall back to st_ctime on Linux
    return getattr(stat, "st_birthtime", stat.st_ctime)


def set_created_time(path: Path, created: float):
    """Restore original created time. On Windows uses SetFileTime via ctypes."""
    try:
        import ctypes
        import ctypes.wintypes as wt

        GENERIC_WRITE = 0x40000000
        OPEN_EXISTING = 3
        FILE_ATTRIBUTE_NORMAL = 0x80

        kernel32 = ctypes.windll.kernel32

        handle = kernel32.CreateFileW(
            str(path), GENERIC_WRITE, 0, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None
        )
        if handle == wt.HANDLE(-1).value:
            return

        # Convert Unix timestamp to FILETIME (100-ns intervals since 1601-01-01)
        EPOCH_DIFF = 116444736000000000
        ft_val = int(created * 10_000_000) + EPOCH_DIFF
        ft = wt.FILETIME(ft_val & 0xFFFFFFFF, ft_val >> 32)

        kernel32.SetFileTime(handle, ctypes.byref(ft), None, None)
        kernel32.CloseHandle(handle)
    except Exception:
        pass  # Non-Windows or permission issue — skip silently


def migrate_workflow(src: Path, dest: Path) -> bool:
    text = src.read_text(encoding="utf-8")

    if not any(old in text for old in NODE_ID_MAP):
        return False

    data = json.loads(text)
    changed = False

    # New ComfyUI v2 format: {"nodes": [{..."type": "LTXDirector"...}, ...]}
    if isinstance(data, dict) and isinstance(data.get("nodes"), list):
        for node in data["nodes"]:
            if not isinstance(node, dict):
                continue
            t = node.get("type")
            if t in NODE_ID_MAP:
                node["type"] = NODE_ID_MAP[t]
                changed = True

    # Old v1 format: {"1": {"class_type": "LTXDirector", ...}, ...}
    elif isinstance(data, dict):
        for node in data.values():
            if not isinstance(node, dict):
                continue
            ct = node.get("class_type")
            if ct in NODE_ID_MAP:
                node["class_type"] = NODE_ID_MAP[ct]
                changed = True

    if not changed:
        return False

    created = get_created_time(src)

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    set_created_time(dest, created)

    return True


def main():
    parser = argparse.ArgumentParser(description="Migrate WhatDreamsCost node IDs to CoachBate.")
    parser.add_argument("directory", help="Directory containing workflow JSON files")
    parser.add_argument("--out", metavar="OUTPUT_DIR",
                        help="Write migrated files here instead of overwriting (preserves directory structure)")
    parser.add_argument("--recursive", "-r", action="store_true",
                        help="Also scan subdirectories")
    args = parser.parse_args()

    root = Path(args.directory)
    if not root.is_dir():
        print(f"Not a directory: {root}")
        sys.exit(1)

    out_root = Path(args.out) if args.out else None
    files = list(root.rglob("*.json") if args.recursive else root.glob("*.json"))
    print(f"Scanning {len(files)} JSON files in {root}...\n")

    migrated = 0
    for src in files:
        if out_root:
            dest = out_root / src.relative_to(root)
        else:
            # In-place: back up original first
            backup = src.with_suffix(".json.bak")
            shutil.copy2(src, backup)
            dest = src

        if migrate_workflow(src if out_root else backup, dest):
            print(f"  Migrated: {src.relative_to(root)}")
            migrated += 1
        elif not out_root:
            # Nothing changed — remove the backup we just made
            backup.unlink()

    print(f"\nDone — {migrated} migrated, {len(files) - migrated} unchanged.")
    if migrated:
        print("Node ID changes applied:")
        for old, new in NODE_ID_MAP.items():
            print(f"  {old} -> {new}")
        if out_root:
            print(f"\nMigrated files written to: {out_root}")
        else:
            print("\nOriginals backed up as .json.bak alongside each migrated file.")


if __name__ == "__main__":
    main()
