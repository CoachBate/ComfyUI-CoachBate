"""Read the prompt a ComfyUI-generated video was made with, out of its own metadata."""

import os

from .video_prompt_resolver import prompt_from_video


class CoachBateVideoPromptFromMetadata:
    """The generation prompt embedded in an mp4 by ComfyUI, resolved through the graph."""

    CATEGORY = "CoachBate/H3"
    FUNCTION = "read"
    RETURN_TYPES = ("STRING", "STRING", "BOOLEAN")
    RETURN_NAMES = ("prompt", "info", "found")
    DESCRIPTION = (
        "Recovers the positive prompt from the workflow/prompt JSON ComfyUI embeds in "
        "an mp4, following links through switches, concatenates and primitives. "
        "Feed it the same path as your video loader so a refine pass reuses the clip's "
        "own prompt without pasting it by hand."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                # `fallback` is deliberately the FIRST input. When this node is bypassed the
                # frontend routes output slot N to input slot N whenever the consumer is an
                # any-typed input (e.g. Text Preview and Edit's `any`), regardless of types.
                # With `video` first, bypassing pushed the video PATH out of `prompt` and over
                # whatever the user had typed downstream. With the normally-unlinked fallback
                # widget at slot 0, a bypassed node emits nothing on `prompt` instead, and the
                # downstream input reads as unconnected.
                "fallback": ("STRING", {
                    "multiline": True, "default": "",
                    "tooltip": "Used as the prompt when the file carries none. Leave empty "
                               "to output an empty prompt in that case.",
                }),
                "video": ("STRING", {
                    "default": "",
                    "tooltip": "Path to the mp4 (the same value as VHS Load Video (Path)). "
                               "Surrounding quotes are stripped.",
                }),
            },
        }

    def read(self, fallback, video):
        path = (video or "").strip()
        if len(path) >= 2 and path[0] == path[-1] and path[0] in {'"', "'"}:
            path = path[1:-1].strip()
        path = os.path.expandvars(os.path.expanduser(path))
        text, reason = prompt_from_video(path)
        if text:
            info = "prompt from %s: %s" % (os.path.basename(path), reason)
            print("[CoachBate] " + info)
            return (text, info, True)
        info = "no prompt recovered from %s (%s)%s" % (
            os.path.basename(path) or path, reason, "; using fallback" if fallback else "")
        print("[CoachBate] " + info)
        return (fallback or "", info, False)
