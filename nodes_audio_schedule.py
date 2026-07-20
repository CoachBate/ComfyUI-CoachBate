"""
CoachBate audio schedule node.

Stores per-character voice references and speaking time windows so that
CoachBateAudioSamplerPass (implemented after Layer 1 validation) can run
targeted audio-only refinement passes for each character.

Data format stored in COACHBATE_AUDIO_SCHEDULE:
  {
    "entries": [
      {
        "adapter_name": str,        # must match CoachBateLTXLoRALoader adapter_name
        "reference_audio": dict,    # ComfyUI AUDIO dict {waveform, sample_rate}
        "start_sec": float,         # speaking window start (seconds)
        "end_sec": float,           # speaking window end (seconds)
        "voice_strength": float,    # passed to LTXVReferenceAudio identity_guidance_scale
      },
      ...
    ],
    "framerate": int,               # for sec→frame conversion in the sampler pass
  }
"""

import logging

log = logging.getLogger("coachbate.audio_schedule")

COACHBATE_AUDIO_SCHEDULE_TYPE = "COACHBATE_AUDIO_SCHEDULE"


class CoachBateAudioSchedule:
    """
    Define when each character speaks and provide their voice reference clip.

    The reference_audio clip is encoded by LTXVReferenceAudio during the audio
    sampler pass — ~5 seconds of clear speech gives the best voice identity.

    start_sec / end_sec mark the portion of the generated video where that
    character is speaking. Non-overlapping windows are recommended.
    """

    RETURN_TYPES = (COACHBATE_AUDIO_SCHEDULE_TYPE,)
    RETURN_NAMES = ("audio_schedule",)
    FUNCTION = "build_schedule"
    CATEGORY = "CoachBate/LTX-FreeFuse"
    DESCRIPTION = (
        "Defines when each character speaks in the generated video and provides their voice reference clip. "
        "Each entry maps a character adapter name to a reference audio clip and a speaking time window. "
        "Non-overlapping time windows are recommended; overlapping regions will blend voice references."
    )
    RETURN_TOOLTIPS = (
        "Audio schedule data containing voice references and speaking windows — pass to the audio sampler node.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "framerate": ("INT", {
                    "default": 24, "min": 1, "max": 120,
                    "tooltip": "Must match the framerate used in your video generation workflow.",
                }),
                # Character 1 (required)
                "char1_name": ("STRING", {
                    "default": "char1",
                    "tooltip": "Must match the adapter_name in CoachBateLTXLoRALoader for character 1.",
                }),
                "char1_audio": ("AUDIO", {
                    "tooltip": "Voice reference clip for character 1 (~5 seconds of clear speech).",
                }),
                "char1_start_sec": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 600.0, "step": 0.1,
                    "tooltip": "Second in the video where character 1 starts speaking.",
                }),
                "char1_end_sec": ("FLOAT", {
                    "default": 5.0, "min": 0.0, "max": 600.0, "step": 0.1,
                    "tooltip": "Second in the video where character 1 stops speaking.",
                }),
                "char1_voice_strength": ("FLOAT", {
                    "default": 3.0, "min": 0.0, "max": 10.0, "step": 0.1,
                    "tooltip": "Voice identity guidance scale for character 1 (3.0 = default, higher = stronger identity).",
                }),
                # Character 2 (required)
                "char2_name": ("STRING", {
                    "default": "char2",
                    "tooltip": "Must match the adapter_name in CoachBateLTXLoRALoader for character 2.",
                }),
                "char2_audio": ("AUDIO", {
                    "tooltip": "Voice reference clip for character 2 (~5 seconds of clear speech).",
                }),
                "char2_start_sec": ("FLOAT", {
                    "default": 5.0, "min": 0.0, "max": 600.0, "step": 0.1,
                    "tooltip": "Second in the video where character 2 starts speaking.",
                }),
                "char2_end_sec": ("FLOAT", {
                    "default": 10.0, "min": 0.0, "max": 600.0, "step": 0.1,
                    "tooltip": "Second in the video where character 2 stops speaking.",
                }),
                "char2_voice_strength": ("FLOAT", {
                    "default": 3.0, "min": 0.0, "max": 10.0, "step": 0.1,
                    "tooltip": "Voice identity guidance scale for character 2 (3.0 = default, higher = stronger identity).",
                }),
            },
            "optional": {
                # Character 3
                "char3_name": ("STRING", {
                    "default": "",
                    "tooltip": "Must match the adapter_name in CoachBateLTXLoRALoader for character 3. Leave blank to skip.",
                }),
                "char3_audio": ("AUDIO", {
                    "tooltip": "Voice reference clip for character 3 (~5 seconds of clear speech).",
                }),
                "char3_start_sec": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 600.0, "step": 0.1,
                    "tooltip": "Second in the video where character 3 starts speaking.",
                }),
                "char3_end_sec": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 600.0, "step": 0.1,
                    "tooltip": "Second in the video where character 3 stops speaking.",
                }),
                "char3_voice_strength": ("FLOAT", {
                    "default": 3.0, "min": 0.0, "max": 10.0, "step": 0.1,
                    "tooltip": "Voice identity guidance scale for character 3.",
                }),
                # Character 4
                "char4_name": ("STRING", {
                    "default": "",
                    "tooltip": "Must match the adapter_name in CoachBateLTXLoRALoader for character 4. Leave blank to skip.",
                }),
                "char4_audio": ("AUDIO", {
                    "tooltip": "Voice reference clip for character 4 (~5 seconds of clear speech).",
                }),
                "char4_start_sec": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 600.0, "step": 0.1,
                    "tooltip": "Second in the video where character 4 starts speaking.",
                }),
                "char4_end_sec": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 600.0, "step": 0.1,
                    "tooltip": "Second in the video where character 4 stops speaking.",
                }),
                "char4_voice_strength": ("FLOAT", {
                    "default": 3.0, "min": 0.0, "max": 10.0, "step": 0.1,
                    "tooltip": "Voice identity guidance scale for character 4.",
                }),
            },
        }

    def build_schedule(
        self,
        framerate,
        char1_name, char1_audio, char1_start_sec, char1_end_sec, char1_voice_strength,
        char2_name, char2_audio, char2_start_sec, char2_end_sec, char2_voice_strength,
        char3_name="", char3_audio=None, char3_start_sec=0.0, char3_end_sec=0.0, char3_voice_strength=3.0,
        char4_name="", char4_audio=None, char4_start_sec=0.0, char4_end_sec=0.0, char4_voice_strength=3.0,
    ):
        entries = []

        for name, audio, start, end, strength in [
            (char1_name, char1_audio, char1_start_sec, char1_end_sec, char1_voice_strength),
            (char2_name, char2_audio, char2_start_sec, char2_end_sec, char2_voice_strength),
            (char3_name, char3_audio, char3_start_sec, char3_end_sec, char3_voice_strength),
            (char4_name, char4_audio, char4_start_sec, char4_end_sec, char4_voice_strength),
        ]:
            if not name.strip() or audio is None:
                continue
            if end <= start:
                log.warning(
                    "[CoachBate AudioSchedule] '%s': end_sec (%.1f) <= start_sec (%.1f) — skipping",
                    name, end, start,
                )
                continue
            entries.append({
                "adapter_name": name.strip(),
                "reference_audio": audio,
                "start_sec": float(start),
                "end_sec": float(end),
                "voice_strength": float(strength),
            })
            log.info(
                "[CoachBate AudioSchedule] '%s': %.1f – %.1f s, strength=%.1f",
                name, start, end, strength,
            )

        if not entries:
            raise ValueError("[CoachBate AudioSchedule] No valid entries — check names, audio inputs, and time windows")

        # Warn on overlapping windows
        for i, a in enumerate(entries):
            for b in entries[i + 1:]:
                if a["start_sec"] < b["end_sec"] and b["start_sec"] < a["end_sec"]:
                    log.warning(
                        "[CoachBate AudioSchedule] Overlapping windows: '%s' (%.1f-%.1f) and '%s' (%.1f-%.1f). "
                        "Overlapping regions will blend voice references.",
                        a["adapter_name"], a["start_sec"], a["end_sec"],
                        b["adapter_name"], b["start_sec"], b["end_sec"],
                    )

        schedule = {"entries": entries, "framerate": framerate}
        return (schedule,)
