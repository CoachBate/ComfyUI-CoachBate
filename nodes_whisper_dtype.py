"""
Patches the VRGDG transcribe nodes so Whisper's fp32 input features are cast
to whatever dtype the checkpoint actually loaded in.

Recent transformers loads openai/whisper-large-v3 in its native fp16
(`dtype="auto"` is the default now), but WhisperProcessor always returns fp32
features. The first conv1d then dies with:

    RuntimeError: Input type (float) and bias type (struct c10::Half)
                  should be the same

comfyui-vrgamedevgirl is fixed locally (its own commit casts to model.dtype at
each call site), but that fix is a working-tree change on somebody else's repo
and will be lost the next time the pack updates. This patch is the belt to that
braces: it survives the update, and it goes quiet -- costing one extra no-op
`.to()` -- once upstream carries the fix.

Scope is deliberately narrow. WhisperForConditionalGeneration.generate is only
wrapped lazily, from inside a VRGDG transcribe call, so transformers is never
imported on our account and no other pack's Whisper usage is touched until one
of these nodes actually runs.
"""
import logging

log = logging.getLogger("coachbate")

# node id -> method that ends up calling Whisper
_TARGET_NODES = {
    "VRGDG_TranscribeText": "transcribe",
    "VRGDG_LoadAudioSplit_HUMO_Transcribe": "split_audio",
}

_generate_patched = False


def _patch_whisper_generate():
    """Cast fp32 input_features to the model's own dtype. Idempotent."""
    global _generate_patched
    if _generate_patched:
        return
    _generate_patched = True  # set first: one failed attempt should not retry per-frame

    try:
        from transformers import WhisperForConditionalGeneration
    except Exception as exc:
        log.warning("[CoachBate] Whisper dtype patch skipped, transformers unavailable: %s", exc)
        return

    if getattr(WhisperForConditionalGeneration, "_coachbate_dtype_patch", False):
        return

    original_generate = WhisperForConditionalGeneration.generate

    def patched_generate(self, input_features=None, *args, **kwargs):
        if input_features is not None and getattr(input_features, "dtype", None) != self.dtype:
            # .to() is a no-op when they already match, so an upstream fix
            # (or an fp32 checkpoint) makes this cost nothing.
            input_features = input_features.to(self.dtype)
        return original_generate(self, input_features, *args, **kwargs)

    WhisperForConditionalGeneration.generate = patched_generate
    WhisperForConditionalGeneration._coachbate_dtype_patch = True
    log.info("[CoachBate] Whisper generate() patched: input features are cast to the model dtype.")


def _wrap_node(cls, method_name):
    if getattr(cls, "_coachbate_whisper_dtype_patch", False):
        return True
    original = getattr(cls, method_name, None)
    if original is None:
        return False

    def wrapper(self, *args, **kwargs):
        _patch_whisper_generate()
        return original(self, *args, **kwargs)

    setattr(cls, method_name, wrapper)
    cls._coachbate_whisper_dtype_patch = True
    return True


def _apply_patch():
    """Wrap the VRGDG transcribe entry points, if the pack has loaded yet."""
    try:
        import nodes as comfy_nodes
        mappings = comfy_nodes.NODE_CLASS_MAPPINGS or {}
    except Exception:
        return False

    found = 0
    for node_id, method_name in _TARGET_NODES.items():
        cls = mappings.get(node_id)
        if cls is not None and _wrap_node(cls, method_name):
            found += 1
    return found > 0


def patch_whisper_dtype():
    """Apply the patch, deferring past ComfyUI's custom_node load order.

    Same shape as patch_gemma_env_key: custom_nodes import alphabetically, so
    ComfyUI-CoachBate runs before comfyui-vrgamedevgirl and the immediate
    attempt below normally misses. Returns True if patched now or if a startup
    retry was scheduled.
    """
    if _apply_patch():
        return True

    try:
        from server import PromptServer

        instance = getattr(PromptServer, "instance", None)
        if instance is not None and hasattr(instance, "app"):
            async def _on_startup(_app):
                if not _apply_patch():
                    log.info(
                        "[CoachBate] No VRGDG transcribe nodes found, so the Whisper "
                        "dtype patch is inactive. That is expected unless "
                        "comfyui-vrgamedevgirl is installed."
                    )

            instance.app.on_startup.append(_on_startup)
            return True
    except Exception as exc:
        log.warning("[CoachBate] Could not schedule the Whisper dtype patch retry: %s", exc)

    return False
