import copy
import ctypes
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, PngImagePlugin

import folder_paths

from .nodes import log

try:
    from ComfyUI_VideoHelperSuite.videohelpersuite.utils import ffmpeg_path  # type: ignore
except Exception:
    try:
        from videohelpersuite.utils import ffmpeg_path  # type: ignore
    except Exception:
        ffmpeg_path = None

if not ffmpeg_path:
    ffmpeg_path = shutil.which("ffmpeg")


GEMMA_NODE_TYPE = "GemmaAPITextEncode"
GEMMA_WORKFLOW_WIDGET_INDEX = 0
GEMMA_PROMPT_INPUT_NAME = "api_key"
# Field names that hold a secret regardless of which node type carries them.
# Scrubbing keys off THIS NAME rather than off the node's type or the value's
# shape is what actually generalizes: it also catches a Gemma node wrapped
# inside a subgraph, where the top-level node's `type` is the subgraph's own
# UUID rather than "GemmaAPITextEncode", and it needs no assumption about what
# a given API provider's key looks like.
SECRET_FIELD_NAMES = ("api_key",)
# Backstop only -- a value-shape guess is inherently a step behind whatever
# key format actually gets issued (real ltxv_ tokens use base64url, which
# includes '-'; the field-name scrub above is the real defense).
LTXV_SECRET_RE = re.compile(r"^ltxv_[\w-]{20,}$")
SAFE_BEHAVIORS = (
    "include api key (normal)",
    "remove api key before saving mp4",
    "create additional video without api key",
)
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
PNG_EXTENSIONS = {".png"}
SUPPORTED_EXTENSIONS = VIDEO_EXTENSIONS | PNG_EXTENSIONS


def _escape_ffmpeg_metadata(key, value):
    value = str(value)
    value = value.replace("\\", "\\\\")
    value = value.replace(";", "\\;")
    value = value.replace("#", "\\#")
    value = value.replace("=", "\\=")
    value = value.replace("\n", "\\\n")
    return f"{key}={value}"


def _unescape_ffmpeg_metadata(value):
    out = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            out.append(value[index + 1])
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _scrub_json_string(value):
    try:
        payload = json.loads(value)
    except Exception:
        return value, False
    sanitized, changed = sanitize_metadata_payload(payload)
    if not changed:
        return value, False
    return json.dumps(sanitized, separators=(",", ":")), True


def _looks_like_ltxv_secret(value):
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    return bool(LTXV_SECRET_RE.fullmatch(candidate))


def _scrub_nested_payload(value, key_hint=None):
    """Scrub ltxv-pattern secrets anywhere in an arbitrary JSON structure."""
    changed = False

    if isinstance(value, dict):
        for key, item in value.items():
            new_item, item_changed = _scrub_nested_payload(item, key)
            if item_changed:
                value[key] = new_item
                changed = True
        return value, changed

    if isinstance(value, list):
        for index, item in enumerate(value):
            new_item, item_changed = _scrub_nested_payload(item, key_hint)
            if item_changed:
                value[index] = new_item
                changed = True
        return value, changed

    if isinstance(value, str) and _looks_like_ltxv_secret(value):
        return "", True

    return value, False


