"""
nodes.py — CoachBateShotLoader

Iterates through a shotlist JSON file one shot per queue run, mimicking the
behaviour of the 'JSON Array Iterator' node.

Iteration modes
---------------
  fixed      — always output the shot at `shot_index`; index never advances
  increment  — advance forward one shot each run; wraps at the end
  decrement  — advance backward one shot each run; wraps at the beginning

For automatic looping enable **Auto Queue** in ComfyUI's queue panel (the
drop-down next to the Queue button → "Auto Queue").  The node will stop
advancing and IS_CHANGED will stabilise once every shot has been processed
(for increment mode, once the index wraps back to 0).

JSON file format
----------------
Either a bare array:
    [ { "shot_id": "001", ... }, ... ]

Or wrapped:
    { "shots": [ { "shot_id": "001", ... }, ... ] }

Required fields per shot
------------------------
  shot_id               — identifier string
  video_filename_prefix — output filename prefix
  duration_seconds      — clip length (int)

Optional fields
---------------
  start_image           — path to start-frame image  (default "")
  end_image             — path to end-frame image    (default "")
  start_image_prompt    — text prompt for start frame (default "")
  start_image_strength  — strength for start image (default 1.0 when image exists and file found, else 0.0)
  end_image_strength    — strength for end image   (default 1.0  when image exists and file found, else 0.0)
  end_image_timestamp   — float (or int) seconds at which the end image is placed; if omitted, defaults to
                          duration_seconds − 1; if duration_seconds is also absent, outputs −1.0
  status                — informational only; not returned
  scene                 — informational only; not returned

  video_prompt          — generation prompt string

  OR (for Prompt Relay workflow)

  global_prompt         — scene/project-level context prompt (default "") - <lora:character:0.7> tags here only, not in local_prompts
  local_prompts         — shot-specific pipe-separated sub-prompt segments (default "")


Compatibility
-------------
  V3 (ComfyUI nodes 2.0): class extends io.ComfyNode; uses define_schema() + execute() + fingerprint_inputs().
  V1 (older ComfyUI):      class extends object; uses INPUT_TYPES + FUNCTION="execute" + IS_CHANGED().
  Both paths share the same execute() implementation.
"""

import json
import logging
import os
import re
import sys
import importlib
import threading
import time

# Matches the duration embedded in each pipe-separated local_prompts segment.
# Expected segment opener: "(Shot description, 6s): ..."
_SEG_DURATION_RE = re.compile(r",\s*(\d+(?:\.\d+)?)s\)", re.IGNORECASE)

log = logging.getLogger("coachbate")

# ---------------------------------------------------------------------------
# V3 / V1 compatibility shim
# ---------------------------------------------------------------------------

try:
    from comfy_api.latest import io as _io
    _NodeBase = _io.ComfyNode
    _V3 = True
except ImportError:
    _io = None
    _NodeBase = object
    _V3 = False

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _send_toast(message: str, severity: str, summary: str = "CoachBate", life: int = 4000, kind: str = ""):
    """Push a toast notification to the ComfyUI frontend via WebSocket."""
    try:
        from server import PromptServer
        payload = {"message": message, "severity": severity, "summary": summary, "life": life}
        if kind:
            payload["kind"] = kind
        PromptServer.instance.send_sync("coachbate.toast", payload)
    except Exception as exc:
        log.warning("[CoachBate] Could not send toast: %s", exc)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

# Module-level state avoids writing to locked V3 class attributes.
# _state_lock guards all reads/writes of _shot_loader_state from execute()
# (node thread pool) and the aiohttp route handlers (event-loop thread).
_shot_loader_state: dict = {"stored_index": 0, "_seeded": False}
_state_lock = threading.Lock()

# (BatchPrompter no longer needs internal state — the start_index widget
# itself is the source of truth, and is advanced by the frontend after
# each run.  This makes the workflow hash change between runs, which is
# what ComfyUI's Auto Queue actually requires to re-fire.)


