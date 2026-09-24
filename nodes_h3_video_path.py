"""Episode-independent video reference inputs, decoded by VideoHelperSuite."""
import json
import math
import os


VIDEO_DEFAULTS = dict(force_rate=24, custom_width=0, custom_height=0,
                      frame_load_cap=0, skip_first_frames=0, select_every_nth=1,
                      format="None", include_audio=False)


def video_settings(value):
    if not isinstance(value, dict):
        raise ValueError("Video reference settings must be a JSON object")
    unknown = set(value) - VIDEO_DEFAULTS.keys()
    if unknown:
        raise ValueError(f"Unknown video reference settings: {sorted(unknown)}")
    settings = {**VIDEO_DEFAULTS, **value}
    for key in ("custom_width", "custom_height", "frame_load_cap", "skip_first_frames", "select_every_nth"):
        minimum = 1 if key == "select_every_nth" else 0
        if type(settings[key]) is not int or settings[key] < minimum:
            raise ValueError(f"{key} must be an integer >= {minimum}")
    rate = settings["force_rate"]
    if type(rate) not in (int, float) or not math.isfinite(rate) or not 0 <= rate <= 60:
        raise ValueError("force_rate must be a finite number between 0 and 60")
    if type(settings["include_audio"]) is not bool:
        raise ValueError("include_audio must be a JSON boolean")
    if not isinstance(settings["format"], str) or not settings["format"].strip():
        raise ValueError("format must be a nonempty VHS format name")
    return settings


def resolve_video_references(shot, count=3):
    """Append-only loader contract: paths first, then serialized slot settings."""
    array = shot.get("h3_reference_videos", [])
    if not isinstance(array, list) or len(array) > count:
        raise ValueError(f"h3_reference_videos must be an array of at most {count} paths")
    paths, settings = [], []
    for index in range(1, count + 1):
        # Explicit blank overrides an older alias or array entry.
        path = shot.get(f"h3_ref_video_{index}", shot.get(f"reference_video_{index}",
                       array[index - 1] if index <= len(array) else ""))
        if not isinstance(path, str):
            raise ValueError(f"reference_video_{index} must be a path string")
        paths.append(path.strip())
        options = video_settings(shot.get(f"reference_video_{index}_settings", {}))
        settings.append(json.dumps(options, sort_keys=True))
    return (*paths, *settings)


class CoachBateLoadH3VideoPath:
    RETURN_TYPES = ("IMAGE", "AUDIO", "INT")
    RETURN_NAMES = ("frames", "audio", "frame_count")
    FUNCTION = "load_video_path"
    CATEGORY = "CoachBate/H3"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "video_path": ("STRING", {"default": "", "tooltip": "Blank omits this video reference. Relative paths use ComfyUI output."}),
            "settings_json": ("STRING", {"default": "{}", "multiline": True,
                "tooltip": "Per-shot VHS clip settings. Source audio is disconnected unless include_audio is true."}),
        }}

    @staticmethod
    def _resolve_path(value):
        value = value.strip().strip('"')
        if not value:
            return ""
        if os.path.isabs(value):
            return os.path.abspath(value)
        import folder_paths
        return folder_paths.get_annotated_filepath(value, folder_paths.get_output_directory())

    @classmethod
    def IS_CHANGED(cls, video_path, settings_json="{}"):
        path = cls._resolve_path(video_path)
        if not path:
            return ("", settings_json)
        try:
            stat = os.stat(path)
            return (path, stat.st_size, stat.st_mtime_ns, settings_json)
        except OSError:
            return float("nan")

    def load_video_path(self, video_path, settings_json="{}"):
        settings = video_settings(json.loads(settings_json))
        path = self._resolve_path(video_path)
        if not path:
            return (None, None, 0)
        if not os.path.isfile(path):
            raise ValueError(f"H3 reference video not found: {path}")
        import nodes
        loader_class = nodes.NODE_CLASS_MAPPINGS.get("VHS_LoadVideoPath")
        if loader_class is None:
            raise ValueError("H3 video references require VideoHelperSuite")
        include_audio = settings.pop("include_audio")
        formats = loader_class.INPUT_TYPES()["optional"]["format"][0]
        if settings["format"] not in formats:
            raise ValueError(f"Unknown installed VHS format: {settings['format']}")
        loaded = loader_class().load_video(video=path, **settings)
        values = loaded["result"] if isinstance(loaded, dict) else loaded
        frames, count, audio = values[:3]
        if not count:
            raise ValueError(f"No reference frames decoded from selected clip: {path}")
        return (frames, audio if include_audio else None, count)


class CoachBateShotAudioTail:
    """Optional per-shot output tail using the same existing audio nodes."""
    RETURN_TYPES = ("AUDIO",)
    FUNCTION = "apply"
    CATEGORY = "CoachBate/H3"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"audio": ("AUDIO",), "shot_payload": ("STRING", {"forceInput": True})}}

    def apply(self, audio, shot_payload):
        tail = json.loads(shot_payload)["shot"].get("output_audio_tail")
        if tail is None:
            return (audio,)
        required = {"keep_seconds", "silence_seconds", "sample_rate", "channels"}
        if not isinstance(tail, dict) or set(tail) != required:
            raise ValueError("output_audio_tail requires keep_seconds, silence_seconds, sample_rate, channels")
        for key in ("keep_seconds", "silence_seconds"):
            value = tail[key]
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"output_audio_tail {key} must be positive and finite")
        for key in ("sample_rate", "channels"):
            if type(tail[key]) is not int or tail[key] < 1:
                raise ValueError(f"output_audio_tail {key} must be a positive integer")
        import nodes
        mapping = nodes.NODE_CLASS_MAPPINGS
        def call(name, **kwargs):
            node = mapping[name]()
            result = getattr(node, node.FUNCTION)(**kwargs)
            return result["result"][0] if isinstance(result, dict) else result[0]
        speech = call("TrimAudioDuration", audio=audio, start_index=0, duration=tail["keep_seconds"])
        silence = call("EmptyAudio", duration=tail["silence_seconds"],
                       sample_rate=tail["sample_rate"], channels=tail["channels"])
        return (call("AudioConcat", audio1=speech, audio2=silence, direction="after"),)
