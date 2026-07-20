"""
CoachBate LTX-FreeFuse nodes.

Four nodes for multi-character LoRA separation with LTX-2.3:

  CoachBateLTXLoRALoader    — load a character LoRA in masked-bypass mode
  CoachBateLTXConceptMap    — map adapter names to character concept text + find Gemma3 positions
  CoachBateLTXPhase1Sampler — run a short sampling pass to collect attention & generate masks
  CoachBateLTXMaskApplicator — apply the Phase 1 masks to the bypass LoRA hooks

Typical workflow:
  [UNETLoader] → [CoachBateLTXLoRALoader×2] → [CoachBateLTXConceptMap]
       ↓
  [CoachBateLTXPhase1Sampler] → masks
       ↓
  [CoachBateLTXMaskApplicator] → model → [KSampler]
"""

import logging
import math
import os
from typing import Dict

import torch
import torch.nn.functional as F

import comfy.samplers
import comfy.sample

from .ltx_freefuse.attention_replace import (
    LTXFreeFuseState,
    apply_ltx_patches,
    compute_masks_from_state,
)
from .ltx_freefuse.lora_hook import (
    MaskedBypassInjectionManager,
    load_masked_bypass_lora,
    apply_masks_to_managers,
)
from .ltx_freefuse.token_utils import (
    get_gemma3_tokenizer_for_ltx,
    find_concept_positions_gemma3,
)

log = logging.getLogger("coachbate.ltx_freefuse")

# Custom data types passed between nodes
LTXFREEFUSE_DATA_TYPE = "LTXFREEFUSE_DATA"
LTXFREEFUSE_MASKS_TYPE = "LTXFREEFUSE_MASKS"

def _list_loras():
    try:
        import folder_paths
        return folder_paths.get_filename_list("loras")
    except Exception:
        return []


# ---------------------------------------------------------------------------
# CoachBateLTXLoRALoader
# ---------------------------------------------------------------------------

class CoachBateLTXLoRALoader:
    """
    Load a character LoRA for LTX-2.3 in masked-bypass mode.

    Chain multiple loaders (one per character). Each adds to LTXFREEFUSE_DATA.
    """

    RETURN_TYPES = ("MODEL", "CLIP", LTXFREEFUSE_DATA_TYPE)
    RETURN_NAMES = ("model", "clip", "ltxfreefuse_data")
    FUNCTION = "load_lora"
    CATEGORY = "CoachBate/LTX-FreeFuse"
    EXPERIMENTAL = True
    DEV_ONLY = True
    DESCRIPTION = (
        "Loads a character LoRA for LTX-2.3 in masked-bypass mode. "
        "Chain multiple instances (one per character) to load up to 4 character LoRAs. "
        "Each LoRA is spatially isolated to its character's region using attention masks derived in Phase 1, "
        "preventing LoRA bleed between characters sharing the frame."
    )
    RETURN_TOOLTIPS = (
        "Base model with this character's LoRA applied in bypass mode.",
        "CLIP model with this character's LoRA applied.",
        "LoRA manager data containing all loaded adapters — pass to CoachBateLTXConceptMap.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL", {
                    "tooltip": "The base model to apply the character LoRA to.",
                }),
                "clip": ("CLIP", {
                    "tooltip": "The CLIP text encoder to apply the character LoRA to.",
                }),
                "lora_name": (_list_loras(), {
                    "tooltip": "LoRA file to load from your ComfyUI loras folder.",
                }),
                "adapter_name": ("STRING", {
                    "default": "char1",
                    "tooltip": "Unique name for this character — must match concept_name in CoachBateLTXConceptMap.",
                }),
                "strength_model": ("FLOAT", {
                    "default": 1.0, "min": -10.0, "max": 10.0, "step": 0.05,
                    "tooltip": "LoRA weight multiplier applied to the UNet model weights.",
                }),
                "strength_clip": ("FLOAT", {
                    "default": 1.0, "min": -10.0, "max": 10.0, "step": 0.05,
                    "tooltip": "LoRA weight multiplier applied to the CLIP text encoder weights.",
                }),
            },
            "optional": {
                "ltxfreefuse_data": (LTXFREEFUSE_DATA_TYPE, {
                    "tooltip": "Chain from a previous CoachBateLTXLoRALoader to add a second character.",
                }),
            },
        }

    def load_lora(self, model, clip, lora_name, adapter_name, strength_model, strength_clip, ltxfreefuse_data=None):
        import folder_paths
        lora_path = folder_paths.get_full_path("loras", lora_name)
        if lora_path is None or not os.path.isfile(lora_path):
            raise ValueError(f"[CoachBate LTXFreeFuse] LoRA file not found: {lora_name}")

        new_model, new_clip, manager = load_masked_bypass_lora(
            model, clip, lora_path, strength_model, strength_clip, adapter_name
        )

        # Build or extend freefuse_data dict
        data = dict(ltxfreefuse_data) if ltxfreefuse_data else {}
        managers: Dict[str, MaskedBypassInjectionManager] = dict(data.get("managers", {}))
        managers[adapter_name] = manager

        data["managers"] = managers
        # Preserve existing token_pos_maps if re-chaining
        if "token_pos_maps" not in data:
            data["token_pos_maps"] = {}

        log.info("[CoachBate LTXFreeFuse] Loaded LoRA '%s' as adapter '%s'", lora_name, adapter_name)
        return (new_model, new_clip, data)