class CoachBateShotLoader(_NodeBase):
    """Iterates through a shotlist JSON file, one shot per queue run."""

    # ── V1 class attributes (ignored by V3, required for V1) ───────────────

    RETURN_TYPES  = ("STRING",       "INT",               "STRING",   "STRING",               "STRING",      "STRING",    "STRING",             "STRING",           "STRING",               "INT",          "FLOAT",                 "FLOAT",              "FLOAT",               "STRING",        "STRING",       "STRING")
    RETURN_NAMES  = ("video_prompt", "duration_seconds",  "shot_id",  "video_filename_prefix", "start_image", "end_image", "start_image_prompt", "negative_prompt",  "negative_audio_prompt", "total_shots",  "start_image_strength",  "end_image_strength", "end_image_timestamp", "global_prompt", "local_prompts", "segment_lengths")
    FUNCTION      = "execute"
    CATEGORY      = "CoachBate"
    OUTPUT_NODE   = True
    DESCRIPTION   = (
        "Iterates through a shotlist JSON file one shot per queue run, outputting all fields of the current shot "
        "as typed values. Supports increment (forward), decrement (backward), and fixed (always same index) modes. "
        "Shots with status set to \"DONE\" in the JSON are automatically skipped. "
        "Use with ComfyUI Auto Queue to process every shot in a production queue without manual intervention."
    )
    RETURN_TOOLTIPS = (
        "Generation prompt text from the shot's video_prompt field.",
        "Clip duration in seconds from the shot's duration_seconds field.",
        "Unique identifier string from the shot's shot_id field.",
        "Output filename prefix for saving, from the shot's video_filename_prefix field.",
        "Absolute path to the start-frame conditioning image. Empty string if not specified.",
        "Absolute path to the end-frame conditioning image. Empty string if not specified.",
        "Text prompt describing the start frame, from the shot's start_image_prompt field.",
        "Negative prompt for video generation, from the shot's negative_prompt field.",
        "Negative prompt for audio generation, from the shot's negative_audio_prompt field.",
        "Total number of shots in the shotlist that are not marked DONE.",
        "Conditioning strength for the start image. Automatically 0.0 if the image file does not exist.",
        "Conditioning strength for the end image. Automatically 0.0 if the image file does not exist.",
        "Timestamp in seconds at which the end image is placed. Defaults to duration−1 if not specified in the JSON.",
        "Scene-level context prompt for Prompt Relay workflows, from the shot's global_prompt field.",
        "Shot-specific pipe-separated sub-prompt segments for Prompt Relay, from the shot's local_prompts field.",
        "Comma-separated frame counts derived from duration tags in the local_prompts segments.",
    )

    if not _V3:
        @classmethod
        def INPUT_TYPES(cls):
            return {
                "required": {
                    "json_path": ("STRING", {
                        "default": "",
                        "multiline": False,
                        "tooltip": "Absolute path to the shotlist JSON file",
                    }),
                    "shot_number": ("INT", {
                        "default": 1,
                        "min": 1,
                        "max": 9999,
                        "step": 1,
                        "tooltip": "Shot number to start from (1-based). In increment mode: seeds the starting position on first run. Updated automatically after each shot.",
                    }),
                    "mode": (["increment", "decrement", "fixed"], {
                        "default": "increment",
                        "tooltip": (
                            "fixed: always output shot_index. "
                            "increment/decrement: advance one shot per run using the stored counter."
                        ),
                    }),
                    "framerate": ("INT", {
                        "default": 24,
                        "min": 1,
                        "max": 120,
                        "step": 1,
                        "tooltip": "Frames per second used to convert segment durations in local_prompts to frame counts.",
                    }),
                },
            }

    # ── V1: IS_CHANGED ─────────────────────────────────────────────────────

    if not _V3:
        @classmethod
        def IS_CHANGED(cls, mode, **kwargs):
            if mode != "fixed":
                return time.time()
            return 0

    # ── V3: define_schema ──────────────────────────────────────────────────

    @classmethod
    def define_schema(cls):
        from comfy_api.latest import io
        return io.Schema(
            node_id="CoachBateShotLoader",
            display_name="CoachBate Shot Loader",
            category="CoachBate",
            description=(
                "Iterates through a shotlist JSON file one shot per queue run, outputting all fields of the current "
                "shot as typed values. Supports increment (forward), decrement (backward), and fixed (always same "
                "index) modes. Shots with status set to \"DONE\" in the JSON are automatically skipped. "
                "Use with ComfyUI Auto Queue to process every shot in a production queue without manual intervention."
            ),
            inputs=[
                io.String.Input(
                    "json_path",
                    default="",
                    tooltip="Absolute path to the shotlist JSON file",
                ),
                io.Int.Input(
                    "shot_number",
                    default=1,
                    min=1,
                    max=9999,
                    step=1,
                    tooltip=(
                        "Shot number to start from (1-based). In increment mode: seeds the starting position on first run. "
                        "Updated automatically after each shot."
                    ),
                ),
                io.Combo.Input(
                    "mode",
                    options=["increment", "decrement", "fixed"],
                    default="increment",
                    tooltip=(
                        "fixed: always output shot_index. "
                        "increment/decrement: advance one shot per run using the stored counter."
                    ),
                ),
                io.Int.Input(
                    "framerate",
                    default=24,
                    min=1,
                    max=120,
                    step=1,
                    tooltip="Frames per second used to convert segment durations in local_prompts to frame counts.",
                ),
            ],
            outputs=[
                io.String.Output("video_prompt"),
                io.Int.Output("duration_seconds"),
                io.String.Output("shot_id"),
                io.String.Output("video_filename_prefix"),
                io.String.Output("start_image"),
                io.String.Output("end_image"),
                io.String.Output("start_image_prompt"),
                io.String.Output("negative_prompt"),
                io.String.Output("negative_audio_prompt"),
                io.Int.Output("total_shots"),
                io.Float.Output("start_image_strength"),
                io.Float.Output("end_image_strength"),
                io.Float.Output("end_image_timestamp"),
                io.String.Output("global_prompt"),
                io.String.Output("local_prompts"),
                io.String.Output("segment_lengths"),
            ],
            is_output_node=True,
        )

    # ── V3: fingerprint_inputs ─────────────────────────────────────────────

    @classmethod
    def fingerprint_inputs(cls, mode, **kwargs):
        if mode != "fixed":
            return time.time()
        return 0

    # ── Execute (shared by both V1 via FUNCTION and V3) ───────────────────

    @classmethod
    def execute(cls, json_path: str, shot_number: int, mode: str, framerate: int = 24):
        shot_index = max(0, shot_number - 1)  # widget is 1-based; clamp so old saved value of 0 → index 0

        # JSON is read fresh from disk every cycle so edits take effect immediately.
        shots, total = cls._load_shots(json_path)

        # --- build list of non-DONE indices (re-evaluated each cycle from fresh JSON) ---
        active = [i for i, s in enumerate(shots)
                  if str(s.get("status", "")).upper() != "DONE"]

        if not active:
            raise ValueError("[CoachBate] All shots have status DONE — nothing left to process.")

        # --- resolve index, skipping DONE shots ---
        with _state_lock:
            if mode == "fixed":
                start = shot_index % total
                candidates = [i for i in active if i >= start]
                idx = candidates[0] if candidates else active[0]

            elif mode == "increment":
                # stored_index is the authoritative counter.  It is only seeded from
                # the widget on the very first run (before _seeded is True); after
                # that the widget is display-only and the "Restart at index" button
                # is the explicit way for the user to change the starting position.
                if not _shot_loader_state["_seeded"]:
                    _shot_loader_state["stored_index"] = shot_index
                _shot_loader_state["_seeded"] = True
                start = _shot_loader_state["stored_index"] % total
                candidates = [i for i in active if i >= start]
                idx = candidates[0] if candidates else active[0]

            else:  # decrement
                start = _shot_loader_state["stored_index"] % total
                candidates = [i for i in active if i <= start]
                idx = candidates[-1] if candidates else active[-1]

            # --- check for missing images: toast, advance stored_index, abort this run ---
            _shot_pre = shots[idx]
            _name_pre = str(_shot_pre.get("video_filename_prefix", _shot_pre.get("shot_id", idx)))
            missing = [
                (key, str(_shot_pre.get(key, "")).strip())
                for key in ("start_image", "end_image")
                if str(_shot_pre.get(key, "")).strip()
                and not os.path.exists(str(_shot_pre.get(key, "")).strip())
            ]
            if missing:
                if mode == "increment":
                    _shot_loader_state["stored_index"] = (idx + 1) % total
                elif mode == "decrement":
                    _shot_loader_state["stored_index"] = (idx - 1) % total

            # --- advance stored_index for a valid shot ---
            elif mode == "increment":
                _shot_loader_state["stored_index"] = (idx + 1) % total
            elif mode == "decrement":
                _shot_loader_state["stored_index"] = (idx - 1) % total

        if missing:
            for key, path in missing:
                msg = f"Skipping '{_name_pre}': {key} not found: {path}"
                _send_toast(message=msg, severity="warn", life=8000)
                log.warning("[CoachBate] %s", msg)
            import comfy.model_management
            raise comfy.model_management.InterruptProcessingException()

        shot         = shots[idx]
        is_last      = (idx == active[-1]) if mode != "decrement" else (idx == active[0])
        active_pos   = active.index(idx) + 1
        active_total = len(active)

        # --- extract fields ---
        video_prompt          = str(shot.get("video_prompt", ""))
        shot_id               = str(shot.get("shot_id", str(idx)))
        video_filename_prefix = str(shot.get("video_filename_prefix", shot_id))
        start_image           = str(shot.get("start_image", "")).strip()
        end_image             = str(shot.get("end_image", "")).strip()
        start_image_prompt    = str(shot.get("start_image_prompt", ""))
        negative_prompt       = str(shot.get("negative_prompt", ""))
        negative_audio_prompt = str(shot.get("negative_audio_prompt", ""))
        global_prompt         = str(shot.get("global_prompt", ""))
        local_prompts         = str(shot.get("local_prompts", ""))
        segment_lengths       = cls._compute_segment_lengths(local_prompts, framerate)
        raw_duration          = shot.get("duration_seconds")
        duration_seconds      = cls._parse_duration(raw_duration if raw_duration is not None else 4)

        raw_eit = shot.get("end_image_timestamp")
        if raw_eit is not None:
            try:
                end_image_timestamp = float(raw_eit)
            except (TypeError, ValueError):
                log.warning("[CoachBate] Invalid end_image_timestamp '%s', falling back to duration−1", raw_eit)
                end_image_timestamp = float(duration_seconds - 1) if raw_duration is not None else -1.0
        elif raw_duration is not None:
            end_image_timestamp = float(duration_seconds - 1)
        else:
            end_image_timestamp = -1.0

        start_image_strength = cls._resolve_image_strength(start_image, shot.get("start_image_strength"), 1.0)
        end_image_strength   = cls._resolve_image_strength(end_image,   shot.get("end_image_strength"), 1.0)

        # --- status toast ---
        severity = "error" if is_last else "info"
        suffix   = " — last shot!" if is_last else ""
        _send_toast(
            message=f"Shot {active_pos}/{active_total}: {video_filename_prefix}{suffix}",
            severity=severity,
            life=0,
            kind="status",
        )

        log.info(
            "[CoachBate] mode=%s  shot %d/%d (array idx %d, %d DONE skipped)  %s%s",
            mode, active_pos, active_total, idx, total - active_total, video_filename_prefix, suffix,
        )

        # --- node display ---
        if is_last:
            status_line = "[last]"
        else:
            pos = active.index(idx)
            next_pos = pos - 1 if mode == "decrement" else pos + 1
            next_idx  = active[next_pos]
            next_name = str(shots[next_idx].get("video_filename_prefix",
                            shots[next_idx].get("shot_id", next_idx)))
            status_line = f"➡️ {next_name}"

        display_text = (
            f"{active_pos}/{active_total}  {video_filename_prefix}\n"
            f"{duration_seconds}s  {status_line}\n"
            f"stored_idx: {_shot_loader_state['stored_index']}"
        )

        outputs = (
            video_prompt,
            duration_seconds,
            shot_id,
            video_filename_prefix,
            start_image,
            end_image,
            start_image_prompt,
            negative_prompt,
            negative_audio_prompt,
            active_total,
            start_image_strength,
            end_image_strength,
            end_image_timestamp,
            global_prompt,
            local_prompts,
            segment_lengths,
        )

        ui_data = {
            "text":      [display_text],
            "array_idx": [idx],
            "total":     [total],
            "is_last":   [is_last],
        }

        if _V3:
            from comfy_api.latest import io
            return io.NodeOutput(*outputs, ui=ui_data)
        else:
            return {"ui": ui_data, "result": outputs}

    # ── Private helpers ────────────────────────────────────────────────────

    @staticmethod
    def _load_shots(json_path: str):
        """Load the shotlist JSON. Accepts bare array or {"shots":[...]} wrapper."""
        json_path = json_path.strip('"').strip()
        if not json_path:
            raise ValueError("[CoachBate] json_path is empty — set the path to your shotlist JSON file")

        if not os.path.isabs(json_path):
            json_path = os.path.abspath(json_path)

        if not os.path.isfile(json_path):
            raise ValueError(f"[CoachBate] Shotlist file not found: {json_path}")

        try:
            with open(json_path, "r", encoding="utf-8") as fh:
                raw = fh.read()
            log.debug("[CoachBate] JSON read fresh from disk: %s", json_path)
        except OSError as exc:
            raise ValueError(f"[CoachBate] Cannot read shotlist file: {exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"[CoachBate] Invalid JSON in {json_path}: {exc}") from exc

        if isinstance(data, list):
            shots = data
        elif isinstance(data, dict):
            shots = data.get("shots") or data.get("shot_list") or data.get("items")
        else:
            shots = None

        if not isinstance(shots, list) or len(shots) == 0:
            raise ValueError(
                f"[CoachBate] Shotlist must be a non-empty JSON array, or an object with a "
                f"'shots' key containing one: {json_path}"
            )

        bad = [i for i, s in enumerate(shots) if not isinstance(s, dict)]
        if bad:
            raise ValueError(
                f"[CoachBate] Shotlist entries must be objects — found non-object at index(es): {bad}"
            )

        return shots, len(shots)

    @staticmethod
    def _resolve_image_strength(image_path: str, json_value, default: float = 1.0) -> float:
        """Return the effective image strength.

        - No path (empty/missing) → 0.0
        - Path set but file not found → 0.0
        - Path set and file exists → json_value if provided, else 1.0
        """
        path = image_path.strip()
        if not path or not os.path.exists(path):
            return 0.0
        if json_value is None:
            return default
        try:
            return float(json_value)
        except (TypeError, ValueError):
            log.warning("[CoachBate] Invalid image strength value '%s', defaulting to %s", json_value, default)
            return default

    @staticmethod
    def _compute_segment_lengths(local_prompts: str, framerate: int) -> str:
        """Return a comma-separated string of frame counts from pipe-separated local_prompts.

        Each segment is expected to start with "(Description, Xs): ..." where X is
        the duration in seconds.  Returns "" if local_prompts is empty, or if any
        segment's duration cannot be parsed (so the caller can fall back to even spacing).
        """
        if not local_prompts.strip():
            return ""
        segments = local_prompts.split("|")
        lengths = []
        for seg in segments:
            m = _SEG_DURATION_RE.search(seg)
            if not m:
                log.warning("[CoachBate] Could not parse duration from local_prompts segment: %.80s…", seg)
                return ""
            raw = float(m.group(1)) * framerate
            # Snap to nearest value of the form 8n+1 (required by most video-gen models)
            frames = round((raw - 1) / 8) * 8 + 1
            lengths.append(str(frames))
        return ",".join(lengths)

    @staticmethod
    def _parse_duration(raw) -> int:
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            log.warning("[CoachBate] Invalid duration_seconds '%s', defaulting to 4", raw)
            return 4


# ---------------------------------------------------------------------------
# CoachBateLoadVideosWithAudio
# ---------------------------------------------------------------------------

_VIDEO_EXTENSIONS = {"webm", "mp4", "mkv", "gif", "mov", "avi"}


def _get_vhs_module():
    """Return the VHS videohelpersuite package, searching sys.modules for Windows compatibility."""
    for name in (
        "ComfyUI-VideoHelperSuite.videohelpersuite",
        "comfyui-videohelpersuite.videohelpersuite",
    ):
        try:
            return importlib.import_module(name)
        except ImportError:
            pass

    for module in sys.modules.values():
        mod_name = getattr(module, "__name__", "") or ""
        if mod_name.endswith("videohelpersuite") and hasattr(module, "load_video_nodes"):
            return module

    raise ImportError(
        "CoachBateLoadVideosWithAudio requires ComfyUI-VideoHelperSuite to be installed."
    )


class CoachBateLoadVideosWithAudio:
    """Load all videos from a folder and return concatenated frames plus combined audio."""

    RETURN_TYPES = ("IMAGE", "AUDIO")
    RETURN_NAMES = ("IMAGE", "AUDIO")
    FUNCTION = "load_videos_with_audio"
    CATEGORY = "CoachBate"
    DESCRIPTION = (
        "Loads all video files from a folder, concatenates their frames into a single image batch, "
        "and merges their audio tracks into a single combined audio output with a consistent sample rate and channel count. "
        "Videos are processed in alphabetical order. Requires ComfyUI-VideoHelperSuite."
    )
    RETURN_TOOLTIPS = (
        "All video frames concatenated in sequence as an image batch (N, H, W, 3).",
        "Combined audio from all videos, resampled to the highest sample rate found across clips.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "folder": ("STRING", {
                    "default": "X://insert/path/",
                    "tooltip": "Absolute path to the folder containing video files (webm, mp4, mkv, gif, mov, avi).",
                }),
                "force_rate": ("FLOAT", {
                    "default": 0, "min": 0, "max": 60, "step": 1, "disable": 0,
                    "tooltip": "Override framerate for all loaded videos. Set to 0 to use each video's source FPS.",
                }),
                "custom_width": ("INT", {
                    "default": 0, "min": 0, "max": 4096, "disable": 0,
                    "tooltip": "Resize each video frame to this width in pixels. Set to 0 to use source width.",
                }),
                "custom_height": ("INT", {
                    "default": 0, "min": 0, "max": 4096, "disable": 0,
                    "tooltip": "Resize each video frame to this height in pixels. Set to 0 to use source height.",
                }),
                "frame_load_cap": ("INT", {
                    "default": 0, "min": 0, "max": 10000, "step": 1, "disable": 0,
                    "tooltip": "Maximum number of frames to load per video. Set to 0 to load all frames.",
                }),
                "skip_first_frames": ("INT", {
                    "default": 0, "min": 0, "max": 10000, "step": 1,
                    "tooltip": "Number of frames to skip at the start of each video.",
                }),
                "select_every_nth": ("INT", {
                    "default": 1, "min": 1, "max": 1000, "step": 1,
                    "tooltip": "Load every Nth frame only. 1 loads all frames, 2 loads every other frame, etc.",
                }),
            }
        }

    @classmethod
    def IS_CHANGED(cls, folder, **kwargs):
        import hashlib
        h = hashlib.md5(usedforsecurity=False)
        if os.path.isdir(folder):
            for f in sorted(os.listdir(folder)):
                h.update(f.encode())
        return h.hexdigest()

    def load_videos_with_audio(self, folder, force_rate, custom_width, custom_height,
                                frame_load_cap, skip_first_frames, select_every_nth):
        import torch

        if not os.path.isdir(folder):
            raise ValueError(f"[CoachBate] Folder not found: {folder}")

        videos_list = [
            os.path.join(folder, f)
            for f in sorted(os.listdir(folder))
            if os.path.isfile(os.path.join(folder, f))
            and "." in f
            and f.rsplit(".", 1)[-1].lower() in _VIDEO_EXTENSIONS
        ]

        if not videos_list:
            raise ValueError(f"[CoachBate] No video files found in: {folder}")

        vhs_load = _get_vhs_module().load_video_nodes.load_video
        vhs_kwargs = dict(
            force_rate=force_rate,
            custom_width=custom_width,
            custom_height=custom_height,
            frame_load_cap=frame_load_cap,
            skip_first_frames=skip_first_frames,
            select_every_nth=select_every_nth,
        )

        # Pass 1: load frames + raw audio; track best quality seen
        loaded_frames = []
        raw_clips = []  # (waveform_or_None, sr_or_None, frame_count, fps)
        best_sr = 0
        best_ch = 0

        for video_path in videos_list:
            images, _count, audio, info = vhs_load(video=video_path, **vhs_kwargs)
            loaded_frames.append(images)

            raw_fps = (info or {}).get("source_fps") or (info or {}).get("fps") or 24
            fps = float(force_rate) if force_rate > 0 else float(raw_fps)

            try:
                waveform = audio["waveform"]  # shape: (1, channels, samples)
                sr = int(audio["sample_rate"])
                best_sr = max(best_sr, sr)
                best_ch = max(best_ch, waveform.shape[1])
                raw_clips.append((waveform, sr, images.shape[0], fps))
            except Exception as e:
                log.info("[CoachBate] No audio in %s: %s", video_path, e)
                raw_clips.append((None, None, images.shape[0], fps))

        # Best quality found wins; fall back to stereo 48 kHz
        target_sr = best_sr if best_sr > 0 else 48000
        target_ch = max(best_ch, 2)  # mono upgrades to stereo; 5.1 etc. stays as-is

        # Pass 2: build time-aligned audio clips
        audio_clips = []
        for waveform, sr, frame_count, fps in raw_clips:
            if waveform is None:
                silent_samples = max(1, round(frame_count / fps * target_sr))
                audio_clips.append(torch.zeros(1, target_ch, silent_samples))
            else:
                if sr != target_sr:
                    import torchaudio
                    waveform = torchaudio.functional.resample(waveform, sr, target_sr)
                if waveform.shape[1] < target_ch:
                    if waveform.shape[1] == 1:
                        # Mono: replicate across all channels
                        waveform = waveform.expand(-1, target_ch, -1).contiguous()
                    else:
                        # Multi-ch but fewer than target: zero-pad missing channels
                        pad = torch.zeros(1, target_ch - waveform.shape[1], waveform.shape[2],
                                         dtype=waveform.dtype, device=waveform.device)
                        waveform = torch.cat([waveform, pad], dim=1)
                audio_clips.append(waveform)

        combined_frames = torch.cat(loaded_frames, dim=0)
        audio_out = {
            "waveform": torch.cat(audio_clips, dim=2),
            "sample_rate": target_sr,
        }

        return (combined_frames, audio_out)


