"""
routes.py — CoachBate HTTP API endpoints

Registers custom routes on ComfyUI's PromptServer so the frontend can
trigger actions (e.g. skip the current shot) without re-queuing.
"""

import asyncio
import json
import logging
import os
import sys
from aiohttp import web
from server import PromptServer

from .nodes import _shot_loader_state, _state_lock
from .contains_search import build_predicate as build_contains_predicate

log = logging.getLogger("coachbate")
routes = PromptServer.instance.routes


def _open_file_dialog_sync(title="Select file", filetypes=None):
    """Open a native Windows file-open dialog and return the chosen path (or '' if cancelled)."""
    import tkinter as tk
    from tkinter import filedialog
    if filetypes is None:
        filetypes = [("All files", "*.*")]
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askopenfilename(title=title, filetypes=filetypes)
    root.destroy()
    return path or ""


@routes.get("/coachbate/platform")
async def get_platform(request):
    return web.json_response({"windows": sys.platform == "win32"})


@routes.get("/coachbate/browse_json")
async def browse_json(request):
    if sys.platform != "win32":
        return web.json_response({"error": "File browser only available on Windows"}, status=400)
    try:
        loop = asyncio.get_running_loop()
        path = await loop.run_in_executor(
            None, _open_file_dialog_sync,
            "Select shotlist JSON file",
            [("JSON files", "*.json"), ("All files", "*.*")],
        )
        return web.json_response({"path": path})
    except ImportError:
        log.error("[CoachBate] tkinter not available")
        return web.json_response({"error": "tkinter is not installed in this Python environment"}, status=500)
    except Exception as exc:
        log.error("[CoachBate] /coachbate/browse_json error: %s", exc)
        return web.json_response({"error": str(exc)}, status=500)


@routes.get("/coachbate/browse_media")
async def browse_media(request):
    if sys.platform != "win32":
        return web.json_response({"error": "File browser only available on Windows"}, status=400)
    try:
        loop = asyncio.get_running_loop()
        path = await loop.run_in_executor(
            None, _open_file_dialog_sync,
            "Select PNG or video file",
            [
                ("Media files", "*.png *.mp4 *.mov *.mkv *.webm"),
                ("PNG images", "*.png"),
                ("Video files", "*.mp4 *.mov *.mkv *.webm"),
                ("All files", "*.*"),
            ],
        )
        return web.json_response({"path": path})
    except ImportError:
        log.error("[CoachBate] tkinter not available")
        return web.json_response({"error": "tkinter is not installed in this Python environment"}, status=500)
    except Exception as exc:
        log.error("[CoachBate] /coachbate/browse_media error: %s", exc)
        return web.json_response({"error": str(exc)}, status=500)


@routes.post("/coachbate/restart")
async def restart_at_index(request):
    """
    Seed stored_index to an explicit value chosen by the user.

    Expects JSON body: { "index": <int> }
    """
    try:
        body  = await request.json()
        index = int(body.get("index", 0))
        with _state_lock:
            _shot_loader_state["stored_index"] = index
            # _seeded=False causes execute() to re-seed from the widget on the next run.
            _shot_loader_state["_seeded"]      = False
        log.info("[CoachBate] Restart — stored_index set to %d", index)
        return web.json_response({"stored_index": index})
    except Exception as exc:
        log.error("[CoachBate] /coachbate/restart error: %s", exc)
        return web.json_response({"error": str(exc)}, status=400)