# ---------------------------------------------------------------------------
# CoachBateLTXConceptMap
# ---------------------------------------------------------------------------

class CoachBateLTXConceptMap:
    """
    Map adapter names to character concept text, find Gemma3 token positions.

    The concept_text must appear verbatim in your positive prompt.
    E.g. adapter_name="char1", concept_text="Alice"  with prompt "Alice and Bob walk together"
    """

    RETURN_TYPES = (LTXFREEFUSE_DATA_TYPE,)
    RETURN_NAMES = ("ltxfreefuse_data",)
    FUNCTION = "build_concept_map"
    CATEGORY = "CoachBate/LTX-FreeFuse"
    EXPERIMENTAL = True
    DEV_ONLY = True
    DESCRIPTION = (
        "Maps adapter names to character concept text and finds their token positions in the positive prompt "
        "using the Gemma3 tokenizer. "
        "The concept text must appear verbatim in the positive prompt — these positions are used in "
        "Phase 1 to derive spatial attention masks for each character."
    )
    RETURN_TOOLTIPS = (
        "Updated ltxfreefuse_data with token position maps added — pass to CoachBateLTXPhase1Sampler.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "clip": ("CLIP", {
                    "tooltip": "The CLIP model — used to access the Gemma3 tokenizer for token position finding.",
                }),
                "positive_prompt": ("STRING", {
                    "multiline": True,
                    "default": "Alice and Bob stand together in a park",
                    "tooltip": "The full positive prompt for video generation; each character's concept text must appear verbatim here.",
                }),
                "ltxfreefuse_data": (LTXFREEFUSE_DATA_TYPE, {
                    "tooltip": "LoRA manager data from the CoachBateLTXLoRALoader chain.",
                }),
                "char1_name": ("STRING", {
                    "default": "char1",
                    "tooltip": "Must match the adapter_name used in CoachBateLTXLoRALoader for character 1.",
                }),
                "char1_concept": ("STRING", {
                    "default": "Alice",
                    "tooltip": "Text identifying character 1 in the prompt; must appear verbatim.",
                }),
                "char2_name": ("STRING", {
                    "default": "char2",
                    "tooltip": "Must match the adapter_name used in CoachBateLTXLoRALoader for character 2.",
                }),
                "char2_concept": ("STRING", {
                    "default": "Bob",
                    "tooltip": "Text identifying character 2 in the prompt; must appear verbatim.",
                }),
            },
            "optional": {
                "char3_name": ("STRING", {
                    "default": "",
                    "tooltip": "Adapter name for optional third character; leave blank to skip.",
                }),
                "char3_concept": ("STRING", {
                    "default": "",
                    "tooltip": "Text identifying character 3 in the prompt; leave blank if not used.",
                }),
                "char4_name": ("STRING", {
                    "default": "",
                    "tooltip": "Adapter name for optional fourth character; leave blank to skip.",
                }),
                "char4_concept": ("STRING", {
                    "default": "",
                    "tooltip": "Text identifying character 4 in the prompt; leave blank if not used.",
                }),
            },
        }

    def build_concept_map(
        self, clip, positive_prompt, ltxfreefuse_data,
        char1_name, char1_concept, char2_name, char2_concept,
        char3_name="", char3_concept="",
        char4_name="", char4_concept="",
    ):
        concepts = {char1_name: char1_concept, char2_name: char2_concept}
        for n, c in [(char3_name, char3_concept), (char4_name, char4_concept)]:
            if n.strip() and c.strip():
                concepts[n.strip()] = c.strip()

        tokenizer = get_gemma3_tokenizer_for_ltx(clip)
        token_pos_maps = find_concept_positions_gemma3(tokenizer, positive_prompt, concepts)

        for name, positions in token_pos_maps.items():
            if not positions or not positions[0]:
                log.warning(
                    "[CoachBate LTXFreeFuse] Concept '%s' ('%s') not found in prompt",
                    name, concepts[name],
                )

        data = dict(ltxfreefuse_data)
        data["token_pos_maps"] = token_pos_maps
        data["concepts"] = concepts
        data["prompt"] = positive_prompt
        return (data,)


