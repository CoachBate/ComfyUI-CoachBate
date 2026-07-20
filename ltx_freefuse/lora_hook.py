"""
Masked bypass LoRA hook for LTX-2.3.

Each character LoRA's contribution is multiplied by a spatial mask before
being added back:  output = base_forward(x) + spatial_mask * lora_path(x)

Uses a per-module dispatcher so that:
  - Multiple character LoRAs on the same module are accumulated correctly
  - Stale hooks from previous runs are replaced, not stacked
    (which would cause infinite recursion via self.original_forward → self._bypass_forward)
"""

import logging
from typing import Dict, List, Optional

import torch
import torch.nn.functional as F

import comfy.lora
import comfy.lora_convert
import comfy.utils
import comfy.weight_adapter
from comfy.weight_adapter.base import WeightAdapterBase
from comfy.weight_adapter.bypass import BypassForwardHook, BypassInjectionManager
from comfy.patcher_extension import PatcherInjection

log = logging.getLogger("coachbate.ltx_freefuse")

# ──────────────────────────────────────────────────────────────────────────────
# Per-module dispatcher
# ──────────────────────────────────────────────────────────────────────────────

class _ModuleDispatcher:
    """
    Attached to a module as `module._ltxff_dispatcher`.
    Replaces module.forward and accumulates contributions from all
    registered MaskedBypassForwardHook instances.

    Hooks are keyed by adapter_name so a new run's hooks replace the old
    ones for the same character rather than stacking on top.
    """

    def __init__(self, true_forward):
        self.true_forward = true_forward
        # {adapter_name: hook}
        self._hooks: Dict[str, "MaskedBypassForwardHook"] = {}

    @property
    def hooks(self) -> List["MaskedBypassForwardHook"]:
        return list(self._hooks.values())

    def add(self, hook: "MaskedBypassForwardHook"):
        name = getattr(hook, "adapter_name", id(hook))
        self._hooks[name] = hook

    def remove(self, hook: "MaskedBypassForwardHook"):
        name = getattr(hook, "adapter_name", id(hook))
        self._hooks.pop(name, None)

    def is_empty(self) -> bool:
        return not self._hooks

    def __call__(self, x: torch.Tensor, *args, **kwargs) -> torch.Tensor:
        base_out = self.true_forward(x, *args, **kwargs)
        for hook in self._hooks.values():
            h_out = hook.adapter.h(x, base_out)
            h_out = _apply_spatial_mask(h_out, hook)
            base_out = hook.adapter.g(base_out + h_out)
        return base_out


def _apply_spatial_mask(h_out: torch.Tensor, hook: "MaskedBypassForwardHook") -> torch.Tensor:
    """Apply the hook's spatial mask to h_out, auto-scaling for Phase 2 resolution."""
    if hook.spatial_mask is None or h_out.dim() != 3:
        return h_out
    # Audio attention layers use a different token layout — skip spatial masking so
    # the character's audio/voice contribution is applied uniformly (not half-blanked).
    if hook.is_audio:
        return h_out

    N_tokens = h_out.shape[1]
    T = hook.video_T
    N_spatial_stored = hook.video_H * hook.video_W

    if T <= 0 or N_tokens % T != 0:
        return h_out

    N_spatial_actual = N_tokens // T

    if N_spatial_actual == N_spatial_stored:
        mask_2d = hook.spatial_mask
    else:
        scale = (N_spatial_actual / N_spatial_stored) ** 0.5
        new_H = round(hook.video_H * scale)
        new_W = round(hook.video_W * scale)
        if new_H * new_W != N_spatial_actual:
            return h_out  # Aspect ratio changed — skip
        mask_2d = F.interpolate(
            hook.spatial_mask.unsqueeze(0).unsqueeze(0),
            size=(new_H, new_W), mode="bilinear", align_corners=False,
        ).squeeze(0).squeeze(0)

    mask_flat = mask_2d.reshape(-1)
    if T > 1:
        mask_flat = mask_flat.unsqueeze(0).expand(T, -1).reshape(-1)
    mask_flat = mask_flat.to(device=h_out.device, dtype=h_out.dtype)
    return h_out * mask_flat.unsqueeze(0).unsqueeze(-1)


# ──────────────────────────────────────────────────────────────────────────────
# Hook
# ──────────────────────────────────────────────────────────────────────────────

_AUDIO_KEY_FRAGMENTS = ("audio_attn", "audio_cross_attn", "audio_ff", "a_attn")


def _is_audio_key(key: str) -> bool:
    """Return True if this module key belongs to an audio attention layer."""
    k = key.lower()
    return any(frag in k for frag in _AUDIO_KEY_FRAGMENTS)


