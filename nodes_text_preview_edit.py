import torch


class _AnyType(str):
    def __ne__(self, other):
        return False


_any = _AnyType("*")


def _to_str(value):
    try:
        if isinstance(value, torch.Tensor):
            return (
                f"Tensor  shape={tuple(value.shape)}"
                f"  dtype={value.dtype}"
                f"  min={value.min().item():.4f}"
                f"  max={value.max().item():.4f}"
            )
        if isinstance(value, dict) and "samples" in value:
            return f"Latent  shape={tuple(value['samples'].shape)}"
        return str(value)
    except Exception:
        return str(value)


class CoachBateTextPreviewEdit:
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("string",)
    FUNCTION = "execute"
    OUTPUT_NODE = True
    CATEGORY = "CoachBate"
    DESCRIPTION = (
        "A text widget that acts as both viewer and editable buffer. "
        "When prompt_in is connected, it displays and passes through any incoming value, "
        "converting tensors and latents to human-readable summaries. "
        "When not connected, it outputs the typed widget text directly."
    )
    RETURN_TOOLTIPS = (
        "The active prompt text — either the converted prompt_in value or the typed widget text.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "text": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "tooltip": "Editable text shown in the node. Used as the output when nothing is connected to 'any'.",
                }),
            },
            "optional": {
                "any": (_any, {
                    "forceInput": True,
                    "tooltip": "Any value to display and pass through. Overrides the typed text when connected. Tensors and latents are converted to a readable summary.",
                }),
            },
            "hidden": {
                "unique_id": "UNIQUE_ID",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    def execute(self, text="", any=None, unique_id=None, extra_pnginfo=None):
        out = _to_str(any) if any is not None else text

        if any is not None and extra_pnginfo is not None:
            workflow = extra_pnginfo.get("workflow") or {}
            # Subgraph nodes receive compound execution ids like "10:3"
            # (outerNodeId:innerNodeId); the serialized node id is the last
            # segment, both at top level and inside subgraph definitions.
            uid = str(unique_id).split(":")[-1]

            def _find_and_update(nodes):
                for node in nodes:
                    if str(node.get("id")) == uid:
                        vals = node.get("widgets_values")
                        # widgets_values may be a positional list (classic) or a
                        # dict keyed by widget name (Nodes 2.0).  The "text"
                        # textarea is the first/only widget either way.
                        if isinstance(vals, list):
                            if vals:
                                vals[0] = out
                            else:
                                vals.append(out)
                        elif isinstance(vals, dict):
                            vals["text"] = out
                        else:
                            node["widgets_values"] = [out]
                        return True
                return False

            if not _find_and_update(workflow.get("nodes", [])):
                for sg in (workflow.get("definitions") or {}).get("subgraphs", []):
                    if _find_and_update(sg.get("nodes", [])):
                        break

        # Only push a UI update when a value actually flowed in through 'any'.
        # When nothing is connected the widget text IS the source — emitting
        # ui.text would make onExecuted repaint the textarea with the value
        # serialized at queue time, clobbering anything typed after pressing
        # Run (visible as a spurious "update" at the end of every batch job).
        if any is not None:
            return {"ui": {"text": [out]}, "result": (out,)}
        return {"result": (out,)}


NODE_CLASS_MAPPINGS = {
    "CoachBateTextPreviewEdit": CoachBateTextPreviewEdit,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "CoachBateTextPreviewEdit": "CoachBate Text Preview and Edit",
}