@routes.post("/coachbate/skip")
async def skip_shot(request):
    """
    Advance stored_index past the current shot so the next queue run
    picks up the following non-DONE shot.

    Expects JSON body: { "current_index": <int>, "total": <int> }
    Both values come from the node's last execution so the server can
    do a safe conditional advance (avoids double-skipping if the button
    is clicked more than once before the next run).
    """
    try:
        body = await request.json()
        total = int(body.get("total", 1))

        with _state_lock:
            current = int(body.get("current_index", _shot_loader_state["stored_index"]))
            # Only advance if stored_index still points at (or before) the shot
            # the user is looking at — guards against double-clicks.
            if _shot_loader_state["stored_index"] % total == current % total:
                _shot_loader_state["stored_index"] = (current + 1) % total
                log.info("[CoachBate] Shot skipped — stored_index now %d", _shot_loader_state["stored_index"])
            new_index = _shot_loader_state["stored_index"]

        return web.json_response({"stored_index": new_index})

    except Exception as exc:
        log.error("[CoachBate] /coachbate/skip error: %s", exc)
        return web.json_response({"error": str(exc)}, status=400)


_OVERRIDES_FILE = os.path.join(
    os.path.dirname(__file__), "workflow_path_autofix_overrides.txt"
)


@routes.get("/coachbate/workflow_path_autofix/overrides")
async def workflow_path_autofix_overrides(request):
    """
    Serve the ordered model-path override rules from
    workflow_path_autofix_overrides.txt so the frontend auto-fix can apply them.

    Format (one rule per line): search<TAB>replacement
    Blank lines and lines starting with '#' are ignored.  Rule strings are
    returned verbatim (the frontend does its own path normalization) and order
    is preserved — the frontend applies rules first-match-wins per widget.
    """
    try:
        overrides = []
        if os.path.isfile(_OVERRIDES_FILE):
            with open(_OVERRIDES_FILE, "r", encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.rstrip("\n").rstrip("\r")
                    if not line.strip() or line.lstrip().startswith("#"):
                        continue
                    search, sep, replacement = line.partition("\t")
                    if not sep or not search:
                        log.warning("[CoachBate] Skipping malformed override line: %r", raw)
                        continue
                    overrides.append({"search": search, "replacement": replacement})
        return web.json_response({"overrides": overrides})
    except Exception as exc:
        log.error("[CoachBate] /coachbate/workflow_path_autofix/overrides error: %s", exc)
        return web.json_response({"error": str(exc)}, status=500)


@routes.post("/coachbate/workflow_path_autofix/log")
async def workflow_path_autofix_log(request):
    """
    Record the model-path replacements the frontend applied on workflow load.

    Expects JSON body: { "replacements": [ { "original": ..., "replacement": ... }, ... ] }
    """
    try:
        body = await request.json()
        replacements = body.get("replacements", []) or []
        for entry in replacements:
            log.info(
                "[CoachBate] path auto-fix: %s -> %s",
                entry.get("original"),
                entry.get("replacement"),
            )
        return web.json_response({"ok": True})
    except Exception as exc:
        log.error("[CoachBate] /coachbate/workflow_path_autofix/log error: %s", exc)
        return web.json_response({"error": str(exc)}, status=400)


def _read_lower(path):
    """Read a workflow file as lowercase text for the contains-search scan.
    Runs in a thread-pool executor so large/slow reads don't block the loop."""
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        return fh.read().lower()


_GREP_PROGRESS_EVERY = 50  # send a progress line at most every N files scanned


@routes.get("/coachbate/workflows/grep")
async def workflows_grep(request):
    """
    Search inside every workflow JSON's raw text for the Workflows+ panel's
    "Contains" mode (e.g. finding a prompt used once, location unknown).

    Streams newline-delimited JSON so the frontend can show scan progress and
    cancel mid-scan (abort the fetch; this handler notices the closed
    connection at the next per-file check and stops reading further files).

    Query params:
      q=<AND/OR/phrase query>  (same grammar as filename search, see
        contains_search.py / web/js/workflows_plus/queryParser.js)
      folder=<relative path>   (optional; scope the scan to this folder,
        e.g. "MCB" or "MCB/sub" — default "" = the workflows root)
      recurse=0                (optional; when present and falsy, only scan
        files directly inside `folder`, not its subfolders — the "search
        subfolders" toggle set to OFF; default is recursive)

    Response body (application/x-ndjson), one JSON object per line:
      {"type": "total", "total": N}
      {"type": "progress", "scanned": i, "total": N}   (periodic)
      {"type": "done", "matches": [{"path", "size", "modified", "created"}, ...]}
    """
    query = request.query.get("q", "")
    predicate = build_contains_predicate(query)
    if predicate is None:
        return web.json_response({"error": "empty query"}, status=400)
    recurse = request.query.get("recurse", "1") not in ("0", "false", "no")
    folder_param = request.query.get("folder", "").strip().strip("/")

    try:
        workflows_dir = PromptServer.instance.user_manager.get_request_user_filepath(
            request, "workflows", create_dir=False
        )
    except KeyError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    if not workflows_dir or not os.path.isdir(workflows_dir):
        return web.json_response({"error": "workflows directory not found"}, status=404)

    workflows_dir = os.path.normpath(workflows_dir)
    if folder_param:
        scan_dir = os.path.normpath(os.path.join(workflows_dir, folder_param))
        if os.path.commonpath([scan_dir, workflows_dir]) != workflows_dir:
            return web.json_response({"error": "invalid folder"}, status=403)
    else:
        scan_dir = workflows_dir
    if not os.path.isdir(scan_dir):
        return web.json_response({"error": "folder not found"}, status=404)

    file_list = []
    if not recurse:
        # "Search subfolders" toggle OFF: only files directly inside scan_dir.
        try:
            for fn in os.listdir(scan_dir):
                if fn.startswith(".") or not fn.lower().endswith(".json"):
                    continue
                abspath = os.path.join(scan_dir, fn)
                if os.path.isfile(abspath):
                    rel = os.path.relpath(abspath, workflows_dir).replace(os.sep, "/")
                    file_list.append((rel, abspath))
        except OSError as exc:
            return web.json_response({"error": str(exc)}, status=500)
    else:
        for dirpath, _dirnames, filenames in os.walk(scan_dir):
            for fn in filenames:
                if fn.startswith(".") or not fn.lower().endswith(".json"):
                    continue
                abspath = os.path.join(dirpath, fn)
                rel = os.path.relpath(abspath, workflows_dir).replace(os.sep, "/")
                file_list.append((rel, abspath))
    total = len(file_list)

    resp = web.StreamResponse(
        status=200,
        headers={"Content-Type": "application/x-ndjson", "Cache-Control": "no-cache"},
    )
    await resp.prepare(request)

    async def write_line(obj):
        await resp.write((json.dumps(obj) + "\n").encode("utf-8"))

    await write_line({"type": "total", "total": total})

    matches = []
    loop = asyncio.get_running_loop()
    last_progress_at = 0

    for i, (rel, abspath) in enumerate(file_list):
        transport = request.transport
        if transport is None or transport.is_closing():
            log.info("[CoachBate] workflows/grep cancelled by client at %d/%d", i, total)
            return resp

        try:
            text = await loop.run_in_executor(None, _read_lower, abspath)
        except OSError as exc:
            log.warning("[CoachBate] workflows/grep skipping unreadable file %s: %s", rel, exc)
            text = None

        if text is not None and predicate(text):
            try:
                st = os.stat(abspath)
                matches.append(
                    {
                        "path": rel,
                        "size": st.st_size,
                        "modified": int(st.st_mtime * 1000),
                        "created": int(st.st_ctime * 1000),
                    }
                )
            except OSError:
                pass

        if i - last_progress_at >= _GREP_PROGRESS_EVERY or i == total - 1:
            last_progress_at = i
            try:
                await write_line({"type": "progress", "scanned": i + 1, "total": total})
            except (ConnectionResetError, ConnectionAbortedError):
                log.info("[CoachBate] workflows/grep client disconnected at %d/%d", i + 1, total)
                return resp

    try:
        await write_line({"type": "done", "matches": matches})
        await resp.write_eof()
    except (ConnectionResetError, ConnectionAbortedError):
        pass

    return resp