def _scrub_node_by_field_name(node):
    """Blank any SECRET_FIELD_NAMES value found on a single node dict, by
    whichever representation it's stored in. Node-type agnostic on purpose:
    the caller doesn't need to know this is a Gemma node, only that ComfyUI
    named the field "api_key"."""
    changed = False

    # Newer ComfyUI: a named dict living alongside the positional array.
    # Reading it directly means no positional guesswork -- the field is
    # findable by name however the node happens to be wired or nested.
    named = node.get("widgets_values_named")
    positional = node.get("widgets_values")
    if isinstance(named, dict):
        order = list(named.keys())
        for i, field in enumerate(order):
            if field not in SECRET_FIELD_NAMES:
                continue
            val = named.get(field)
            if isinstance(val, str) and val:
                named[field] = ""
                changed = True
            # widgets_values_named preserves the SAME ordering as the
            # positional widgets_values array it was derived from, so index i
            # here is index i there too -- keep both copies in sync.
            if isinstance(positional, list) and i < len(positional):
                if isinstance(positional[i], str) and positional[i]:
                    positional[i] = ""
                    changed = True

    # Array-based `inputs` (no widgets_values_named): map the api_key INPUT
    # to its widgets_values slot by counting preceding unlinked widget inputs.
    inputs_def = node.get("inputs")
    if isinstance(inputs_def, list) and isinstance(positional, list) and not isinstance(named, dict):
        widget_offset = 0
        for inp in inputs_def:
            if not isinstance(inp, dict):
                continue
            if inp.get("link") is not None:      # linked inputs consume no slot
                continue
            if not inp.get("widget"):
                continue
            if inp.get("name") in SECRET_FIELD_NAMES:
                if widget_offset < len(positional):
                    val = positional[widget_offset]
                    if isinstance(val, str) and val:
                        positional[widget_offset] = ""
                        changed = True
                break
            widget_offset += 1

    # Oldest format: no `inputs` array at all, just a hardcoded slot. Guard
    # with the value-shape regex here ONLY, since without field names or a
    # typed `inputs` array there is nothing else to key off of.
    if (not isinstance(named, dict) and not isinstance(inputs_def, list)
            and node.get("type") == GEMMA_NODE_TYPE and isinstance(positional, list)
            and len(positional) > GEMMA_WORKFLOW_WIDGET_INDEX):
        val = positional[GEMMA_WORKFLOW_WIDGET_INDEX]
        if _looks_like_ltxv_secret(val):
            positional[GEMMA_WORKFLOW_WIDGET_INDEX] = ""
            changed = True

    return changed


def _scrub_workflow_payload(workflow):
    """Scrub secret fields from every node in workflow JSON format,
    including subgraph INSTANCE nodes (whose `type` is the subgraph's own
    UUID, not the wrapped node's type -- a type == "GemmaAPITextEncode" check
    never even looks at these) and subgraph DEFINITION nodes (in case a
    template still carries a literal value)."""
    changed = False
    nodes = workflow.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if isinstance(node, dict):
                changed = _scrub_node_by_field_name(node) or changed

    for sg in (workflow.get("definitions") or {}).get("subgraphs", []) or []:
        if not isinstance(sg, dict):
            continue
        for node in sg.get("nodes", []) or []:
            if isinstance(node, dict):
                changed = _scrub_node_by_field_name(node) or changed

    return changed


def _scrub_prompt_payload(prompt):
    """Scrub secret fields from any node in prompt API format.

    Matches by INPUT NAME (SECRET_FIELD_NAMES), not by node class_type: this
    graph is already flattened (ComfyUI expands subgraphs into it with
    compound ids like "5851:3298", each keeping its real class_type), so a
    type check does work here today for GemmaAPITextEncode specifically -- but
    keying off the field name instead means a future node type that exposes
    its own "api_key" input is covered automatically, with no registry update.
    """
    changed = False
    if not isinstance(prompt, dict):
        return changed
    for node in prompt.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for field in SECRET_FIELD_NAMES:
            val = inputs.get(field)
            if isinstance(val, str) and val:
                inputs[field] = ""
                changed = True
    return changed


def sanitize_metadata_payload(payload):
    sanitized = copy.deepcopy(payload)
    changed = False
    sanitized, nested_changed = _scrub_nested_payload(sanitized)
    changed = changed or nested_changed
    if isinstance(sanitized, dict):
        changed = _scrub_workflow_payload(sanitized) or changed
        changed = _scrub_prompt_payload(sanitized) or changed
    return sanitized, changed


def sanitize_prompt_and_pnginfo(prompt=None, extra_pnginfo=None):
    prompt_out = prompt
    extra_out = extra_pnginfo
    changed = False

    if prompt is not None:
        prompt_out, prompt_changed = sanitize_metadata_payload(prompt)
        changed = changed or prompt_changed

    if extra_pnginfo is not None:
        extra_out, extra_changed = sanitize_metadata_payload(extra_pnginfo)
        changed = changed or extra_changed

    return prompt_out, extra_out, changed


