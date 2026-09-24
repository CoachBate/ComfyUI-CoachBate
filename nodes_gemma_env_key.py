"""
Patches ComfyUI-LTXVideo's GemmaAPITextEncode so a BLANK `api_key` widget
falls back to the LTXV_API_KEY environment variable, instead of requiring the
raw key to be typed into (and therefore saved inside) every workflow.

This is the actual fix for keys leaking into saved PNG/MP4 metadata: nothing
scrubbed after the fact can be as reliable as the secret never becoming a
graph value in the first place. A workflow that leaves the widget blank never
serializes a key, however deeply it's wired through subgraphs.

Existing workflows that already have a key typed into the widget are
unaffected -- that value is still used and still gets saved, same as before.
metadata_safety.py's scrub functions remain the safety net for those.
"""
import logging
import os

log = logging.getLogger("coachbate")

GEMMA_ENV_VAR = "LTXV_API_KEY"

_GEMMA_CLASS = None


def _get_gemma_class():
    """Find the ALREADY-LOADED GemmaAPITextEncode class.

    Deliberately does NOT import LTXVideo itself. custom_nodes load
    alphabetically, so ComfyUI-CoachBate is imported BEFORE
    ComfyUI-LTXVideo -- importing gemma_api_conditioning here would drag
    LTXVideo's module in early, out of order and outside its own package
    context. Instead this only reads ComfyUI's node registry, and the caller
    retries later (see patch_gemma_env_key) once every pack has loaded.
    """
    global _GEMMA_CLASS
    if _GEMMA_CLASS is not None:
        return _GEMMA_CLASS

    # ComfyUI's node registry is the ONLY source consulted, deliberately.
    # It is authoritative -- LTXVideo registers the class under exactly this
    # id -- and it is safe.
    #
    # A previous version also swept sys.modules asking every module for a
    # `GemmaAPITextEncode` attribute. That was actively harmful twice over:
    # torch.classes raises RuntimeError for unknown attribute names (which
    # getattr's default does not swallow, and which killed the whole pack
    # import), and transformers' lazy module __getattr__ HAPPILY RETURNS an
    # alias object for any name you ask for -- emitting a deprecation warning
    # per module and risking binding to something that is not this node at
    # all. Never probe arbitrary modules by attribute name.
    try:
        import nodes as comfy_nodes
        cls = (comfy_nodes.NODE_CLASS_MAPPINGS or {}).get("GemmaAPITextEncode")
        if cls is not None and hasattr(cls, "encode"):
            _GEMMA_CLASS = cls
            return _GEMMA_CLASS
    except Exception:
        pass

    return None


def _apply_patch():
    """Wrap GemmaAPITextEncode.encode, if the class can be found yet."""
    cls = _get_gemma_class()
    if cls is None:
        return False
    if getattr(cls, "_coachbate_env_key_patch", False):
        return True

    original_encode = cls.encode

    def patched_encode(self, api_key, prompt, ckpt_name, enhance_prompt=False):
        # Substitute ONLY as a local argument for this call. The widget and
        # the serialized graph keep their empty value, so the key never
        # reaches the saved workflow/prompt metadata -- which is the whole
        # point of doing this at runtime rather than filling in the node.
        if not api_key:
            api_key = os.environ.get(GEMMA_ENV_VAR, "")
        return original_encode(self, api_key, prompt, ckpt_name, enhance_prompt)

    cls.encode = patched_encode
    cls._coachbate_original_encode = original_encode
    cls._coachbate_env_key_patch = True
    log.info("[CoachBate] GemmaAPITextEncode patched: a blank api_key widget "
             "now falls back to the %s environment variable.", GEMMA_ENV_VAR)
    return True


def patch_gemma_env_key():
    """Apply the patch, deferring past ComfyUI's custom_node load order.

    custom_nodes are imported alphabetically, so ComfyUI-CoachBate runs
    BEFORE ComfyUI-LTXVideo and GemmaAPITextEncode does not exist yet at
    import time -- the immediate attempt below almost always misses. The
    PromptServer startup hook then retries once everything is loaded.
    Returns True if patched immediately, or if a retry was successfully
    scheduled; only False when neither is possible.
    """
    if _apply_patch():
        return True

    try:
        from server import PromptServer

        instance = getattr(PromptServer, "instance", None)
        if instance is not None and hasattr(instance, "app"):
            async def _on_startup(_app):
                if not _apply_patch():
                    log.warning(
                        "[CoachBate] GemmaAPITextEncode not found after startup, so the %s "
                        "environment-variable fallback is inactive. Is ComfyUI-LTXVideo enabled?",
                        GEMMA_ENV_VAR,
                    )

            instance.app.on_startup.append(_on_startup)
            return True
    except Exception as exc:
        log.warning("[CoachBate] Could not schedule the Gemma env-key patch retry: %s", exc)

    return False
