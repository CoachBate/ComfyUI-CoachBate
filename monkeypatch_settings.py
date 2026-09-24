"""Persistent settings for optional third-party compatibility patches."""

import json
import os
import tempfile


DEFAULT_MONKEYPATCH_SETTINGS = {
    "vhs_video_combine": True,
    "gemma_env_key": True,
    "whisper_dtype": True,
    "vhs_preview_loop": True,
    "h3_facerefine": False,
}


def load_monkeypatch_settings(path: str):
    """Load settings, keeping safe default-on values for missing or invalid entries."""
    settings = dict(DEFAULT_MONKEYPATCH_SETTINGS)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
    except (FileNotFoundError, OSError, ValueError, TypeError, UnicodeError):
        return settings

    if not isinstance(stored, dict):
        return settings
    for key in settings:
        if isinstance(stored.get(key), bool):
            settings[key] = stored[key]
    return settings


def validate_monkeypatch_settings(settings):
    """Require one boolean value for every supported compatibility patch."""
    if not isinstance(settings, dict):
        raise ValueError("settings must be an object")
    unknown = set(settings) - set(DEFAULT_MONKEYPATCH_SETTINGS)
    if unknown:
        raise ValueError(f"unknown monkeypatch setting: {sorted(unknown)[0]}")
    missing = set(DEFAULT_MONKEYPATCH_SETTINGS) - set(settings)
    if missing:
        raise ValueError(f"missing monkeypatch setting: {sorted(missing)[0]}")
    if any(not isinstance(settings[key], bool) for key in DEFAULT_MONKEYPATCH_SETTINGS):
        raise ValueError("monkeypatch settings must be booleans")
    return {key: settings[key] for key in DEFAULT_MONKEYPATCH_SETTINGS}


def save_monkeypatch_settings(path: str, settings):
    """Atomically persist validated compatibility-patch settings."""
    normalized = validate_monkeypatch_settings(settings)
    directory = os.path.dirname(path) or "."
    fd, temporary_path = tempfile.mkstemp(
        dir=directory,
        prefix=os.path.basename(path) + ".",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(normalized, fh, indent=2)
            fh.write("\n")
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise
    return normalized