class MaskedBypassForwardHook(BypassForwardHook):
    """
    BypassForwardHook with an optional spatial mask.

    Rather than directly replacing module.forward, this hook registers
    itself with the module's _ModuleDispatcher so that:
      - Multiple character LoRAs accumulate correctly
      - Re-runs replace stale hooks instead of stacking them
    """

    def __init__(self, module, adapter, multiplier=1.0):
        super().__init__(module, adapter, multiplier)
        self.spatial_mask: Optional[torch.Tensor] = None
        self.video_T: int = 1
        self.video_H: int = 32
        self.video_W: int = 32
        self.adapter_name: str = ""   # set by MaskedBypassInjectionManager
        self.module_key: str = ""     # set by MaskedBypassInjectionManager
        self.is_audio: bool = False   # set by MaskedBypassInjectionManager

    def set_spatial_mask(self, mask: torch.Tensor, T: int, H: int, W: int):
        self.spatial_mask = mask.float()
        self.video_T = T
        self.video_H = H
        self.video_W = W

    # ------------------------------------------------------------------
    # Override inject/eject to use per-module dispatcher
    # ------------------------------------------------------------------

    def inject(self):
        if self.original_forward is not None:
            return  # Already injected

        # Move adapter weights to the compute device — same as BypassForwardHook.inject()
        # (we override inject() so must do this ourselves)
        import comfy.model_management
        device = comfy.model_management.get_torch_device()
        dtype = None
        if hasattr(self.module, "weight") and self.module.weight is not None:
            dtype = self.module.weight.dtype
        if dtype is not None and dtype not in (torch.float32, torch.float16, torch.bfloat16):
            dtype = None
        self._move_adapter_weights_to_device(device, dtype)

        dispatcher = getattr(self.module, "_ltxff_dispatcher", None)

        if dispatcher is None:
            # First hook on this module — save the true original forward.
            # Walk past any pre-existing dispatchers (shouldn't happen, but be safe).
            true_fwd = self.module.forward
            while isinstance(true_fwd, _ModuleDispatcher):
                true_fwd = true_fwd.true_forward
            dispatcher = _ModuleDispatcher(true_fwd)
            self.module._ltxff_dispatcher = dispatcher
            self.module.forward = dispatcher

        dispatcher.add(self)
        # Store true_forward for compatibility (e.g. eject restoring)
        self.original_forward = dispatcher.true_forward
        log.debug(
            "[LTXFreeFuse] Injected hook '%s' on %s (hooks on module: %d)",
            self.adapter_name, type(self.module).__name__, len(dispatcher.hooks),
        )

    def eject(self):
        if self.original_forward is None:
            return  # Not injected

        dispatcher = getattr(self.module, "_ltxff_dispatcher", None)
        if dispatcher is not None:
            dispatcher.remove(self)
            if dispatcher.is_empty():
                self.module.forward = dispatcher.true_forward
                try:
                    delattr(self.module, "_ltxff_dispatcher")
                except AttributeError:
                    pass

        self.original_forward = None
        log.debug("[LTXFreeFuse] Ejected hook '%s'", self.adapter_name)

    # _bypass_forward is kept for API compatibility but is no longer called
    # (the dispatcher handles accumulation directly).
    def _bypass_forward(self, x: torch.Tensor, *args, **kwargs) -> torch.Tensor:
        base_out = self.original_forward(x, *args, **kwargs)
        h_out = self.adapter.h(x, base_out)
        h_out = _apply_spatial_mask(h_out, self)
        return self.adapter.g(base_out + h_out)


# ──────────────────────────────────────────────────────────────────────────────
# Manager
# ──────────────────────────────────────────────────────────────────────────────

class MaskedBypassInjectionManager(BypassInjectionManager):
    """
    BypassInjectionManager that creates MaskedBypassForwardHook instances
    and tags them with adapter_name for stale-hook replacement.
    """

    def __init__(self, adapter_name: str = ""):
        super().__init__()
        self.adapter_name = adapter_name

    def get_hook_count(self) -> int:
        """Return the number of bypass hooks registered for this adapter."""
        return len(self.hooks)

    def create_injections(self, model) -> List[PatcherInjection]:
        self.hooks.clear()
        n_audio = 0

        for key, (adapter, strength) in self.adapters.items():
            module = self._get_module_by_key(model, key)
            if module is None:
                log.warning("[LTXFreeFuse] Bypass: module not found for key: %s", key)
                continue
            if not hasattr(module, "weight"):
                log.warning("[LTXFreeFuse] Bypass: module %s has no weight attr", key)
                continue

            hook = MaskedBypassForwardHook(module, adapter, multiplier=strength)
            hook.adapter_name = self.adapter_name
            hook.module_key = key
            hook.is_audio = _is_audio_key(key)
            if hook.is_audio:
                n_audio += 1
            self.hooks.append(hook)
            log.debug("[LTXFreeFuse] Created hook for %s (adapter=%s, audio=%s)", key, self.adapter_name, hook.is_audio)

        n_video = len(self.hooks) - n_audio
        log.info(
            "[LTXFreeFuse] Adapter '%s': %d total hooks (%d video, %d audio)",
            self.adapter_name, len(self.hooks), n_video, n_audio,
        )

        # Capture current hooks list in closures (not self.hooks reference)
        hooks_snapshot = list(self.hooks)

        def inject_all(model_patcher):
            for h in hooks_snapshot:
                h.inject()

        def eject_all(model_patcher):
            for h in hooks_snapshot:
                h.eject()

        return [PatcherInjection(inject=inject_all, eject=eject_all)]