# ---------------------------------------------------------------------------
# CoachBateLTXPhase1Sampler
# ---------------------------------------------------------------------------

class CoachBateLTXPhase1Sampler:
    """
    Phase 1: full denoising pass that also collects cross-attention similarity maps.

    Runs all steps (same as a normal KSampler), intercepts the specified transformer
    block at collect_step to extract per-character spatial attention, and produces:
      - ltxfreefuse_masks  — pass to CoachBateLTXMaskApplicator before Phase 2
      - latent             — the fully denoised Phase 1 latent (feed to upscaler/Phase 2)
      - mask_preview       — RGB visualisation of character masks
    """

    RETURN_TYPES = (LTXFREEFUSE_MASKS_TYPE, "LATENT", "IMAGE")
    RETURN_NAMES = ("ltxfreefuse_masks", "latent", "mask_preview")
    FUNCTION = "run_phase1"
    CATEGORY = "CoachBate/LTX-FreeFuse"
    EXPERIMENTAL = True
    DEV_ONLY = True
    DESCRIPTION = (
        "Runs a partial sampling pass to collect cross-attention similarity maps, then derives spatial masks "
        "showing where each character appears in the latent. "
        "Only denoises to collect_step — the original input latent is returned unchanged so Phase 2 always starts from pure noise. "
        "Connect ltxfreefuse_masks to CoachBateLTXMaskApplicator and latent directly to your Phase 2 KSampler."
    )
    RETURN_TOOLTIPS = (
        "Spatial masks derived from attention maps — pass to CoachBateLTXMaskApplicator.",
        "The original input latent, unchanged — connect to Phase 2 KSampler as the starting latent.",
        "RGB visualization of character spatial masks: red = char1, blue = char2, green = char3.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL", {
                    "tooltip": "Model with all character LoRAs applied from the CoachBateLTXLoRALoader chain.",
                }),
                "positive": ("CONDITIONING", {
                    "tooltip": "Positive conditioning for sampling.",
                }),
                "negative": ("CONDITIONING", {
                    "tooltip": "Negative conditioning for sampling.",
                }),
                "latent": ("LATENT", {
                    "tooltip": "Empty latent to denoise; returned unchanged for use as Phase 2 input.",
                }),
                "ltxfreefuse_data": (LTXFREEFUSE_DATA_TYPE, {
                    "tooltip": "Data from CoachBateLTXConceptMap with token position maps.",
                }),
                "seed": ("INT", {
                    "default": 0, "min": 0, "max": 0xffffffffffffffff,
                    "tooltip": "Random seed for noise generation.",
                }),
                "steps": ("INT", {
                    "default": 20, "min": 1, "max": 150,
                    "tooltip": "Total number of denoising steps in the sampling schedule.",
                }),
                "collect_step": ("INT", {"default": 5, "min": 1, "max": 150,
                    "tooltip": "Step at which to collect attention maps (1-based). Higher values produce a more developed layout before mask extraction."}),
                "cfg": ("FLOAT", {
                    "default": 3.0, "min": 0.0, "max": 20.0, "step": 0.1,
                    "tooltip": "Classifier-free guidance scale.",
                }),
                "sampler_name": (comfy.samplers.KSampler.SAMPLERS, {
                    "tooltip": "Sampling algorithm.",
                }),
                "scheduler": (comfy.samplers.KSampler.SCHEDULERS, {
                    "tooltip": "Noise schedule type.",
                }),
                "collect_block": ("INT", {"default": 10, "min": 0, "max": 55,
                    "tooltip": "Transformer block index to collect from (0-27 for LTXV)"}),
                "include_background": ("BOOLEAN", {
                    "default": False,
                    "tooltip": (
                        "False (recommended for 2 people): clean 50/50 split — every pixel "
                        "belongs to one character. "
                        "True: ~1/3 each + ~1/3 background (no LoRA). Use when characters "
                        "occupy a small portion of the frame."
                    ),
                }),
                "mask_mode": (["spatial", "soft", "face_priority", "none"], {
                    "default": "spatial",
                    "tooltip": (
                        "spatial: hard per-pixel split, each pixel belongs to one character. "
                        "soft: proportional attention blend, both LoRAs contribute everywhere. "
                        "face_priority: hard separation at the face/head region, soft blend on the body — "
                        "best for intimate content where bodies overlap. "
                        "none: no spatial masking, both LoRAs apply at full strength everywhere."
                    ),
                }),
            },
            "optional": {
                "collect_block_end": ("INT", {"default": 10, "min": 0, "max": 55,
                    "tooltip": "If > collect_block, average maps across block range"}),
                "sigmas": ("SIGMAS", {
                    "tooltip": (
                        "Override sigma schedule for the attention-collection pass. "
                        "Required for distilled LTX models — connect the LTX-specific "
                        "sigma node here. steps/scheduler are ignored when sigmas is connected."
                    ),
                }),
            },
        }

    def run_phase1(
        self, model, positive, negative, latent, ltxfreefuse_data,
        seed, steps, collect_step, cfg, sampler_name, scheduler,
        collect_block, include_background=False, collect_block_end=None,
        mask_mode="spatial", sigmas=None,
    ):
        if collect_block_end is None or collect_block_end < collect_block:
            collect_block_end = collect_block

        # Determine spatial dims from latent
        # LTXAV uses a NestedTensor (video, audio); extract video tensor for shape inspection
        latent_samples = latent["samples"]
        video_samples = latent_samples.tensors[0] if getattr(latent_samples, "is_nested", False) else latent_samples
        if video_samples.dim() == 5:
            _, _, lat_T, lat_H, lat_W = video_samples.shape
        elif video_samples.dim() == 4:
            lat_T = 1
            _, _, lat_H, lat_W = video_samples.shape
        else:
            raise ValueError(f"[CoachBate LTXFreeFuse] Unexpected latent shape: {video_samples.shape}")

        # Build state
        state = LTXFreeFuseState()
        state.collect_step = collect_step
        state.collect_block = collect_block
        state.collect_block_end = collect_block_end
        state.latent_t = lat_T
        state.latent_h = lat_H
        state.latent_w = lat_W
        state.token_pos_maps = ltxfreefuse_data.get("token_pos_maps", {})

        if not state.token_pos_maps:
            raise ValueError(
                "[CoachBate LTXFreeFuse] No token position maps found in ltxfreefuse_data. "
                "Connect a CoachBateLTXConceptMap node first."
            )

        # Clone model and apply block replace patches (observation only — does not alter output)
        patched_model = model.clone()
        apply_ltx_patches(patched_model, state)

        # Phase 1 only needs to reach collect_step to harvest attention maps.
        # Running the full schedule would fully denoise the latent, causing Phase 2
        # (which expects noise as input) to produce corrupted output.
        # We stop at collect_step+1 and return the ORIGINAL latent so Phase 2
        # always starts from pure noise.
        try:
            comfy.sample.sample(
                patched_model,
                noise=comfy.sample.prepare_noise(latent_samples, seed, None),
                steps=steps,
                cfg=cfg,
                sampler_name=sampler_name,
                scheduler=scheduler,
                positive=positive,
                negative=negative,
                latent_image=latent_samples,
                start_step=0,
                last_step=collect_step + 1,
                force_full_denoise=False,
                noise_mask=None,
                sigmas=sigmas,
                callback=None,
                disable_pbar=False,
                seed=seed,
            )
        except Exception as exc:
            log.warning("[CoachBate LTXFreeFuse] Phase 1 sampling raised: %s", exc)
            raise

        if not state.similarity_maps and not state.block_similarity_maps:
            raise RuntimeError(
                "[CoachBate LTXFreeFuse] No similarity maps collected. "
                f"Try reducing collect_step (currently {collect_step}) or adjust collect_block."
            )

        masks = compute_masks_from_state(state, lat_H, lat_W,
                                         include_background=include_background,
                                         mask_mode=mask_mode)

        for name, m in masks.items():
            coverage = m.gt(0.5).float().mean().item() * 100
            log.info("[CoachBate LTXFreeFuse] Mask '%s': coverage=%.1f%%  shape=%s", name, coverage, tuple(m.shape))

        # Warn if no managers have registered hooks (bypass masking won't work)
        managers = ltxfreefuse_data.get("managers", {})
        for adapter_name, manager in managers.items():
            if manager.get_hook_count() == 0:
                log.warning(
                    "[CoachBate LTXFreeFuse] Adapter '%s' has 0 bypass hooks — "
                    "spatial masking will NOT be applied. This LoRA may use a format "
                    "not compatible with bypass mode.",
                    adapter_name,
                )

        preview = _build_mask_preview(masks, lat_H, lat_W, lat_T, include_background=include_background)

        masks_data = {
            "masks": masks,
            "latent_t": lat_T,
            "latent_h": lat_H,
            "latent_w": lat_W,
        }
        # Return the original input latent unchanged — Phase 2 must start from pure
        # noise, not a partially- or fully-denoised Phase 1 result.
        return (masks_data, {"samples": latent_samples}, preview)


