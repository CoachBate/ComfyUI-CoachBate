"""
comfyui-coachbate
=================
A ComfyUI custom node package for shot-by-shot batch video production with LTX-2.3.

The single node, CoachBateShotLoader, reads one shot per run from a pre-authored shotlist.json,
returns the typed outputs the workflow needs, and optionally auto-queues the next shot.

This package is also designed as a clean starter template for ComfyUI custom node development.
See CONTRIBUTING.md for a guide to adding new nodes.

To add more nodes:
  1. Define your class in nodes.py (or a new module)
  2. Import it here
  3. Add it to NODE_CLASS_MAPPINGS and NODE_DISPLAY_NAME_MAPPINGS below
"""

import logging
from pathlib import Path

from .nodes import CoachBateShotLoader, CoachBateLoadVideosWithAudio, CoachBateBatchPrompter
from .nodes_h3_audio_path import CoachBateLoadH3AudioPath
from .nodes_h3_video_path import CoachBateLoadH3VideoPath, CoachBateShotAudioTail
from .nodes_shot_graph import CoachBateShotGraph
from .nodes_audio_schedule import CoachBateAudioSchedule
from .nodes_file_exists import CoachBateFileExists
from .nodes_lyrics_json import CoachBateLyricsJSONParser
from .nodes_metadata_safety import CoachBateStripAPIKeyMetadata, CoachBateVideoCombine, patch_vhs_video_combine
from .nodes_gemma_env_key import patch_gemma_env_key
from .nodes_whisper_dtype import patch_whisper_dtype
from .nodes_vhs_preview_loop import patch_vhs_preview_loop
from .nodes_h3_facerefine_patches import patch_h3_facerefine
from .nodes_numbered_text import CoachBateNumberedText
from .nodes_text_preview_edit import CoachBateTextPreviewEdit
from .nodes_tensorrt_video import CoachBateUpscaleRifeTensorRT
from .nodes_h3_segm_mask import CoachBateH3SegmMask, CoachBateH3SubjectCount, CoachBateH3LoopSubject
from .nodes_h3_subject_refine import (CoachBateH3SubjectTrack, CoachBateH3InjectVideoLatent,
                                     CoachBateH3PerSubjectDenoise, CoachBateH3SubjectStitch)
from .nodes_video_prompt import CoachBateVideoPromptFromMetadata
from .monkeypatch_settings import load_monkeypatch_settings
from . import routes  # registers /coachbate/* API endpoints

log = logging.getLogger("coachbate")
_monkeypatch_settings = load_monkeypatch_settings(str(Path(__file__).parent / "monkeypatch_settings.json"))

if _monkeypatch_settings["vhs_video_combine"]:
    _vhs_patch_available = patch_vhs_video_combine()
    if not _vhs_patch_available:
        log.warning(
            "[CoachBate] VideoHelperSuite was not available, so the VHS_VideoCombine API-key patch was skipped. "
            "Install ComfyUI-VideoHelperSuite to use that integration."
        )
else:
    log.info("[CoachBate] VHS_VideoCombine API-key patch disabled in global settings.")

# Usually defers to a PromptServer startup hook: custom_nodes load
# alphabetically, so LTXVideo's GemmaAPITextEncode does not exist yet at this
# point. The hook logs its own warning if the class never turns up.
#
# Wrapped defensively: this is an optional convenience, and it must NEVER be
# able to stop the pack from loading. It already did once -- a getattr scan
# hit torch.classes, which raises RuntimeError for unknown attributes, and
# the whole pack failed to import.
if _monkeypatch_settings["gemma_env_key"]:
    try:
        _gemma_patch_available = patch_gemma_env_key()
    except Exception as exc:
        _gemma_patch_available = False
        log.warning("[CoachBate] Gemma env-key patch raised %s: %s", type(exc).__name__, exc)
    if not _gemma_patch_available:
        log.warning(
            "[CoachBate] Could not apply or schedule the GemmaAPITextEncode LTXV_API_KEY "
            "environment-variable fallback."
        )
else:
    log.info("[CoachBate] GemmaAPITextEncode environment-key patch disabled in global settings.")

# Same deferred-patch story as the Gemma one above: comfyui-vrgamedevgirl
# loads after us, so this normally lands via the startup hook. Absent that
# pack the patch is simply inactive, which is not an error.
if _monkeypatch_settings["whisper_dtype"]:
    try:
        _whisper_dtype_patch_available = patch_whisper_dtype()
    except Exception as exc:
        _whisper_dtype_patch_available = False
        log.warning("[CoachBate] Whisper dtype patch raised %s: %s", type(exc).__name__, exc)
else:
    log.info("[CoachBate] VRGDG Whisper dtype patch disabled in global settings.")