# ──────────────────────────────────────────────────────────────────────────────
# LoRA loader
# ──────────────────────────────────────────────────────────────────────────────

def load_masked_bypass_lora(
    model,
    clip,
    lora_path: str,
    strength_model: float,
    strength_clip: float,
    adapter_name: str,
) -> tuple:
    """
    Load a LoRA and attach it via MaskedBypassForwardHook dispatcher.

    Returns (new_model, new_clip, manager).
    After Phase 1, call apply_masks_to_managers(managers, masks, T, H, W)
    to activate spatial masking.
    """
    lora_sd = comfy.utils.load_torch_file(lora_path, safe_load=True)

    key_map: Dict = {}
    if model is not None:
        key_map = comfy.lora.model_lora_keys_unet(model.model, key_map)
    if clip is not None:
        key_map = comfy.lora.model_lora_keys_clip(clip.cond_stage_model, key_map)

    lora_converted = comfy.lora_convert.convert_lora(lora_sd)
    loaded = comfy.lora.load_lora(lora_converted, key_map)

    bypass_patches: Dict = {}
    regular_patches: Dict = {}
    for key, patch_data in loaded.items():
        if isinstance(patch_data, WeightAdapterBase):
            bypass_patches[key] = patch_data
        else:
            regular_patches[key] = patch_data

    log.info(
        "[LTXFreeFuse] LoRA '%s': %d bypass adapters, %d regular patches",
        adapter_name, len(bypass_patches), len(regular_patches),
    )

    manager = MaskedBypassInjectionManager(adapter_name=adapter_name)

    new_model = model
    if model is not None:
        new_model = model.clone()

        if regular_patches:
            new_model.add_patches(regular_patches, strength_model)

        if bypass_patches:
            model_sd_keys = set(new_model.model.state_dict().keys())
            for key, adapter in bypass_patches.items():
                if key in model_sd_keys:
                    manager.add_adapter(key, adapter, strength=strength_model)
                else:
                    log.debug("[LTXFreeFuse] Key not in model state_dict, skipping: %s", key)

            injections = manager.create_injections(new_model.model)
            if manager.get_hook_count() > 0:
                injection_key = f"ltx_freefuse_{adapter_name}"
                new_model.set_injections(injection_key, injections)
                log.info(
                    "[LTXFreeFuse] LoRA '%s': injected %d hooks as '%s'",
                    adapter_name, manager.get_hook_count(), injection_key,
                )

    new_clip = clip
    if clip is not None and regular_patches:
        new_clip = clip.clone()
        new_clip.add_patches(regular_patches, strength_clip)

    return new_model, new_clip, manager


# ──────────────────────────────────────────────────────────────────────────────
# Mask application
# ──────────────────────────────────────────────────────────────────────────────

def apply_masks_to_managers(
    managers_by_name: Dict[str, MaskedBypassInjectionManager],
    masks: Dict[str, torch.Tensor],
    T: int,
    H: int,
    W: int,
):
    """
    Assign spatial masks to all hooks for each named adapter.

    Args:
        managers_by_name: {adapter_name -> MaskedBypassInjectionManager}
        masks: {adapter_name -> (H, W) float mask tensor}
        T, H, W: video latent dimensions
    """
    for adapter_name, manager in managers_by_name.items():
        mask = masks.get(adapter_name)
        if mask is None:
            log.warning("[LTXFreeFuse] No mask found for adapter '%s'", adapter_name)
            continue
        n_video = sum(1 for h in manager.hooks if not h.is_audio)
        n_audio = sum(1 for h in manager.hooks if h.is_audio)
        coverage = mask.gt(0.5).float().mean().item() * 100
        for hook in manager.hooks:
            hook.set_spatial_mask(mask, T, H, W)
        log.info(
            "[LTXFreeFuse] Adapter '%s': mask coverage=%.1f%%  hooks: %d video (masked) + %d audio (unmasked)",
            adapter_name, coverage, n_video, n_audio,
        )