_PREVIEW_MIN_PX = 256  # upscale so the shorter side is at least this many pixels

_CHAR_COLOURS = [
    torch.tensor([1.0, 0.3, 0.3]),  # red   — char 1
    torch.tensor([0.3, 0.5, 1.0]),  # blue  — char 2
    torch.tensor([0.3, 1.0, 0.3]),  # green — char 3
    torch.tensor([1.0, 1.0, 0.3]),  # yellow — char 4
]
_BG_COLOUR = torch.tensor([0.15, 0.15, 0.15])  # dark gray — background (no LoRA)


def _build_mask_preview(masks: dict, H: int, W: int, num_frames: int = 1, include_background: bool = False) -> torch.Tensor:
    """
    Return (T, H', W', 3) RGB preview of character masks, upscaled to a viewable size.

    Repeating the same spatial mask across T frames so the output can be fed
    directly into a video-preview / CreateVideo node.

    When include_background=True the unassigned pixels are shown in dark gray.
    When include_background=False every pixel belongs to a character.
    """
    char_masks = {}
    for idx, (name, mask) in enumerate(masks.items()):
        m = mask.float().to("cpu")
        if m.shape != (H, W):
            m = F.interpolate(m.unsqueeze(0).unsqueeze(0), size=(H, W), mode="nearest").squeeze()
        char_masks[name] = (m, _CHAR_COLOURS[idx % len(_CHAR_COLOURS)])

    if include_background:
        assigned = sum(m for m, _ in char_masks.values()).clamp(0, 1)
        bg = (1.0 - assigned).clamp(0, 1)
        canvas = bg.unsqueeze(-1) * _BG_COLOUR.unsqueeze(0).unsqueeze(0)
    else:
        canvas = torch.zeros(H, W, 3)

    for m, colour in char_masks.values():
        canvas = canvas + m.unsqueeze(-1) * colour.unsqueeze(0).unsqueeze(0)
    canvas = canvas.clamp(0, 1)

    # Upscale if latent-resolution is too small to be useful as a preview
    short = min(H, W)
    if short < _PREVIEW_MIN_PX:
        scale = math.ceil(_PREVIEW_MIN_PX / short)
        canvas = F.interpolate(
            canvas.permute(2, 0, 1).unsqueeze(0),  # (1,3,H,W)
            size=(H * scale, W * scale),
            mode="nearest",
        ).squeeze(0).permute(1, 2, 0)  # (H',W',3)

    # Repeat the single spatial frame across all video frames so downstream
    # video-preview nodes receive the correct frame count.
    return canvas.unsqueeze(0).expand(num_frames, -1, -1, -1).contiguous()  # (T, H', W', 3)