# ---------------------------------------------------------------------------
# CoachBateBatchPrompter
# ---------------------------------------------------------------------------

class CoachBateBatchPrompter:
    """
    Outputs one prompt per queue run, iterating through all non-blank lines in
    multiline_text.

    How it works
    ------------
    Each run reads `start_index` from the widget, finds the first non-blank line
    at or after that position, and emits it as `prompt`.  The `ui` payload
    includes `next_start_index` (the line index of the *next* non-blank prompt);
    the frontend reads that value and writes it back into the start_index widget,
    which changes the workflow hash and causes ComfyUI's Auto Queue to re-fire
    with the advanced position.

    When the last prompt has been processed, the node sends a `coachbate.batch_done`
    event so the frontend can clear any pending queue items (preventing the runaway
    "dozens of failing jobs" behaviour seen with Run-on-Change).

    To restart: set start_index back to 0 (or any earlier line) and click Queue.
    """

    RETURN_TYPES  = ("STRING",)
    RETURN_NAMES  = ("prompt",)
    FUNCTION      = "execute"
    CATEGORY      = "CoachBate"
    OUTPUT_NODE   = True
    DESCRIPTION   = (
        "Outputs one prompt per queue run, iterating through all non-blank lines in a multiline text block. "
        "The starting position advances automatically after each run via the frontend; set it back to 1 to restart. "
        "Use with ComfyUI Auto Queue to generate one image per prompt line without manual re-queuing."
    )
    RETURN_TOOLTIPS = (
        "The current prompt line with prepend_text and append_text applied.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prepend_text": ("STRING", {
                    "multiline": False,
                    "default": "",
                    "tooltip": "Text prepended to every prompt before it is output.",
                }),
                "multiline_text": ("STRING", {
                    "multiline": True,
                    "default": "a beautiful sunset over the ocean\n\na mountain landscape at dawn\n\na city skyline at night",
                    "tooltip": "One prompt per line.  Blank lines are ignored.",
                }),
                "append_text": ("STRING", {
                    "multiline": False,
                    "default": "",
                    "tooltip": "Text appended to every prompt before it is output.",
                }),
                "starting_number": ("INT", {
                    "default": 1,
                    "min": 1,
                    "max": 9999,
                    "tooltip": (
                        "Prompt number (1-based) to start from, matching the gutter "
                        "numbers — blank lines don't count.  Advanced automatically "
                        "during a run; restored to its original value when the batch "
                        "finishes."
                    ),
                }),
                "max_prompts": ("INT", {
                    "default": 1000,
                    "min": 1,
                    "max": 9999,
                    "tooltip": "Maximum number of prompts to run. Blank lines don't count.",
                }),
                "queue_all_at_once": ("BOOLEAN", {
                    "default": True,
                    "tooltip": (
                        "ON — Queue All At Once (default):\n"
                        "Pressing Queue immediately posts every prompt as a separate job into "
                        "ComfyUI's queue. All jobs are visible in the queue panel up front and "
                        "run back-to-back without any delay between them. Fast, but you won't "
                        "see individual prompts appear in real time — they all land in the queue "
                        "before the first one even starts.\n\n"
                        "OFF — One At A Time (sequential):\n"
                        "Works exactly like the Shot Loader node. Pressing Queue runs a single "
                        "prompt, then waits for it to fully finish before firing the next one. "
                        "You see each result appear before the next job starts. Because there is "
                        "a gap between jobs, you can tweak the workflow — change a sampler "
                        "setting, swap a LoRA, adjust a prompt — and the change will be picked "
                        "up by the next job. To restart from the beginning, set Starting Number "
                        "back to 1. The Stop button halts the sequence at any point."
                    ),
                }),
            },
            "optional": {
                "job_index": ("INT", {
                    "default": 0,
                    "min": 0,
                    "max": 9999,
                    "tooltip": "Set automatically by Queue All Prompts. 1-based position of this job in the bulk batch.",
                }),
                "job_total": ("INT", {
                    "default": 0,
                    "min": 0,
                    "max": 9999,
                    "tooltip": "Set automatically by Queue All Prompts. Total number of jobs in the bulk batch.",
                }),
                # ⚠️ New widgets must be APPENDED here, never inserted earlier
                # in the widget order: saved workflows restore widget values
                # positionally, so an insertion shifts every later value by one
                # slot. randomize briefly lived after queue_all_at_once and
                # corrupted job_index/job_total on previously saved workflows
                # ("couldn't convert job_total to the expected type: INT").
                "randomize": ("BOOLEAN", {
                    "default": False,
                    "tooltip": (
                        "Execute the prompts in random order — never repeating — until "
                        "max_prompts is reached. Reshuffled on every Run press. With "
                        "max_prompts = 1 this runs a single randomly chosen prompt, "
                        "handy for injecting one random prompt into a workflow. "
                        "Prompts before starting_number are excluded from the pool."
                    ),
                }),
            },
        }

    @classmethod
    def IS_CHANGED(cls, multiline_text, starting_number, **kwargs):
        # Force re-execution so cached output never short-circuits the batch.
        return time.time()

    def execute(
        self,
        multiline_text: str,
        prepend_text: str = "",
        append_text: str = "",
        starting_number: int = 1,
        max_prompts: int = 1000,
        queue_all_at_once: bool = True,
        randomize: bool = False,
        job_index: int = 0,
        job_total: int = 0,
    ):
        # randomize is consumed entirely by the frontend (it shuffles the
        # ordinals it queues / steps through); Python always executes the
        # single prompt at starting_number.
        lines = multiline_text.split('\n')

        # starting_number is a 1-based PROMPT ordinal — blank lines do NOT
        # count, matching the gutter numbering the user sees on the node.
        # (It used to be a raw line index, so with blank lines in the text the
        # widget number disagreed with the gutter and could exceed the prompt
        # count.) Find the line index of the starting_number-th non-blank line.
        cur_ordinal   = 0
        clamped_start = len(lines)   # past the end → "batch finished" branch
        for idx, ln in enumerate(lines):
            if ln.strip():
                cur_ordinal += 1
                if cur_ordinal >= max(1, starting_number):
                    clamped_start = idx
                    break

        # Advance window_end until we have covered max_prompts non-blank lines.
        # Blank lines are skipped and do NOT count toward the limit.
        window_end     = clamped_start
        prompt_count   = 0
        while window_end < len(lines) and prompt_count < max_prompts:
            if lines[window_end].strip():
                prompt_count += 1
            window_end += 1

        # Each non-blank line is one prompt block (one image per job).
        # Each entry: (line_idx, prompt_body, next_non_blank_line_idx)
        blocks: list[tuple[int, str, int]] = []
        i = clamped_start
        while i < window_end:
            if not lines[i].strip():
                i += 1
                continue
            body = lines[i].strip()
            # next non-blank line (for next_start_index in legacy auto-queue mode)
            next_block = i + 1
            while next_block < len(lines) and not lines[next_block].strip():
                next_block += 1
            blocks.append((i, body, next_block))
            i += 1

        # ── Batch finished ───────────────────────────────────────────────────
        if not blocks:
            _send_toast(
                message="All prompts have been processed.",
                severity="info",
                summary="CoachBate Batch Prompter",
                life=5000,
            )
            log.info("[CoachBate] BatchPrompter: all prompts done (starting_number=%d)", starting_number)

            # Signal the frontend so it can clear the queue / stop Auto Queue.
            try:
                from server import PromptServer
                PromptServer.instance.send_sync("coachbate.batch_done", {})
            except Exception as exc:
                log.warning("[CoachBate] BatchPrompter: failed to send done event: %s", exc)

            # Halt this run instead of returning "" — an empty-string prompt
            # flowing into downstream nodes (image loaders etc.) raises an
            # error there. InterruptProcessingException stops the graph the
            # same way the Interrupt button does: quietly, no error dialog.
            try:
                from comfy.model_management import InterruptProcessingException
                raise InterruptProcessingException()
            except ImportError:
                pass

            return {
                "ui": {
                    "text":      ["Done — all prompts processed"],
                    "remaining": [0],
                    "is_last":   [True],
                    "done":      [True],
                },
                "result": ("",),
            }

        # ── Emit the current prompt block and advance for the next run ───────
        current_line_idx, current_body, next_block_idx = blocks[0]
        remaining_count = len(blocks) - 1
        is_last         = remaining_count == 0

        # Whether any prompt exists after this one in the WHOLE text —
        # distinct from is_last, which only says the max_prompts window is
        # exhausted. next_block_idx already points at the next non-blank line
        # (or len(lines) if none), regardless of the window.
        has_more = next_block_idx < len(lines)

        current_prompt_out = prepend_text + current_body + append_text

        if job_total > 0:
            toast_msg = f"Prompt {job_index} of {job_total}: {current_body[:60]}"
        else:
            toast_msg = f"{current_body[:80]}" + (f" — {remaining_count} remaining" if remaining_count > 0 else "")
        _send_toast(
            message=toast_msg,
            severity="info",
            summary="CoachBate Batch Prompter",
            life=3000,
        )

        log.info(
            "[CoachBate] BatchPrompter: line %d  remaining=%d  prompt=%.80s",
            current_line_idx, remaining_count, current_body,
        )

        display_text = (
            f"Prompt {cur_ordinal}: {current_body[:55]}\n"
            f"{remaining_count} prompt{'s' if remaining_count != 1 else ''} remaining"
        )

        return {
            "ui": {
                "text":             [display_text],
                "remaining":        [remaining_count],
                "is_last":          [is_last],
                "has_more":         [has_more],
                # Frontend writes this back into the starting_number widget so
                # ComfyUI's Auto Queue / Run-on-Change sees a workflow change.
                # Prompt ordinal (1-based, blanks don't count), same units as
                # the widget and the gutter numbering.
                "next_start_index": [cur_ordinal + 1],
            },
            "result": (current_prompt_out,),
        }
