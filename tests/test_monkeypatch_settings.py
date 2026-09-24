import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from monkeypatch_settings import (  # noqa: E402
    DEFAULT_MONKEYPATCH_SETTINGS,
    load_monkeypatch_settings,
    save_monkeypatch_settings,
    validate_monkeypatch_settings,
)


def test_missing_and_malformed_files_use_enabled_defaults():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "settings.json")
        assert load_monkeypatch_settings(path) == DEFAULT_MONKEYPATCH_SETTINGS
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("not json")
        assert load_monkeypatch_settings(path) == DEFAULT_MONKEYPATCH_SETTINGS


def test_partial_file_preserves_defaults():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "settings.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"gemma_env_key": False, "whisper_dtype": "no"}, fh)
        settings = load_monkeypatch_settings(path)
        assert settings["vhs_video_combine"] is True
        assert settings["gemma_env_key"] is False
        assert settings["whisper_dtype"] is True


def test_validation_rejects_incomplete_or_invalid_settings():
    for settings in (
        None,
        {},
        {**DEFAULT_MONKEYPATCH_SETTINGS, "extra": True},
        {**DEFAULT_MONKEYPATCH_SETTINGS, "whisper_dtype": 1},
    ):
        try:
            validate_monkeypatch_settings(settings)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid settings: {settings!r}")


def test_save_round_trip():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "settings.json")
        expected = {
            "vhs_video_combine": False,
            "gemma_env_key": True,
            "whisper_dtype": False,
            "vhs_preview_loop": True,
        }
        assert save_monkeypatch_settings(path, expected) == expected
        assert load_monkeypatch_settings(path) == expected


if __name__ == "__main__":
    test_missing_and_malformed_files_use_enabled_defaults()
    test_partial_file_preserves_defaults()
    test_validation_rejects_incomplete_or_invalid_settings()
    test_save_round_trip()
    print("OK — all monkeypatch settings checks passed")