# ---------------------------------------------------------------------------
# CoachBateLTXMaskApplicator
# ---------------------------------------------------------------------------

class CoachBateLTXMaskApplicator:
    """
    Apply Phase 1 masks to the bypass LoRA hooks.

    After this node, each character's LoRA only affects its spatial region.
    Connect the output model to your Phase 2 KSampler.

    Optionally accepts SAM-generated masks via external_masks to override
    attention-derived masks (requires SAM3_Detect / SAM3_VideoTrack).
    """

    RETURN_TYPES = ("MODEL",)
    RETURN_NAMES = ("model",)
    FUNCTION = "apply_masks"
    CATEGORY = "CoachBate/LTX-FreeFuse"
    EXPERIMENTAL = True
    DEV_ONLY = True
    DESCRIPTION = (
        "Applies the spatial masks from Phase 1 to the bypass LoRA hooks, so each character's LoRA "
        "influences only its assigned spatial region during Phase 2 sampling. "
        "Optionally accepts SAM-generated masks to override the attention-derived masks. "
        "Connect the output model to your Phase 2 KSampler."
    )
    RETURN_TOOLTIPS = (
        "Model with spatial LoRA masking active — connect to Phase 2 KSampler.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL", {
                    "tooltip": "Model with bypass LoRA hooks from the CoachBateLTXLoRALoader chain.",
                }),
                "ltxfreefuse_data": (LTXFREEFUSE_DATA_TYPE, {
                    "tooltip": "LoRA manager data from the CoachBateLTXLoRALoader chain.",
                }),
                "ltxfreefuse_masks": (LTXFREEFUSE_MASKS_TYPE, {
                    "tooltip": "Spatial masks from CoachBateLTXPhase1Sampler.",
                }),
            },
            "optional": {
                "external_masks": ("MASK", {
                    "tooltip": "Optional SAM-generated masks (one tensor per character). When provided, overrides attention-derived masks.",
                }),
            },
        }

    def apply_masks(self, model, ltxfreefuse_data, ltxfreefuse_masks, external_masks=None):
        managers: Dict[str, MaskedBypassInjectionManager] = ltxfreefuse_data.get("managers", {})
        if not managers:
            raise ValueError("[CoachBate LTXFreeFuse] No LoRA managers found in ltxfreefuse_data")

        T = ltxfreefuse_masks["latent_t"]
        H = ltxfreefuse_masks["latent_h"]
        W = ltxfreefuse_masks["latent_w"]

        if external_masks is not None:
            masks = _parse_external_masks(external_masks, managers, H, W)
        else:
            masks = ltxfreefuse_masks["masks"]

        apply_masks_to_managers(managers, masks, T, H, W)

        log.info(
            "[CoachBate LTXFreeFuse] Masks applied for adapters: %s",
            list(managers.keys()),
        )
        # Model already has the bypass injections registered — just return it
        return (model,)


def _parse_external_masks(
    external_masks: torch.Tensor,
    managers: dict,
    H: int,
    W: int,
) -> Dict[str, torch.Tensor]:
    """
    Convert external SAM masks (stacked tensor) to per-adapter dict.

    external_masks shape: (N_chars, H', W') or (H', W') for a single mask.
    We assign them in the order of managers.keys().
    """
    adapter_names = list(managers.keys())
    if external_masks.dim() == 2:
        external_masks = external_masks.unsqueeze(0)

    masks_out = {}
    for i, name in enumerate(adapter_names):
        if i >= external_masks.shape[0]:
            break
        m = external_masks[i].float()
        if m.shape != (H, W):
            m = F.interpolate(
                m.unsqueeze(0).unsqueeze(0), size=(H, W), mode="bilinear", align_corners=False
            ).squeeze()
        masks_out[name] = m

    return masks_out