def _write_png_with_sanitized_metadata(source_path, target_path):
    changed = False
    with Image.open(source_path) as image:
        pnginfo = PngImagePlugin.PngInfo()
        text_items = {}

        if hasattr(image, "text"):
            text_items.update(image.text)
        for key, value in image.info.items():
            if isinstance(value, str) and key not in text_items:
                text_items[key] = value

        for key, value in text_items.items():
            new_value = value
            item_changed = False
            if key in {"prompt", "workflow"}:
                new_value, item_changed = _scrub_json_string(value)
            pnginfo.add_text(key, new_value)
            changed = changed or item_changed

        if not changed:
            return False

        save_kwargs = {}
        if "icc_profile" in image.info:
            save_kwargs["icc_profile"] = image.info["icc_profile"]
        if "dpi" in image.info:
            save_kwargs["dpi"] = image.info["dpi"]

        image.save(target_path, pnginfo=pnginfo, **save_kwargs)
    return changed


def _read_ffmetadata(source_path):
    if ffmpeg_path is None:
        raise RuntimeError("ffmpeg is required to sanitize video metadata but was not found.")
    result = subprocess.run(
        [ffmpeg_path, "-v", "error", "-i", source_path, "-f", "ffmetadata", "-"],
        capture_output=True,
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout


def _sanitize_ffmetadata_text(metadata_text):
    changed = False
    metadata = {}

    for line in metadata_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(";") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        metadata[key] = _unescape_ffmpeg_metadata(value)

    for key in ("prompt", "workflow"):
        if key not in metadata:
            continue
        sanitized_value, item_changed = _scrub_json_string(metadata[key])
        metadata[key] = sanitized_value
        changed = changed or item_changed

    if not changed:
        return None

    lines = [";FFMETADATA1"]
    for key, value in metadata.items():
        lines.append(_escape_ffmpeg_metadata(key, value))
    return "\n".join(lines) + "\n"


def _write_video_with_sanitized_metadata(source_path, target_path):
    metadata_text = _read_ffmetadata(source_path)
    sanitized_text = _sanitize_ffmetadata_text(metadata_text)
    if sanitized_text is None:
        return False

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".ffmeta", delete=False) as handle:
        handle.write(sanitized_text)
        metadata_path = handle.name

    try:
        subprocess.run(
            [
                ffmpeg_path,
                "-v",
                "error",
                "-y",
                "-i",
                source_path,
                "-i",
                metadata_path,
                "-map",
                "0",
                "-map_metadata",
                "1",
                "-c",
                "copy",
                "-movflags",
                "use_metadata_tags",
                target_path,
            ],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    finally:
        try:
            os.remove(metadata_path)
        except OSError:
            pass
    return True


def _set_creation_time_windows(path, creation_time_ns):
    FILE_WRITE_ATTRIBUTES = 0x0100
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    handle = ctypes.windll.kernel32.CreateFileW(
        str(path),
        FILE_WRITE_ATTRIBUTES,
        0,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        raise ctypes.WinError()

    try:
        filetime_value = int(creation_time_ns // 100 + 116444736000000000)

        class FILETIME(ctypes.Structure):
            _fields_ = [
                ("dwLowDateTime", ctypes.c_uint32),
                ("dwHighDateTime", ctypes.c_uint32),
            ]

        creation = FILETIME(filetime_value & 0xFFFFFFFF, filetime_value >> 32)
        if not ctypes.windll.kernel32.SetFileTime(handle, ctypes.byref(creation), None, None):
            raise ctypes.WinError()
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def capture_file_times(path):
    stat = os.stat(path)
    return {
        "atime_ns": stat.st_atime_ns,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": getattr(stat, "st_ctime_ns", None),
    }


def apply_file_times(path, times):
    os.utime(path, ns=(times["atime_ns"], times["mtime_ns"]))
    if os.name == "nt" and times.get("ctime_ns") is not None:
        _set_creation_time_windows(path, times["ctime_ns"])


def send_to_recycle_bin(path):
    FO_DELETE = 3
    FOF_ALLOWUNDO = 0x0040
    FOF_NOCONFIRMATION = 0x0010
    FOF_SILENT = 0x0004

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("wFunc", ctypes.c_uint),
            ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", ctypes.c_bool),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]

    operation = SHFILEOPSTRUCTW()
    operation.wFunc = FO_DELETE
    operation.pFrom = str(path) + "\0\0"
    operation.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0:
        raise OSError(f"SHFileOperationW failed with code {result}")
    if operation.fAnyOperationsAborted:
        raise OSError("Recycle Bin operation was aborted.")


def sanitize_file_metadata_to_path(source_path, target_path):
    extension = Path(source_path).suffix.lower()
    if extension in PNG_EXTENSIONS:
        return _write_png_with_sanitized_metadata(source_path, target_path)
    if extension in VIDEO_EXTENSIONS:
        return _write_video_with_sanitized_metadata(source_path, target_path)
    raise ValueError(f"Unsupported file type: {source_path}")


def sanitize_file_metadata_in_place(path, send_original_to_recycle_bin=False, preserve_times=True):
    extension = Path(path).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {path}")

    original_times = capture_file_times(path) if preserve_times else None
    temp_dir = folder_paths.get_temp_directory()
    os.makedirs(temp_dir, exist_ok=True)
    temp_path = os.path.join(temp_dir, f"{Path(path).stem}.coachbate-scrub{extension}")

    try:
        changed = sanitize_file_metadata_to_path(path, temp_path)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise

    if not changed:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        return False, path

    if send_original_to_recycle_bin:
        send_to_recycle_bin(path)
    shutil.move(temp_path, path)
    if original_times is not None:
        apply_file_times(path, original_times)
    return True, path


def make_safe_copy_path(path):
    candidate = Path(path)
    base = candidate.with_name(f"{candidate.stem}-safe{candidate.suffix}")
    if not base.exists():
        return str(base)
    counter = 2
    while True:
        numbered = candidate.with_name(f"{candidate.stem}-safe-{counter}{candidate.suffix}")
        if not numbered.exists():
            return str(numbered)
        counter += 1


def create_sanitized_copy(path):
    safe_path = make_safe_copy_path(path)
    shutil.copy2(path, safe_path)
    changed, _ = sanitize_file_metadata_in_place(
        safe_path,
        send_original_to_recycle_bin=False,
        preserve_times=True,
    )
    if not changed:
        log.info("[CoachBate] No API key metadata found in %s; safe copy still created.", path)
    return safe_path, changed


def sanitize_video_combine_outputs(output_files, behavior):
    if behavior == SAFE_BEHAVIORS[0]:
        return output_files, None

    if behavior == SAFE_BEHAVIORS[1]:
        # VHS appends the first-frame PNG path to output_files even when
        # the VHS_MetadataImage setting stopped it from being written
        pngs = [p for p in output_files
                if Path(p).suffix.lower() in PNG_EXTENSIONS and Path(p).exists()]
        # Only the last video survives: VHS deletes the pre-mux intermediate once
        # the -audio file is written, so scrubbing earlier entries races that
        # delete and used to abort the loop before the real output was reached.
        videos = [p for p in output_files
                  if Path(p).suffix.lower() in VIDEO_EXTENSIONS and Path(p).exists()]
        for path in pngs + videos[-1:]:
            try:
                sanitize_file_metadata_in_place(
                    path,
                    send_original_to_recycle_bin=False,
                    preserve_times=False,
                )
            except Exception as exc:
                log.warning(
                    "[CoachBate] Could not scrub metadata from %s (%s: %s); "
                    "this file may still contain an API key.",
                    Path(path).name, type(exc).__name__, exc,
                )
        return output_files, None

    final_video = None
    for path in reversed(output_files):
        if Path(path).suffix.lower() in VIDEO_EXTENSIONS and Path(path).exists():
            final_video = path
            break

    if final_video is None:
        return output_files, None

    safe_path, _ = create_sanitized_copy(final_video)
    return output_files + [safe_path], safe_path
