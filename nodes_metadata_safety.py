import copy
import importlib
import logging
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

log = logging.getLogger("CoachBate")

from .metadata_safety import (
    SAFE_BEHAVIORS,
    sanitize_file_metadata_in_place,
    sanitize_prompt_and_pnginfo,
    sanitize_video_combine_outputs,
)
from .nodes import _NodeBase, _V3

_VHS_NODES = None
_DATE_TOKEN_RE = re.compile(r"%date:([^%]+)%")
_VHS_COUNTER_RE = re.compile(r"^(.+?)_(\d+)(-audio)?$")
_COACHBATE_DEFAULT_FORMAT = "video/h265-mp4"
_COACHBATE_DEFAULT_CRF = 16


def _get_vhs_nodes():
    global _VHS_NODES
    if _VHS_NODES is not None:
        return _VHS_NODES

    candidates = (
        "videohelpersuite.nodes",
        "ComfyUI-VideoHelperSuite.videohelpersuite.nodes",
        "comfyui-videohelpersuite.videohelpersuite.nodes",
    )
    for name in candidates:
        try:
            _VHS_NODES = importlib.import_module(name)
            return _VHS_NODES
        except ImportError:
            pass

    custom_nodes_dir = Path(__file__).resolve().parent.parent
    for candidate in sorted(custom_nodes_dir.iterdir()):
        if not candidate.is_dir():
            continue
        if not (candidate / "videohelpersuite" / "nodes.py").exists():
            continue
        candidate_str = str(candidate)
        if candidate_str not in sys.path:
            sys.path.insert(0, candidate_str)
        try:
            _VHS_NODES = importlib.import_module("videohelpersuite.nodes")
            return _VHS_NODES
        except ImportError:
            pass

    for module in sys.modules.values():
        mod_name = getattr(module, "__name__", "") or ""
        if mod_name.endswith("videohelpersuite.nodes") and hasattr(module, "VideoCombine"):
            _VHS_NODES = module
            return _VHS_NODES

    raise ImportError(
        "CoachBate Video Combine requires ComfyUI-VideoHelperSuite. "
        "Install or enable ComfyUI-VideoHelperSuite, then restart ComfyUI."
    )


def _apply_coachbate_format_defaults(spec):
    """Move h265-mp4 to front of the format list and set CRF default to 16 for all formats."""
    fmt_spec = spec.get("required", {}).get("format")
    if not fmt_spec or not isinstance(fmt_spec, (list, tuple)) or len(fmt_spec) < 2:
        return
    options = fmt_spec[0]
    if isinstance(options, list) and _COACHBATE_DEFAULT_FORMAT in options:
        options.remove(_COACHBATE_DEFAULT_FORMAT)
        options.insert(0, _COACHBATE_DEFAULT_FORMAT)
    opts_dict = fmt_spec[1] if isinstance(fmt_spec[1], dict) else {}
    for widgets in opts_dict.get("formats", {}).values():
        if not isinstance(widgets, list):
            continue
        for widget in widgets:
            if (
                isinstance(widget, (list, tuple))
                and len(widget) >= 3
                and widget[0] == "crf"
                and isinstance(widget[2], dict)
            ):
                widget[2]["default"] = _COACHBATE_DEFAULT_CRF


def _clean_output_filenames(output_files, original_prefix):
    """
    Rename VHS output files to remove the auto-counter and the -audio suffix when
    the original prefix did not contain "audio".  The primary output (last file) is
    processed first so it gets priority on the clean name; secondary files fall back
    to keeping their counter if the desired name is already taken.
    """
    if not output_files:
        return output_files
    keep_audio_suffix = "audio" in original_prefix.lower()
    result = list(output_files)
    indices = [len(result) - 1] + list(range(len(result) - 1))
    for i in indices:
        path_str = result[i]
        p = Path(path_str)
        if not p.exists() or p.suffix.lower() == ".png":
            continue
        m = _VHS_COUNTER_RE.match(p.stem)
        if not m:
            continue
        base = m.group(1)
        has_audio_suffix = m.group(3) is not None
        audio_part = "-audio" if (has_audio_suffix and keep_audio_suffix) else ""
        desired = p.parent / f"{base}{audio_part}{p.suffix}"
        if desired == p or desired.exists():
            continue
        try:
            shutil.move(str(p), str(desired))
            result[i] = str(desired)
        except OSError as exc:
            log.warning("[CoachBate] Could not rename %s → %s: %s", p.name, desired.name, exc)
    return result


def _add_api_key_behavior_input(input_spec):
    spec = copy.deepcopy(input_spec)
    _apply_coachbate_format_defaults(spec)
    spec.setdefault("required", {})
    spec["required"]["api_key_behavior"] = (
        list(SAFE_BEHAVIORS),
        {
            "default": SAFE_BEHAVIORS[0],
            "tooltip": "How to handle API keys embedded in saved video metadata: keep them, strip from the saved file, or save a separate clean copy.",
        },
    )
    return spec


