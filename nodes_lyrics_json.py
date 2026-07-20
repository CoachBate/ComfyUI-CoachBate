"""
CoachBate Lyrics JSON parser node.

Parses a JSON string with the expected music metadata shape and exposes
individual fields as ComfyUI outputs.
"""

import json


class CoachBateLyricsJSONParser:
    RETURN_TYPES = ("STRING", "INT", "STRING", "COMBO", "STRING", "FLOAT")
    RETURN_NAMES = ("lyrics", "bpm", "key", "keyscale", "caption", "duration_sec")
    FUNCTION = "execute"
    CATEGORY = "CoachBate"
    DESCRIPTION = (
        "Parses a JSON string containing music metadata and exposes each field as a typed output. "
        "Accepts raw JSON or a markdown code block (```json...```). "
        "Required fields: lyrics, bpm, key, caption, duration_sec. Optional field: keyscale."
    )
    RETURN_TOOLTIPS = (
        "Song lyrics text.",
        "Beats per minute as an integer.",
        "Musical key string (e.g. 'C major').",
        "Key with scale (e.g. 'C major' or 'C minor'); defaults to the key field if keyscale is not provided.",
        "Short description or caption for the music.",
        "Track duration in seconds.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "JSON": ("STRING", {
                    "default": '{\n  "lyrics": "",\n  "bpm": 120,\n  "key": "C major",\n  "caption": "",\n  "duration_sec": 180\n}',
                    "multiline": True,
                    "tooltip": "JSON string containing lyrics, bpm, key, optional keyscale, caption, and duration_sec.",
                }),
            },
        }

    def execute(self, JSON):
        payload_text = (JSON or "").strip()
        if not payload_text:
            raise ValueError("[CoachBate] JSON input is empty")

        if payload_text.startswith("```"):
            lines = payload_text.splitlines()
            if len(lines) >= 2 and lines[-1].strip() == "```":
                payload_text = "\n".join(lines[1:-1]).strip()

        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"[CoachBate] Invalid JSON input: {exc}") from exc

        if not isinstance(payload, dict):
            raise ValueError("[CoachBate] JSON input must decode to an object")

        required_fields = ("lyrics", "bpm", "key", "caption", "duration_sec")
        missing = [field for field in required_fields if field not in payload]
        if missing:
            raise ValueError(f"[CoachBate] JSON input is missing required field(s): {', '.join(missing)}")

        lyrics = payload["lyrics"]
        musical_key = payload["key"]
        keyscale = payload.get("keyscale", musical_key)
        caption = payload["caption"]

        if not isinstance(lyrics, str):
            raise ValueError("[CoachBate] 'lyrics' must be a string")
        if not isinstance(musical_key, str):
            raise ValueError("[CoachBate] 'key' must be a string")
        if not isinstance(keyscale, str):
            raise ValueError("[CoachBate] 'keyscale' must be a string when provided")
        if not isinstance(caption, str):
            raise ValueError("[CoachBate] 'caption' must be a string")

        try:
            bpm = int(payload["bpm"])
        except (TypeError, ValueError) as exc:
            raise ValueError("[CoachBate] 'bpm' must be an integer") from exc

        try:
            duration_sec = float(payload["duration_sec"])
        except (TypeError, ValueError) as exc:
            raise ValueError("[CoachBate] 'duration_sec' must be numeric") from exc

        return (lyrics, bpm, musical_key, keyscale, caption, duration_sec)
