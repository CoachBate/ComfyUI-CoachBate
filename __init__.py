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
from .nodes_audio_schedule import CoachBateAudioSchedule
from .nodes_lyrics_json import CoachBateLyricsJSONParser
from .nodes_metadata_safety import CoachBateStripAPIKeyMetadata, CoachBateVideoCombine, patch_vhs_video_combine
from .nodes_numbered_text import CoachBateNumberedText
from .nodes_text_preview_edit import CoachBateTextPreviewEdit
from . import routes  # registers /coachbate/* API endpoints

log = logging.getLogger("coachbate")
_vhs_patch_available = patch_vhs_video_combine()
if not _vhs_patch_available:
    log.warning(
        "[CoachBate] VideoHelperSuite was not available, so the VHS_VideoCombine API-key patch was skipped. "
        "Install ComfyUI-VideoHelperSuite to use that integration."
    )

NODE_CLASS_MAPPINGS = {
    "CoachBateShotLoader":            CoachBateShotLoader,
    "CoachBateLoadVideosWithAudio":   CoachBateLoadVideosWithAudio,
    "CoachBateBatchPrompter":         CoachBateBatchPrompter,
    "CoachBateLyricsJSONParser":      CoachBateLyricsJSONParser,
    "CoachBateVideoCombine":          CoachBateVideoCombine,
    "CoachBateStripAPIKeyMetadata":   CoachBateStripAPIKeyMetadata,
    "CoachBateNumberedText":          CoachBateNumberedText,
    "CoachBateTextPreviewEdit":       CoachBateTextPreviewEdit,
    # Audio scheduling
    "CoachBateAudioSchedule":         CoachBateAudioSchedule,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "CoachBateShotLoader":            "CoachBate Shot Loader",
    "CoachBateLoadVideosWithAudio":   "CoachBate Load Videos With Audio",
    "CoachBateBatchPrompter":         "CoachBate Batch Prompter",
    "CoachBateLyricsJSONParser":      "Lyrics JSON Parser",
    "CoachBateVideoCombine":          "CoachBate Video Combine",
    "CoachBateStripAPIKeyMetadata":   "CoachBate Strip API Key Metadata",
    "CoachBateNumberedText":          "CoachBate Numbered Text",
    "CoachBateTextPreviewEdit":       "CoachBate Text Preview and Edit",
    # Audio scheduling
    "CoachBateAudioSchedule":         "CoachBate Audio Schedule",
}

# LTX Director suite + LTX-FreeFuse are unfinished/WIP and are not part of the
# public release. They only register when a local, gitignored marker file is
# present (see .gitignore and CLAUDE.md). A public clone never imports this
# code, so the nodes don't exist in NODE_CLASS_MAPPINGS, the ComfyExtension
# node list, or /object_info.
_LTX_ENABLED = (Path(__file__).parent / ".coachbate_enable_ltx").exists()

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