def _format_date_token(date_format):
    now = datetime.now()
    replacements = (
        ("yyyy", now.strftime("%Y")),
        ("yy", now.strftime("%y")),
        ("MM", now.strftime("%m")),
        ("dd", now.strftime("%d")),
        ("HH", now.strftime("%H")),
        ("hh", now.strftime("%H")),
        ("mm", now.strftime("%M")),
        ("ss", now.strftime("%S")),
    )
    value = date_format
    for token, replacement in replacements:
        value = value.replace(token, replacement)
    return value


def _apply_filename_prefix_safety(kwargs):
    if "filename_prefix" not in kwargs:
        return kwargs
    prefix = kwargs.get("filename_prefix")
    if not isinstance(prefix, str) or "%date:" not in prefix:
        return kwargs
    updated = dict(kwargs)
    updated["filename_prefix"] = _DATE_TOKEN_RE.sub(
        lambda match: _format_date_token(match.group(1)),
        prefix,
    )
    return updated


def _combine_video_with_api_key_behavior(combine_func, self, args, kwargs):
    behavior = kwargs.pop("api_key_behavior", SAFE_BEHAVIORS[0])
    run_kwargs = _apply_filename_prefix_safety(kwargs)

    # Strip leading underscores from the filename portion of the prefix.
    # Mikey nodes often produce prefixes like "SubFolder/_001_shot"; lstrip on
    # the full string is a no-op because the string starts with the folder name.
    original_prefix = run_kwargs.get("filename_prefix", "")
    if isinstance(original_prefix, str):
        sep_idx = max(original_prefix.rfind("/"), original_prefix.rfind("\\"))
        if sep_idx >= 0:
            folder_part = original_prefix[: sep_idx + 1]
            name_part = original_prefix[sep_idx + 1 :]
            stripped_name = name_part.lstrip("_") or "CoachBate"
            clean_prefix = folder_part + stripped_name
        else:
            clean_prefix = original_prefix.lstrip("_") or "CoachBate"
        if clean_prefix != original_prefix:
            run_kwargs = dict(run_kwargs)
            run_kwargs["filename_prefix"] = clean_prefix
    else:
        clean_prefix = str(original_prefix)

    if behavior == SAFE_BEHAVIORS[1]:
        prompt = run_kwargs.get("prompt")
        extra_pnginfo = run_kwargs.get("extra_pnginfo")
        sanitized_prompt, sanitized_extra, _ = sanitize_prompt_and_pnginfo(prompt, extra_pnginfo)
        run_kwargs["prompt"] = sanitized_prompt
        run_kwargs["extra_pnginfo"] = sanitized_extra

    result = combine_func(self, *args, **run_kwargs)
    if not isinstance(result, dict):
        return result

    result_payload = result.get("result")
    if not result_payload:
        return result

    save_output, output_files = result_payload[0]
    try:
        primary_idx = len(output_files) - 1
        updated_output_files, safe_path = sanitize_video_combine_outputs(output_files, behavior)
        updated_output_files = _clean_output_filenames(updated_output_files, clean_prefix)
        result["result"] = ((save_output, updated_output_files),)

        gifs = result.get("ui", {}).get("gifs")
        if gifs and 0 <= primary_idx < len(updated_output_files):
            gifs[-1]["filename"] = Path(updated_output_files[primary_idx]).name
            gifs[-1]["fullpath"] = updated_output_files[primary_idx]

        if safe_path and gifs:
            safe_entry = updated_output_files[-1] if len(updated_output_files) > len(output_files) else safe_path
            preview = copy.deepcopy(gifs[-1])
            preview["filename"] = Path(safe_entry).name
            preview["fullpath"] = safe_entry
            gifs.append(preview)
    except Exception as exc:
        log.warning(
            "[CoachBate] Video Combine post-processing failed (%s: %s); "
            "returning VHS result unchanged so the video is not lost.",
            type(exc).__name__, exc,
        )

    return result


def patch_vhs_video_combine():
    try:
        vhs_nodes = _get_vhs_nodes()
    except ImportError:
        return False

    cls = vhs_nodes.VideoCombine
    if getattr(cls, "_coachbate_api_key_patch", False):
        return True

    original_input_types = cls.INPUT_TYPES
    original_combine_video = cls.combine_video

    @classmethod
    def patched_input_types(inner_cls):
        return _add_api_key_behavior_input(original_input_types())

    def patched_combine_video(self, *args, **kwargs):
        return _combine_video_with_api_key_behavior(original_combine_video, self, args, kwargs)

    cls.INPUT_TYPES = patched_input_types
    cls.combine_video = patched_combine_video
    cls._coachbate_original_input_types = original_input_types
    cls._coachbate_original_combine_video = original_combine_video
    cls._coachbate_api_key_patch = True
    return True