# Not deferred, unlike the two above: this one installs an aiohttp middleware
# on PromptServer's app, which must happen before the server starts and the
# middleware list is frozen. It does not care whether VHS has loaded yet --
# the module is looked up lazily, per request.
if _monkeypatch_settings["vhs_preview_loop"]:
    try:
        _vhs_preview_loop_patch_available = patch_vhs_preview_loop()
    except Exception as exc:
        _vhs_preview_loop_patch_available = False
        log.warning("[CoachBate] VHS preview loop patch raised %s: %s", type(exc).__name__, exc)
    if not _vhs_preview_loop_patch_available:
        log.warning(
            "[CoachBate] Could not install the VHS preview fallback for event loops "
            "without subprocess support."
        )
else:
    log.info("[CoachBate] VHS preview event-loop fallback disabled in global settings.")

# Same deferred pattern as the Gemma patch: ComfyUI-H3-FaceRefine loads after us.
# Detector predictor reset, grid padding before encode, crop_size_from option.
if _monkeypatch_settings["h3_facerefine"]:
    try:
        _h3_patch_available = patch_h3_facerefine()
    except Exception as exc:
        _h3_patch_available = False
        log.warning("[CoachBate] H3-FaceRefine patch raised %s: %s", type(exc).__name__, exc)
    if not _h3_patch_available:
        log.warning("[CoachBate] Could not apply or schedule the ComfyUI-H3-FaceRefine patches.")
else:
    log.info("[CoachBate] ComfyUI-H3-FaceRefine patches disabled in global settings.")

from .nodes_shot_continuity import (CoachBatePrepareShotContinuity,
                                    CoachBateTrimShotOutput, CoachBateRecordShotOutput,
                                    CoachBateShotKeyframes)

NODE_CLASS_MAPPINGS = {
    "CoachBatePrepareShotContinuity": CoachBatePrepareShotContinuity,
    "CoachBateTrimShotOutput": CoachBateTrimShotOutput,
    "CoachBateRecordShotOutput": CoachBateRecordShotOutput,
    "CoachBateShotKeyframes": CoachBateShotKeyframes,
    "CoachBateShotLoader":            CoachBateShotLoader,
    "CoachBateLoadH3AudioPath":       CoachBateLoadH3AudioPath,
    "CoachBateLoadH3VideoPath":       CoachBateLoadH3VideoPath,
    "CoachBateShotAudioTail":         CoachBateShotAudioTail,
    "CoachBateShotGraph":             CoachBateShotGraph,
    "CoachBateLoadVideosWithAudio":   CoachBateLoadVideosWithAudio,
    "CoachBateBatchPrompter":         CoachBateBatchPrompter,
    "CoachBateLyricsJSONParser":      CoachBateLyricsJSONParser,
    "CoachBateVideoCombine":          CoachBateVideoCombine,
    "CoachBateStripAPIKeyMetadata":   CoachBateStripAPIKeyMetadata,
    "CoachBateNumberedText":          CoachBateNumberedText,
    "CoachBateTextPreviewEdit":       CoachBateTextPreviewEdit,
    "CoachBateFileExists":            CoachBateFileExists,
    "CoachBateUpscaleRifeTensorRT":   CoachBateUpscaleRifeTensorRT,
    "CoachBateH3SegmMask":            CoachBateH3SegmMask,
    "CoachBateH3SubjectCount":        CoachBateH3SubjectCount,
    "CoachBateH3LoopSubject":         CoachBateH3LoopSubject,
    "CoachBateH3SubjectTrack":        CoachBateH3SubjectTrack,
    "CoachBateH3InjectVideoLatent":   CoachBateH3InjectVideoLatent,
    "CoachBateH3PerSubjectDenoise":   CoachBateH3PerSubjectDenoise,
    "CoachBateH3SubjectStitch":       CoachBateH3SubjectStitch,
    "CoachBateVideoPromptFromMetadata": CoachBateVideoPromptFromMetadata,
    # Audio scheduling
    "CoachBateAudioSchedule":         CoachBateAudioSchedule,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "CoachBateShotLoader":            "CoachBate Shot Loader",
    "CoachBateLoadH3AudioPath":       "CoachBate Load H3 Audio Path",
    "CoachBateLoadH3VideoPath":       "CoachBate Load H3 Video Path",
    "CoachBateShotAudioTail":         "CoachBate Shot Audio Tail",
    "CoachBateShotGraph":             "CoachBate Shot Render Graph",
    "CoachBateLoadVideosWithAudio":   "CoachBate Load Videos With Audio",
    "CoachBateBatchPrompter":         "CoachBate Batch Prompter",
    "CoachBateLyricsJSONParser":      "Lyrics JSON Parser",
    "CoachBateVideoCombine":          "CoachBate Video Combine",
    "CoachBateStripAPIKeyMetadata":   "CoachBate Strip API Key Metadata",
    "CoachBateNumberedText":          "CoachBate Numbered Text",
    "CoachBateTextPreviewEdit":       "CoachBate Text Preview and Edit",
    "CoachBateFileExists":            "CoachBate File Exists",
    "CoachBateUpscaleRifeTensorRT":   "CoachBate Upscale and Frame Interpolator TensorRT",
    "CoachBateH3SegmMask":            "CoachBate H3 Segm Mask (YOLO)",
    "CoachBateH3SubjectCount":        "CoachBate H3 Subject Count",
    "CoachBateH3LoopSubject":         "CoachBate H3 Loop Subject",
    "CoachBateH3SubjectTrack":        "CoachBate H3 Subject Track + Crop",
    "CoachBateH3InjectVideoLatent":   "CoachBate H3 Inject Video Latent (img2img)",
    "CoachBateH3PerSubjectDenoise":   "CoachBate H3 Per-Subject Denoise",
    "CoachBateH3SubjectStitch":       "CoachBate H3 Subject Stitch Back",
    "CoachBateVideoPromptFromMetadata": "CoachBate Video Prompt From Metadata",
    # Audio scheduling
    "CoachBateAudioSchedule":         "CoachBate Audio Schedule",
}

