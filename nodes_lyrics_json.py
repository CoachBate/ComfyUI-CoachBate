"""
CoachBate Lyrics JSON parser node.

Parses a JSON string with the expected music metadata shape and exposes
individual fields as ComfyUI outputs.
"""

import json


class _LyricsUnavailable(ValueError):
    """Anything that means 'no usable song data this run' - caught in execute() to block downstream."""

    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or []


class CoachBateLyricsJSONParser:
    RETURN_TYPES = ("STRING", "INT", "STRING", "COMBO", "STRING", "FLOAT", "STRING")
    RETURN_NAMES = ("lyrics", "bpm", "key", "keyscale", "caption", "duration_sec", "combined")
    FUNCTION = "execute"
    CATEGORY = "CoachBate"
    DESCRIPTION = (
        "Parses a JSON string containing music metadata and exposes each field as a typed output. "
        "Accepts raw JSON or a markdown code block (```json...```). "
        "Required fields: lyrics, bpm, key, caption, duration_sec. Optional fields: keyscale, "
        "timesignature (default 4), language (default 'en'). "
        "The 'combined' output is the AI-Toolkit ACE-Step *.caption format "
        "(<CAPTION>/<LYRICS>/<BPM>/<KEYSCALE>/<TIMESIGNATURE>/<DURATION>/<LANGUAGE> tags)."
    )
    RETURN_TOOLTIPS = (
        "Song lyrics text.",
        "Beats per minute as an integer.",
        "Musical key string (e.g. 'C major').",
        "Key with scale (e.g. 'C major' or 'C minor'); defaults to the key field if keyscale is not provided.",
        "Short description or caption for the music.",
        "Track duration in seconds.",
        "All fields wrapped as an AI-Toolkit ACE-Step *.caption file (tagged <CAPTION>, <LYRICS>, <BPM>, <KEYSCALE>, <TIMESIGNATURE>, <DURATION>, <LANGUAGE> blocks).",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "JSON": ("STRING", {
                    "default": '{\n  "lyrics": "",\n  "bpm": 120,\n  "key": "C major",\n  "caption": "",\n  "duration_sec": 180\n}',
                    "multiline": True,
                    "tooltip": "JSON string containing lyrics, bpm, key, caption, duration_sec, and optional keyscale, timesignature, language.",
                }),
            },
        }

    def execute(self, JSON):
        # Every failure mode here - LLM returned prose instead of JSON, an object with no
        # "lyrics", or an explicit {"found": false} - is logged and then blocks downstream:
        # ExecutionBlocker(None) makes consumers skip without raising, so a batch run keeps going
        # instead of dying on one bad song.
        from comfy_execution.graph_utils import ExecutionBlocker
        try:
            return self._parse(JSON)
        except _LyricsUnavailable as exc:
            print(f"[CoachBate] Lyrics JSON Parser: skipping downstream - {exc}")
            for line in exc.details:
                print(f"[CoachBate]   {line}")
            return (ExecutionBlocker(None),) * len(self.RETURN_TYPES)

    @staticmethod
    def _extract_json_object(text):
        """Pull the JSON object out of LLM output that may wrap it in a fence or prose."""
        text = (text or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if len(lines) >= 2 and lines[-1].strip() == "```":
                text = "\n".join(lines[1:-1]).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as first_exc:
            # Tolerate leading/trailing chatter: retry on the outermost {...} span.
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end > start:
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    pass
            raise ValueError(f"Invalid JSON input: {first_exc}") from first_exc

    def _parse(self, JSON):
        payload_text = (JSON or "").strip()
        if not payload_text:
            raise _LyricsUnavailable("JSON input is empty")

        try:
            payload = self._extract_json_object(payload_text)
        except ValueError as exc:
            raise _LyricsUnavailable(str(exc), [f"raw input: {payload_text[:500]!r}"]) from exc

        if not isinstance(payload, dict):
            raise _LyricsUnavailable("JSON input must decode to an object", [f"raw input: {payload_text[:500]!r}"])

        # The upstream search step reports {"found": false, "reason": ..., "sources_checked": [...]}
        # when it couldn't get lyrics.
        if payload.get("found") is False:
            searched = payload.get("searched_for") or {}
            target = " - ".join(str(v) for v in (searched.get("artist"), searched.get("title")) if v)
            raise _LyricsUnavailable(
                f"lyrics not found{f' for {target}' if target else ''}: {payload.get('reason', 'no reason given')}",
                [f"checked: {src}" for src in payload.get("sources_checked") or []],
            )

        required_fields = ("lyrics", "bpm", "key", "caption", "duration_sec")
        missing = [field for field in required_fields if field not in payload]
        if missing:
            raise _LyricsUnavailable(f"JSON input is missing required field(s): {', '.join(missing)}")

        lyrics = payload["lyrics"]
        musical_key = payload["key"]
        keyscale = payload.get("keyscale", musical_key)
        caption = payload["caption"]

        if not isinstance(lyrics, str):
            raise _LyricsUnavailable("'lyrics' must be a string")
        if not isinstance(musical_key, str):
            raise _LyricsUnavailable("'key' must be a string")
        if not isinstance(keyscale, str):
            raise _LyricsUnavailable("'keyscale' must be a string when provided")
        if not isinstance(caption, str):
            raise _LyricsUnavailable("'caption' must be a string")

        try:
            bpm = int(payload["bpm"])
        except (TypeError, ValueError) as exc:
            raise _LyricsUnavailable("'bpm' must be an integer") from exc

        try:
            duration_sec = float(payload["duration_sec"])
        except (TypeError, ValueError) as exc:
            raise _LyricsUnavailable("'duration_sec' must be numeric") from exc

        timesignature = payload.get("timesignature", 4)
        try:
            timesignature = int(timesignature)
        except (TypeError, ValueError) as exc:
            raise _LyricsUnavailable("'timesignature' must be an integer when provided") from exc

        language = payload.get("language", "en")
        if not isinstance(language, str):
            raise _LyricsUnavailable("'language' must be a string when provided")

        combined = self._build_caption(
            caption, lyrics, bpm, keyscale, timesignature, duration_sec, language
        )

        return (lyrics, bpm, musical_key, keyscale, caption, duration_sec, combined)

    @staticmethod
    def _build_caption(caption, lyrics, bpm, keyscale, timesignature, duration_sec, language):
        """Render the AI-Toolkit ACE-Step *.caption file format."""
        # The reference files carry DURATION as whole seconds; keep a fraction only if there is one.
        duration = int(duration_sec) if float(duration_sec).is_integer() else duration_sec
        return "\n".join((
            "<CAPTION>",
            caption.strip(),
            "</CAPTION>",
            "<LYRICS>",
            lyrics.strip("\n"),
            "</LYRICS>",
            f"<BPM>{bpm}</BPM>",
            f"<KEYSCALE>{keyscale.strip()}</KEYSCALE>",
            f"<TIMESIGNATURE>{timesignature}</TIMESIGNATURE>",
            f"<DURATION>{duration}</DURATION>",
            f"<LANGUAGE>{language.strip()}</LANGUAGE>",
        ))