class CoachBateVideoCombine:
    RETURN_TYPES = ("VHS_FILENAMES",)
    RETURN_NAMES = ("Filenames",)
    OUTPUT_NODE = True
    CATEGORY = "CoachBate"
    FUNCTION = "combine_video"
    DESCRIPTION = (
        "A wrapper around VHS VideoCombine that adds API key safety. "
        "It can strip ltxv_ API keys and other secrets from embedded workflow metadata before saving, "
        "preventing accidental API key exposure when sharing video files. "
        "Requires ComfyUI-VideoHelperSuite."
    )
    RETURN_TOOLTIPS = (
        "Saved video file paths as a VHS_FILENAMES value.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        vhs_nodes = _get_vhs_nodes()
        video_combine = vhs_nodes.VideoCombine
        input_types = getattr(video_combine, "_coachbate_original_input_types", video_combine.INPUT_TYPES)
        return _add_api_key_behavior_input(input_types())

    def combine_video(self, *args, **kwargs):
        vhs_nodes = _get_vhs_nodes()
        video_combine = vhs_nodes.VideoCombine
        combine_func = getattr(
            video_combine,
            "_coachbate_original_combine_video",
            video_combine.combine_video,
        )
        return _combine_video_with_api_key_behavior(
            combine_func,
            video_combine(),
            args,
            kwargs,
        )


class CoachBateStripAPIKeyMetadata(_NodeBase):
    RETURN_TYPES = ("STRING", "BOOLEAN")
    RETURN_NAMES = ("path", "changed")
    FUNCTION = "execute"
    CATEGORY = "CoachBate"
    DESCRIPTION = (
        "Removes API key-like strings (such as ltxv_ secrets) from the embedded workflow metadata of "
        "a saved PNG or video file. The file is sanitized in-place; the original can optionally be moved "
        "to the Recycle Bin before replacement."
    )
    RETURN_TOOLTIPS = (
        "Absolute path to the sanitized output file.",
        "True if any API key strings were found and removed from the metadata.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "file_path": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "placeholder": "X://insert/path/here.png or .mp4",
                    },
                ),
                "send_original_to_recycle_bin": ("BOOLEAN", {"default": True}),
            }
        }

    @classmethod
    def define_schema(cls):
        from comfy_api.latest import io

        return io.Schema(
            node_id="CoachBateStripAPIKeyMetadata",
            display_name="CoachBate Strip API Key Metadata",
            category="CoachBate",
            description=(
                "Removes API key-like strings (such as ltxv_ secrets) from the embedded workflow metadata of "
                "a saved PNG or video file. The file is sanitized in-place; the original can optionally be moved "
                "to the Recycle Bin before replacement."
            ),
            inputs=[
                io.String.Input(
                    "file_path",
                    default="",
                    tooltip="Absolute path to a PNG, MP4, MOV, MKV, or WEBM file, or a folder to process all such files in it.",
                ),
                io.Boolean.Input(
                    "send_original_to_recycle_bin",
                    default=True,
                    tooltip="When replacing a file in place, send the original to the Recycle Bin first for safety.",
                ),
            ],
            outputs=[
                io.String.Output("path"),
                io.Boolean.Output("changed"),
            ],
        )

    _SUPPORTED_EXTENSIONS = {".png", ".mp4", ".mov", ".mkv", ".webm"}

    @classmethod
    def execute(cls, file_path, send_original_to_recycle_bin=True):
        file_path = file_path.strip().strip('"')
        if not file_path:
            raise ValueError("file_path is required.")
        p = Path(file_path)
        if not p.exists():
            raise FileNotFoundError(f"Not found: {file_path}")

        if p.is_dir():
            files = [
                f for f in p.iterdir()
                if f.is_file() and f.suffix.lower() in cls._SUPPORTED_EXTENSIONS
            ]
            if not files:
                return (str(p), False)
            any_changed = False
            last_path = str(p)
            for f in sorted(files):
                changed, final = sanitize_file_metadata_in_place(
                    str(f),
                    send_original_to_recycle_bin=send_original_to_recycle_bin,
                    preserve_times=True,
                )
                any_changed = any_changed or changed
                last_path = final
            return (last_path, any_changed)

        changed, final_path = sanitize_file_metadata_in_place(
            file_path,
            send_original_to_recycle_bin=send_original_to_recycle_bin,
            preserve_times=True,
        )
        return (final_path, changed)

    if not _V3:
        @classmethod
        def VALIDATE_INPUTS(cls, file_path, **kwargs):
            file_path = file_path.strip().strip('"')
            if not file_path:
                return "file_path is required."
            if not Path(file_path).exists():
                return f"Not found: {file_path}"
            return True