# LTX Director suite + LTX-FreeFuse are unfinished/WIP and are not part of the
# public release. They only register when a local, gitignored marker file is
# present (see .gitignore and CLAUDE.md). A public clone never imports this
# code, so the nodes don't exist in NODE_CLASS_MAPPINGS, the ComfyExtension
# node list, or /object_info.
# The publish script also strips the LTX source itself from the public tree, so
# require the code to actually be present — otherwise a stray marker file on a
# public install would turn this into an ImportError at startup.
_PKG_DIR = Path(__file__).parent
_LTX_ENABLED = (_PKG_DIR / ".coachbate_enable_ltx").exists() and (_PKG_DIR / "ltx_director").is_dir()

if _LTX_ENABLED:
    log.info("[CoachBate] .coachbate_enable_ltx found — registering WIP LTX Director/FreeFuse nodes.")

    from .nodes_ltx_freefuse import (
        CoachBateLTXLoRALoader,
        CoachBateLTXConceptMap,
        CoachBateLTXPhase1Sampler,
        CoachBateLTXMaskApplicator,
    )
    # LTX Director suite (forked from WhatDreamsCost/WhatDreamsCost-ComfyUI)
    from .ltx_director import LTXDirector, LTXDirectorGuide, LTXTrimLatent
    from comfy_api.latest import ComfyExtension, io
    from typing_extensions import override

    NODE_CLASS_MAPPINGS.update({
        # LTX-FreeFuse
        "CoachBateLTXLoRALoader":         CoachBateLTXLoRALoader,
        "CoachBateLTXConceptMap":         CoachBateLTXConceptMap,
        "CoachBateLTXPhase1Sampler":      CoachBateLTXPhase1Sampler,
        "CoachBateLTXMaskApplicator":     CoachBateLTXMaskApplicator,
        # LTX Director suite (forked from WhatDreamsCost)
        "CoachBateLTXDirector":           LTXDirector,
        "CoachBateLTXDirectorGuide":      LTXDirectorGuide,
        "CoachBateLTXTrimLatent":         LTXTrimLatent,
    })

    NODE_DISPLAY_NAME_MAPPINGS.update({
        # LTX-FreeFuse
        "CoachBateLTXLoRALoader":         "CoachBate LTX LoRA Loader",
        "CoachBateLTXConceptMap":         "CoachBate LTX Concept Map",
        "CoachBateLTXPhase1Sampler":      "CoachBate LTX Phase 1 Sampler",
        "CoachBateLTXMaskApplicator":     "CoachBate LTX Mask Applicator",
        # LTX Director suite (forked from WhatDreamsCost)
        "CoachBateLTXDirector":           "LTX Director",
        "CoachBateLTXDirectorGuide":      "LTX Director Guide",
        "CoachBateLTXTrimLatent":         "LTX Trim Latent",
    })

    # ComfyExtension entrypoint for new-API nodes (LTXDirector, LTXDirectorGuide, LTXTrimLatent)
    class CoachBateExtension(ComfyExtension):
        @override
        async def get_node_list(self) -> list[type[io.ComfyNode]]:
            return [LTXDirector, LTXDirectorGuide, LTXTrimLatent]

    async def comfy_entrypoint() -> CoachBateExtension:
        return CoachBateExtension()

# Tells ComfyUI to serve every .js file in ./web/ as a frontend extension.
# The path is relative to this __init__.py file.
WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
