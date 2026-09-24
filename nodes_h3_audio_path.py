"""Output-relative, range-aware audio loader for the H3 shot JSON workflow."""

import os

import av
import folder_paths
import torch


def _load_audio(filepath: str) -> tuple[torch.Tensor, int]:
    with av.open(filepath) as container:
        if not container.streams.audio:
            raise ValueError("No audio stream found in the file.")

        stream = container.streams.audio[0]
        sample_rate = stream.codec_context.sample_rate
        channels = stream.channels
        frames = []
        for frame in container.decode(streams=stream.index):
            waveform = torch.from_numpy(frame.to_ndarray())
            if waveform.shape[0] != channels:
                waveform = waveform.view(-1, channels).t()
            frames.append(waveform)

    if not frames:
        raise ValueError("No audio frames decoded.")

    waveform = torch.cat(frames, dim=1)
    if waveform.dtype.is_floating_point:
        return waveform, sample_rate
    if waveform.dtype == torch.int16:
        return waveform.float() / (2 ** 15), sample_rate
    if waveform.dtype == torch.int32:
        return waveform.float() / (2 ** 31), sample_rate
    raise ValueError(f"Unsupported wav dtype: {waveform.dtype}")


class CoachBateLoadH3AudioPath:
    """Decode an H3 reference-audio path and optional source range."""

    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("audio",)
    FUNCTION = "load_audio_path"
    CATEGORY = "CoachBate/H3"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "audio_path": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": (
                        "H3 reference audio. Relative paths resolve under ComfyUI output; "
                        "use '[output]' to make that explicit. Blank returns no reference audio."
                    ),
                }),
                "start_seconds": ("FLOAT", {
                    "default": 0.0,
                    "min": 0.0,
                    "step": 0.01,
                    "tooltip": "Start of the source-audio slice in seconds.",
                }),
                "end_seconds": ("FLOAT", {
                    "default": -1.0,
                    "min": -1.0,
                    "step": 0.01,
                    "tooltip": "End of the source-audio slice in seconds; -1 uses the rest of the file.",
                }),
            },
        }

    @staticmethod
    def _resolve_path(audio_path: str) -> str:
        value = str(audio_path or "").strip().strip('"')
        if not value:
            return ""
        if os.path.isabs(value):
            return os.path.abspath(value)
        return folder_paths.get_annotated_filepath(value, folder_paths.get_output_directory())

    @classmethod
    def IS_CHANGED(cls, audio_path, start_seconds=0.0, end_seconds=-1.0):
        path = cls._resolve_path(audio_path)
        if not path:
            return ""
        return f"{os.path.getmtime(path)}:{float(start_seconds)}:{float(end_seconds)}"

    def load_audio_path(self, audio_path, start_seconds=0.0, end_seconds=-1.0):
        path = self._resolve_path(audio_path)
        if not path:
            return (None,)
        if not os.path.isfile(path):
            raise ValueError(f"[CoachBate] H3 reference audio not found: {path}")

        waveform, sample_rate = _load_audio(path)
        start_seconds = max(0.0, float(start_seconds))
        end_seconds = float(end_seconds)
        start_sample = round(start_seconds * sample_rate)
        end_sample = waveform.shape[1] if end_seconds < 0 else min(round(end_seconds * sample_rate), waveform.shape[1])

        if start_sample >= waveform.shape[1]:
            duration = waveform.shape[1] / sample_rate
            raise ValueError(f"[CoachBate] H3 audio slice starts at {start_seconds}s, after the {duration:.3f}s file ends: {path}")
        if end_sample <= start_sample:
            raise ValueError(
                f"[CoachBate] H3 audio slice end ({end_seconds}s) must be after start ({start_seconds}s): {path}"
            )

        waveform = waveform[:, start_sample:end_sample].contiguous()
        return ({"waveform": waveform.unsqueeze(0), "sample_rate": sample_rate},)
