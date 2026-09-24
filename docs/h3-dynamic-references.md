# Dynamic H3 Shot References

Use the same workflow for each episode. Set the Shot Loader's JSON path and the
workflow's output directory. Each queued shot reads the saved JSON again; an
in-flight shot keeps the record it already loaded. Keep native Auto Queue off:
the Shot Loader's increment mode queues the next shot after completion.

The loader appends six outputs without moving the existing 35 outputs:
`h3_ref_video_1` through `h3_ref_video_3` (path strings), followed by
`h3_ref_video_1_settings` through `h3_ref_video_3_settings` (JSON strings).
Connect each pair to a **CoachBate Load H3 Video Path** node. Connect its frames
to the corresponding H3 reference video slot and, optionally, its audio to the
matching reference-video audio slot. Blank paths return no frames or audio and
do not invoke a decoder. No shot-name switches are required.

## Complete Example Shotlist

```json
[
  {
    "shot_id": "001",
    "duration_seconds": 15,
    "video_filename_prefix": "S02E01_001_Arrival",
    "video_prompt": "A person arrives at the front entrance of the building.",
    "status": "REVIEW",
    "batch_enabled": true,
    "video_continuation": false,
    "reference_video_1": "D:/Media/episode02/arrival.mp4",
    "reference_video_1_settings": {
      "force_rate": 24,
      "custom_width": 640,
      "custom_height": 0,
      "skip_first_frames": 48,
      "frame_load_cap": 107,
      "select_every_nth": 1,
      "format": "None",
      "include_audio": false
    },
    "reference_video_2": "",
    "reference_video_3": ""
  }
]
```

Use exact source filenames, including their numbered suffixes. Relative paths
resolve under ComfyUI's output directory; absolute paths are also accepted.
Output destinations remain workflow settings, not part of `video_filename_prefix`.

Clip settings have the same meaning as VHS Load Video Path: `force_rate` is the
resampled FPS (0 keeps source FPS), `skip_first_frames` skips frames at that rate,
`select_every_nth` chooses the stride, and `frame_load_cap` limits the decoded
selection (0 means no cap). Zero width/height preserves the source dimensions.
`format` must name an installed VHS format; use `None` to avoid format-specific
frame constraints. Existing migrated clips retain their original VHS settings.

Source-video audio is excluded unless `include_audio` is explicitly true. Voice
references continue to use the existing `reference_audio_N` path and range fields.
Prompts must describe the references actually supplied; adding a file does not
automatically rewrite a prompt or insert `<Video N>` tags.

`video_continuation` is separate: it uses the exact recorded output of
`continuation_source_shot_id` in the selected run folder. An ordinary reference
video supplies reference footage, not a protected continuation prefix.

The optional `output_audio_tail` object parameterizes the existing trim-and-silent-
tail recipe: `keep_seconds`, `silence_seconds`, `sample_rate`, and `channels`.
Without it, audio passes through unchanged. It only affects generated output,
never the source file. Do not add it unless that edit is intentional.

Supported path aliases are `h3_ref_video_N` (highest precedence) and
`h3_reference_videos` (an array of up to three path strings). An explicit blank
path disables an alias/array fallback. Settings use only the canonical
`reference_video_N_settings` names. Older editorial fields such as
`video_reference_edit` and `reference_video_source` are not automatically activated;
the workflow migration records the exact references that were actually wired.
