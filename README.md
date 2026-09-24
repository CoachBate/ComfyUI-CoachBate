# comfyui-coachbate

Quality-of-life ComfyUI nodes, built for and used daily in CoachBate's own production work.
Should run on any standard ComfyUI setup.

Highlights:
- **Workflows+** — a fast sidebar panel for searching workflows by name *and* by contents, with
  move/rename/delete for both files and folders. Tested against a library of nearly 8,000
  workflows across folders and subfolders. **Drag and drop** your json, audio, image, of video files here and it will open any embedded workflow like it used to
- **Workflow Model Path Auto-Fix** — finds models and LoRAs saved under folder names that don't
  match your local setup and fixes the widget automatically. A configurable override list lets
  you redirect specific files, e.g. swapping an fp8 model for an int8 convRot build.
- **CoachBate Batch Prompter** — a multiline text block that emits **one line per queue run**, so
  your workflow runs once for every line. The lines don't have to be prompts: it outputs a plain
  string, so it works for *any* value you want to iterate over — paste in a list of file names and
  each run can load a different start frame, or drive a LoRA name, a seed label, or a filename
  prefix. `prepend_text` / `append_text` wrap every line, which makes it easy to turn bare file
  names into full paths. Queue everything at once or step through one job at a time.
- **CoachBate Text Preview and Edit** — connect a string to its `any` input; if the upstream
  node is muted, it falls back to whatever you've typed in instead (hence "Edit"). Unlike
  similar preview nodes, that resolved value is saved into the workflow's own metadata, so
  reopening a saved image or video later shows exactly which dynamically generated prompt
  was used.

---

## Installation

Available in **ComfyUI Manager** — search for "CoachBate".

Or install manually:

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/CoachBate/ComfyUI-CoachBate.git
```

Restart ComfyUI. Nodes appear under **CoachBate** in the Add Node menu.

---

## Nodes

| Node                                                        | Description                                                                                                                                                                                                                        |
|-------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [Workflows+](#workflows-sidebar-panel)                      | Fast virtualized replacement for the built-in Workflows sidebar — search, sort, pin, manage thousands of workflow files without the browser hanging. Drag and drop files onto the workflows+ panel to open their embedded workflow |
| [Batch Prompter](#coachbate-batch-prompter)                 | Queue one job per line from a multiline text block — prompts, file names, or any other string you want to iterate over. All at once, one at a time, or in random order                                                             |
| [Text Preview and Edit](#coachbate-text-preview-and-edit)   | Editable text node that also displays and passes through any connected value                                                                                                                                                       |
| [Numbered Text](#coachbate-numbered-text)                   | Multiline text input with a line-number gutter; passes the full text as a STRING                                                                                                                                                   |
| [Video Combine](#coachbate-video-combine)                   | VHS Video Combine wrapper that strips API keys from video metadata before saving                                                                                                                                                   |
| [Strip API Key Metadata](#coachbate-strip-api-key-metadata) | Removes API key fields from video metadata                                                                                                                                                                                         |
| [Load Videos With Audio](#coachbate-load-videos-with-audio) | Load all the videos in a folder including their audio so they can be edited or saved to new video.                                                                                                                                 |
| [Lyrics JSON Parser](#coachbate-lyrics-json-parser)         | Parses a custom json format for lyrics/timing that an LLM creates                                                                                                                                                                  |
| [Shot Loader](#coachbate-shot-loader)                       | Drives a `shotlist.json` through Auto Queue one shot per run                                                                                                                                                                       |
| [Video Prompt From Metadata](#coachbate-video-prompt-from-metadata) | Reads the prompt a ComfyUI-generated mp4 was made with, out of its embedded workflow |
| [H3 Subject Refine](#coachbate-h3-subject-refine)           | Enhance one subject in a MiniMax H3 video - a face, a penis, anything a YOLO model can find and a LoRA can draw: track it per frame, crop it up to a canvas, let H3 re-render the crops as img2img, paste the result back through the segmentation silhouette. Loops once per subject. |
| [Upscale and Frame Interpolator TensorRT](#coachbate-upscale-and-frame-interpolator-tensorrt) | Runs full TensorRT-upscale and native FILM passes, then writes an H.264/H.265 video with audio |

---

## CoachBate H3 Subject Refine

MiniMax H3 renders a subject badly when it is a small part of the frame - faces are the famous
case, but the same is true of anything else. These nodes fix that the way
[ComfyUI-H3-FaceRefine](https://github.com/Carasibana/ComfyUI-H3-FaceRefine) fixes faces, for any
subject: detect it on every frame with a YOLO model, follow one subject through the clip, crop it so
it fills a 768 canvas, let H3 re-generate the crops as img2img at low denoise (with the LoRA that
knows the subject), and warp each refined crop back onto the exact place it came from, pasting only
through the subject's segmentation silhouette.

The tracking, cropping, latent injection and stitch are a fork of ComfyUI-H3-FaceRefine (MIT,
credited in the source). What is different: subject-neutral vocabulary and no face-only machinery
(no InsightFace / identity matching / face picker, and none of those dependencies); the crop is
sized from the box's longest side so a subject lying sideways is not cut off; off-grid clips are
padded by repeating the last frame *before* VAE encoding (H3's VAE truncates to its 17k+5 frame
grid, and padding the latent afterwards turned the last frames into a different scene); the
detector's predictor is rebuilt per run (a stale one silently stopped detecting); paste masks come
from the segmentation model at several scales (a segm model trained on medium shots scores a
subject that fills half the crop near zero - shrunk to half size it scores 0.8); and the crop is
resampled back with an antialiased bicubic filter rather than plain bilinear.

### Nodes

| Node | Purpose |
|---|---|
| **H3 Subject Track + Crop** | Detect per frame, pick the subject (`largest`, `left_most`, `centre_most`, `right_most`, ..., or `select_override` from a loop), smooth the trajectory, emit a constant-size crop batch plus the `transform` the stitch needs. `crop_size_from = longest_side`, `cut_detection` per shot. |
| **H3 Inject Video Latent (img2img)** | Encode the crops into the video stream of H3's AV latent (the missing video-to-video path); pads off-grid clips by repeating the last frame first. |
| **H3 Per-Subject Denoise** | Per-frame noise-mask strength by subject size. Defaults are flat (1.0), so `denoise` on the scheduler means what it says. |
| **H3 Subject Stitch Back** | Warp the refined crops back onto their float boxes, colour match, feather, composite through `masks`; `resample = bicubic_antialias`. |
| **H3 Segm Mask (YOLO)** | The paste masks: runs the segm model on the input crops and on the refined crops (`mask_source = union`, so neither the old outline nor the new one is clipped), at several scales, temporally smoothed; frames it misses fall back to an ellipse on the tracked box. |
| **H3 Subject Count** | How many subjects the clip shows (largest per-frame count seen on `min_fraction` of the frames, capped by `max_subjects`) - drives the loop. |
| **H3 Loop Subject** | Maps a loop round to the subject it follows: left / centre / right for 2-3 subjects, size rank otherwise. |

### Blueprints

Two subgraph blueprints wrap all of that with a handful of inputs (`images`, `model`, `clip`, `vae`,
`audio_vae`, optional `ref_image`, `prompt`, `detector`, `confidence`, `select`, `steps`, `denoise`,
`crop_factor`, `mask_source`, `mask_dilation`, `feather`, `canvas_size`, `detect_scales`, ...):

- **H3 Subject Refine** - one subject per pass (`select_index` picks which).
- **H3 Subject Refine xN** - counts the subjects and runs one full pass per subject
  (`max_subjects` caps it, `subject_order` = `left_to_right` or `size`). One subject = one pass.

Example workflow: `H3 Subject Refine - Existing Video` (video in, prompt recovered from the clip's
own metadata, refine, video out with the original audio). Dependencies beyond ComfyUI core:
this pack, [ComfyUI-Pixaroma](https://github.com/pixaroma/ComfyUI-Pixaroma) (`Loop Start` /
`Loop End`), [VideoHelperSuite](https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite).

### Models

| What | File | Goes in | Source |
|---|---|---|---|
| penis segmentation (detector **and** paste mask) | `CockAndBallYolo8x.pt` | `models/ultralytics/segm/` | [ashllay/YOLO_Models](https://huggingface.co/ashllay/YOLO_Models/tree/main/segm) |
| other subjects - faces, hands, breasts, vulva ... | e.g. `face_yolov8m-seg_60.pt`, `PitHandDetailer-v1-seg.pt`, `vagina-v3.2.pt` | `models/ultralytics/segm/` (bbox-only models in `bbox/` still track, but paste as an ellipse) | same repo, `segm/` and `bbox/` |
| MiniMax H3 model, text encoder, video + audio VAE, a turbo LoRA | | the usual H3 folders | [Comfy-Org/MiniMax-H3](https://huggingface.co/Comfy-Org/MiniMax-H3), [Kijai/MiniMax-H3_comfy](https://huggingface.co/Kijai/MiniMax-H3_comfy/tree/main/loras) |
| a LoRA that knows the subject | | `models/loras/` | H3's base model does not draw genitals well; the refine pass regenerates from the model you pass in, so pass one with the LoRA applied |

`models/ultralytics/{bbox,segm}` are registered by
[ComfyUI-Impact-Subpack](https://github.com/ltdrdata/ComfyUI-Impact-Subpack); the nodes also look
in those folders directly if it is not installed.

### Settings worth knowing

- `denoise` 0.35-0.55 adds detail without changing the shot; higher rebuilds more and the subject
  starts drifting against the body. H3 is flow-matching with a large sigma shift, so SDXL-style
  values do not transfer.
- `steps` must follow the turbo LoRA in the model you pass in (8-step -> 8, 4-step -> 4). Not
  enforced; a mismatch only costs quality. Unknown? 8.
- `crop_factor` 1.5 puts the subject at ~65% of the crop - more magnification, which is the point.
  Use 2.0-2.5 if the paste seam needs more context around it.
- `canvas_size` 768 is H3's native short edge; cost is canvas^2 x frames.
- `confidence` is per video; watch the track preview (green = detected, red = interpolated).
- Multiple similar subjects close together (three men side by side) are told apart by position,
  not by size rank - that is what `subject_order = left_to_right` is for. Occluded stretches keep
  their original pixels.

## H3-FaceRefine fixes (monkeypatch, off by default)

Only relevant if you also run [ComfyUI-H3-FaceRefine](https://github.com/Carasibana/ComfyUI-H3-FaceRefine)
itself. Switch `h3_facerefine` in the CoachBate patch settings applies the same three fixes to that
pack at startup without editing it (predictor reset, grid padding before encode, `crop_size_from`),
and hands its `H3InjectVideoLatent` node id back to it if another pack has registered a copy over
it. `docs/h3-facerefine-upstream.patch` is the same set as a diff for an upstream PR.

## CoachBate Upscale and Frame Interpolator TensorRT

This optional integration runs TensorRT upscaling first and native ComfyUI FILM frame interpolation
second, then sends the completed frames to FFmpeg. Select the upscale and FILM models, upscale
precision, final size, interpolation multiplier, codec, pixel format, CRF, and output filename on
one node. Supply an `AUDIO` input and the node writes the finished MP4 itself. Upscaler model
download, engine build, and engine load remain lazy and occur only when the node executes. Workflow
cancellation is handled during engine building, frame processing, and encoding.

The custom width and height controls appear only when `resize_to` is `custom`. Obsolete TensorRT
RIFE controls remain serialized but hidden so existing saved workflows retain their widget layout.

For best throughput, the node follows the same stage order as separate optimized nodes: one full
TensorRT upscale pass, one full FILM pass, then video encoding. Before the upscale and again between
the upscale and FILM stages it uses ComfyUI model management to empty the CUDA cache, unload other
loaded models, and run Python garbage collection. This maximizes the VRAM available to each stage
on a 32 GB RTX 5090 and avoids repeatedly swapping TensorRT and FILM in and out of VRAM.

The completed upscaled and interpolated tensors use system RAM and may fall back to the Windows
pagefile under memory pressure. The node returns the saved `VHS_FILENAMES` value and the exact FPS
used by FFmpeg:

- `preserve duration` raises output FPS to match the generated frame count.
- `slow motion` keeps the source FPS, extending playback duration.

`H.264 / AVC` uses VHS's `libx264` MP4 format and `H.265 / HEVC` uses `libx265`. `CRF` is the
constant-rate-factor quality control: lower values increase quality and file size. Both 8-bit
`yuv420p` and 10-bit `yuv420p10le` output are available.

### Main Inputs

| Input | Default | Description |
|-------|---------|-------------|
| `images` | required | Source video frames to upscale and interpolate |
| `audio` | required | ComfyUI `AUDIO` stream muxed into the finished MP4 |
| `resize_to` | upstream default | Final upscale preset; selecting `custom` reveals width and height |
| `rife_multiplier` | `2` | FILM output intervals per source interval; `1` bypasses FILM entirely |
| `source_fps` | `24` | FPS of the incoming frames and basis for calculated output FPS |
| `playback_mode` | `preserve duration` | Raises output FPS after interpolation or retains source FPS for slow motion |
| `codec` | `H.265 / HEVC` | Selects VHS `libx265` or `libx264` MP4 encoding |
| `pixel_format` | `yuv420p10le` | Selects 10-bit or broadly compatible 8-bit YUV output |
| `crf` | `16` | Encoding quality; lower is higher quality and a larger file |
| `filename_prefix` | `CoachBate_TensorRT` | Output filename and optional subfolder prefix |
| `save_output` | `true` | Saves under ComfyUI output; false writes a temporary preview |
| `save_metadata` | `true` | Embeds sanitized workflow metadata through Video Helper Suite |
| `pingpong` | `false` | Appends the generated frames in reverse order without repeating endpoints |
| `trim_to_audio` | `false` | Ends the muxed MP4 at the shorter of the video and supplied audio |

Native ComfyUI FILM uses FP16 when supported, caches features shared by adjacent pairs, evaluates
multiple timesteps together, and automatically retries smaller timestep batches after a CUDA
out-of-memory response. The combined node deliberately does not split an ordinary video into
alternating upscale/FILM chunks because that repeatedly reloads model state and is substantially
slower than two full passes.

When the effective multiplier is `1` for every interval, the FILM checkpoint is not loaded and no
FILM inference runs. The output contains one upscaled frame per input frame and FFmpeg receives the
unchanged `source_fps` in both playback modes.

### Outputs

| Output | Type | Description |
|--------|------|-------------|
| `filenames` | `VHS_FILENAMES` | Saved MP4 paths for preview or downstream file operations |
| `output_fps` | `FLOAT` | Exact frame rate supplied to FFmpeg |

The node replaces the separate Video Combine step. Existing workflows should connect `audio`
directly to this node and remove the old image connection to Video Combine. The output is a saved
video filename value rather than an `IMAGE` batch.

`filename_prefix` supports the same ComfyUI/VHS text replacements as Video Combine, including date
tokens such as `%date:yyMMdd%`, built-in date/time tokens such as `%year%`, and widget references
such as `%node_name.widget_name%`. Date replacement also has a backend fallback for API-submitted
workflows where the frontend serialization hook is not involved.

Ping-pong behavior is delegated to Video Helper Suite after interpolation.

Requires `ComfyUI-Upscaler-Tensorrt`, ComfyUI's native frame-interpolation nodes, and a FILM
checkpoint in the configured `frame_interpolation` model folder. No RIFE nodepack is required.

---

## CoachBate Shot Loader

### Inputs

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| `json_path` | STRING | `""` | Absolute path to your `shotlist.json` |
| `shot_index` | INT | `0` | Seeds the starting position on first run. In `increment` mode, also jumps forward if set higher than the current position. Updated automatically after each shot. |
| `mode` | ENUM | `increment` | `increment` advances forward one shot per run; `decrement` advances backward; `fixed` always outputs the same shot |

### Outputs

| Output | Type | Description |
|--------|------|-------------|
| `video_prompt` | STRING | Full generation prompt for this shot |
| `duration_seconds` | INT | Clip length — multiply by fps for LTX frame count |
| `shot_id` | STRING | Identifier string from the JSON (e.g. `"001"`) |
| `video_filename_prefix` | STRING | Output filename prefix (e.g. `"001-BATE ENTERS GYM"`) |
| `start_image` | STRING | Path to start-frame reference image (empty string if none) |
| `end_image` | STRING | Path to end-frame reference image (empty string if none) |
| `start_image_prompt` | STRING | Text prompt describing the start frame (empty string if none) |
| `negative_prompt` | STRING | Shot-specific negative prompt text (empty string if not set) |
| `negative_audio_prompt` | STRING | Shot-specific negative audio prompt text (empty string if not set) |
| `total_shots` | INT | Number of non-DONE shots remaining |
| `start_image_strength` | FLOAT | Strength for start image conditioning (0.0 if no image or file missing, else JSON value or 1.0) |
| `end_image_strength` | FLOAT | Strength for end image conditioning (0.0 if no image or file missing, else JSON value or 1.0) |

### Status display

After each execution a status box is painted directly on the node face:

```
3/12  001-BATE ENTERS GYM
16s  ➡️ 002-BATE TRAINS
```

The second line shows `[last]` when the final active shot has been loaded.

### Toast notifications

| Colour | Trigger |
|--------|---------|
| Blue (info) | Shot loaded — "Shot 3/12: 001-BATE ENTERS GYM" |
| Red (error) | Last shot loaded — "Shot 12/12: ... — last shot!" |
| Orange (warn) | Shot skipped — start/end image file not found on disk |

---

## shotlist.json format

The file can be a bare array or wrapped in an object with a `shots` key — both are accepted:

```json
[
  {
    "shot_id": "001",
    "video_filename_prefix": "001-BATE ENTERS GYM",
    "status": "DONE",
    "duration_seconds": 16,
    "scene": "gym_interior",
    "start_image": "C:/path/to/images/gymdoor.png",
    "start_image_strength": 0.85,
    "end_image": "",
    "start_image_prompt": "",
    "video_prompt": "A realistic, cinematic sports-drama scene..."
  },
  {
    "shot_id": "002",
    "...": "..."
  }
]
```

Or wrapped:

```json
{
  "shots": [ "..." ]
}
```

### Required fields per shot

| Field | Type | Description |
|-------|------|-------------|
| `shot_id` | string | Identifier string |
| `video_filename_prefix` | string | Output filename prefix |
| `duration_seconds` | int | Clip length in seconds |
| `video_prompt` | string | Generation prompt |

### Optional fields per shot

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `start_image` | string | `""` | Path to start-frame image |
| `end_image` | string | `""` | Path to end-frame image |
| `start_image_prompt` | string | `""` | Text prompt for start frame |
| `start_image_strength` | float | `1.0` | Strength for start image (ignored if no image) |
| `end_image_strength` | float | `1.0` | Strength for end image (ignored if no image) |
| `negative_prompt` | string | `""` | Shot-specific negative prompt |
| `negative_audio_prompt` | string | `""` | Shot-specific negative audio prompt |
| `status` | string | — | Set to `"DONE"` to skip a shot permanently |
| `scene` | string | — | Informational only; not returned by the node |

Fields not listed above are loaded and ignored — use them freely for your own production tracking.

---

## Looping through all shots automatically

The node uses a class-level counter (`stored_index`) that advances one step each time the node
executes, matching the behaviour of ComfyUI's built-in **JSON Array Iterator** node.

Shots with `"status": "DONE"` are skipped automatically. If **all** shots are marked DONE the
node raises an error rather than looping forever.

To loop through the entire shotlist without manual intervention:

1. Set `mode` to `increment`.
2. Open the queue panel drop-down (next to the **Queue** button) and select **Auto Queue**.
3. Click **Queue** once. The node will run shot 0, then shot 1, and so on until it wraps back
   to 0 (or you stop the queue).

---

## HTTP API

A lightweight REST endpoint is registered on ComfyUI's server at startup.

### `POST /coachbate/skip`

Advances `stored_index` past the current shot so the **next** Auto Queue run picks up the
following non-DONE shot — without requiring a full workflow re-run.

**Request body:**
```json
{ "current_index": 3, "total": 12 }
```

Both values are available from the node's last execution UI output (`array_idx` and `total`).
The endpoint is idempotent: if `stored_index` has already moved past `current_index` (e.g. a
double-click) the advance is skipped.

**Response:**
```json
{ "stored_index": 4 }
```

---

## Workflows+ sidebar panel

A replacement for ComfyUI's stock **Workflows** sidebar tab, built because the
stock tab hangs the browser once your `workflows` folder holds thousands of
files. Registers as its own tab (**Workflows+**) alongside the stock one —
nothing about the built-in tab is modified.

The panel is enabled by default. To hide it, turn off **Enable Workflows+ panel**
under **Settings → CoachBate → Workflows+**, then reload ComfyUI. The existing
extension-level toggle remains available under **Settings → Extension** as well.

**Works with any ComfyUI hosting setup** (local, WSL, Linux, or a remote/cloud
instance) — the panel is pure browser JS and never touches the local
filesystem directly. Every action (listing, search, open, rename/move/copy/
delete, the Contains scan) goes through ComfyUI's own HTTP API, so it reads
and writes whatever `workflows` folder the *ComfyUI server* can see — not the
machine your browser happens to be running on. If you can load the ComfyUI
UI in a browser, the panel works exactly the same way, no matter where the
server is.

- **Fast at any scale** — the file list and folder tree are virtualized, so
  opening the panel and expanding large folders stays instant even with
  several thousand workflows.
- **Only real workflow files show up** — `.json` workflows and `.png` images
  (which can carry an embedded workflow) are listed; everything else
  ComfyUI's file listing returns (`desktop.ini`, `.bak` backups, `Thumbs.db`,
  editor swap files, ...) is filtered out. Clicking a `.png` entry extracts
  and opens its embedded workflow the same way dropping it does.
- **Name search** with `AND` / `OR` operators and phrase matching:
  - `black cat` (bare words) matches the literal, contiguous phrase — it
    will **not** match "black cute cat".
  - `"black cat"` (quoted) behaves the same as the bare phrase above.
  - `black AND cat` matches both words independently, anywhere in the name.
  - `black OR cat` matches either word.
  - `AND` / `OR` are recognized only in **uppercase** — lowercase "and"/"or"
    in a workflow name is treated as ordinary text.
- **Contains search** — toggle to **Contains** and press Enter to search
  *inside* every workflow's JSON (prompts, node titles, values — anything in
  the file), not just the filename. Useful for finding a prompt you used once
  but can't remember which workflow it's in. Runs server-side and can take a
  while over a large library; a progress bar tracks the scan and you can stop
  it early with the **Cancel** button or the **Esc** key.
- **Folder-scoped search** — click a folder in the tree to make it the active
  search scope. A prominent bar appears ("📁 Searching in 'FolderName'") with
  a **✕ Show All** button, and the scoped folder itself is highlighted in the
  tree, so it's always obvious a scope is active. Both Name and Contains
  search are restricted to that folder. To go back to searching everything:
  click **✕ Show All**, or click the scoped folder again (which also
  collapses it). The **Search subfolders** checkbox still applies within the
  scope — checked searches the folder and everything nested under it,
  unchecked restricts to files directly inside it. With no folder selected,
  the checkbox behaves the same way against the whole library.
- **Sort** by Name, Modified, or Created — click a header button to sort,
  click again to reverse.
- **Pinning** — right-click any workflow for a context menu with **Pin** /
  **Unpin**. Pinned workflows float to the top of their own folder (Windows
  Start-menu style), still ordered by whatever sort is active, in their own
  group above the rest. The context menu also has **Reset all pins for this
  folder** and **Reset all pins**.
- **Recent** — a compact popup listing the last 10 workflows you opened
  (from anywhere — the topbar, this panel, or drag-and-drop), so it doesn't
  take up permanent vertical space.
- **Drag-and-drop** — drop a workflow `.json`, or a `.png`/`.mp4` with an
  embedded workflow, onto the panel to open it. This restores drag-and-drop
  workflow loading, which has been broken on the main canvas.
- **File management** — rename, move, duplicate, and delete workflows
  directly from the panel:
  - **Multi-select**: Ctrl/Cmd-click to toggle individual files, Shift-click
    to select a contiguous range. Right-click a selection for bulk actions
    ("Move 5 files…", "Delete 5 files…", etc.).
  - **Rename** — right-click → Rename, or just start typing in the inline
    field. Enter commits, Esc cancels.
  - **Move** — right-click → "Move to…" opens a folder picker (type to
    filter existing folders, or type a new folder name to create it on the
    fly). You can also **drag a file (or selection) onto a folder row** to
    move it there directly.
  - **Duplicate** — right-click → Duplicate creates "`name` copy.json"
    (incrementing to "copy 2", "copy 3", … if that name is already taken).
  - **Delete** — soft-deletes to a `_trash` subfolder by default (so nothing
    is lost by accident); deleting a file that's *already* in `_trash`
    permanently removes it, with a confirmation dialog either way.
    Right-click the `_trash` folder itself for **Empty Trash**.
  - Renaming/moving/deleting a file automatically keeps its pin and its
    place in the Recent list pointing at the new location (or removes them
    on delete). If the file is currently open, its tab and save target stay
    in sync too.
  - **Folders can be renamed, moved, and deleted too** — right-click a
    folder for the same Rename / Move to… / Delete actions, or drag one
    folder onto another to move it (moving a folder into itself or one of
    its own subfolders is rejected). Delete moves the whole folder — and
    everything inside it — into `_trash` in one step, after a confirmation
    showing how many files are affected; every pin and Recent-list entry
    underneath follows along. There's no way to delete the workflows root
    itself — only real subfolders have a Delete option.

The **Workflows+** tab is pinned to the top of the sidebar, above the
built-in tabs (Assets, Node Library, etc.), since it's meant to replace your
day-to-day use of the stock Workflows tab.

Closing the panel (switching to another sidebar tab) resets it: the search
box, folder scope, and any expanded folders all clear, so the next time you
open it you get a clean, fully-collapsed tree rather than wherever you left
off. Your sort order and "Search subfolders" preference are real settings
and are kept.

No other configuration needed — the tab appears automatically once the pack
is installed.

---

## Workflow Model Path Auto-Fix

A frontend extension (`web/js/workflowModelPathAutoFix.js`) that runs automatically whenever a
workflow loads. Model/LoRA widgets often reference a filename that lives in a different folder,
or under a slightly different name, than the one on your machine — the same workflow shared
between two ComfyUI setups can point at paths that only exist on the original author's machine.
This extension checks every model-type widget against your local model folders and, if the exact
file isn't found, rewrites the widget to the best match it can resolve — so the workflow loads
pointing at a file that actually exists, instead of showing a blank/invalid widget.

- **Override rules** — `workflow_path_autofix_overrides.txt`, in the package root, is an ordered
  list of `search replacement` rules separated by one or more spaces and/or tabs (one per line;
  blank lines and lines starting with `#` are ignored). Search and replacement names cannot contain
  spaces or tabs. Rules are applied first-match-wins, then validated against that widget's actual
  local choices, so a rule that doesn't resolve to a real file is simply skipped. Use this to
  redirect specific files on purpose — e.g. always substituting an `int8_convrot` transformer
  build for the `fp8_scaled` one a workflow was authored with, or mapping an old folder layout
  (`LTXVideo\v2\...`) to a new one.
- **Global settings** — open ComfyUI **Settings → CoachBate → Model Path Replacements** to enable or
  disable auto-fix and add, remove, or edit Source/Target rows. Disabling the feature hides the
  replacement table but preserves its saved rules. Saving rewrites
  `workflow_path_autofix_overrides.txt`; new rules are used immediately without restarting ComfyUI.
- **Disabling it** — open ComfyUI's **Settings → Extension**, filter for
  `CoachBate.WorkflowModelPathAutoFix`, and turn it off if you'd rather leave paths untouched.
- Every replacement it makes is logged server-side (`POST /coachbate/workflow_path_autofix/log`)
  so you can see what changed on load.

### Compatibility patches

CoachBate applies a few default-on compatibility patches to third-party nodes. Open ComfyUI
**Settings → CoachBate → Compatibility Patches** to independently disable the VHS Video Combine
metadata-protection patch, the Gemma API-key environment fallback, or the VRGDG Whisper dtype
correction. Save the settings and restart ComfyUI for the changes to take effect. Enhancements that
only implement CoachBate nodes are intentionally not included in this list.

---

## Typical workflow wiring

```
CoachBateShotLoader
  ├─ video_prompt           → CLIPTextEncode (positive)
  ├─ negative_prompt        → CLIPTextEncode (negative, append to default)
  ├─ duration_seconds       → frame count calculation (fps × duration)
  ├─ video_filename_prefix  → Save Video filename_prefix
  ├─ start_image            → Load Image → LTX start-frame conditioning
  ├─ start_image_strength   → LTX start-frame strength
  ├─ end_image              → Load Image → LTX end-frame conditioning
  └─ end_image_strength     → LTX end-frame strength
```

---

## Release notes

### 2026-09-14

**H3 Subject Refine**

- New node set (fork of ComfyUI-H3-FaceRefine, MIT): `H3 Subject Track + Crop`, `H3 Inject Video
  Latent`, `H3 Per-Subject Denoise`, `H3 Subject Stitch Back`, plus `H3 Segm Mask (YOLO)`,
  `H3 Subject Count`, `H3 Loop Subject`, and the `H3 Subject Refine` / `H3 Subject Refine xN`
  blueprints. See the section above.
- `Video Prompt From Metadata`: recovers the prompt a ComfyUI-made mp4 was generated with.
- Optional `h3_facerefine` compatibility patch (off by default).

### 2026-09-01

**CoachBate global settings**

- Added **Settings → CoachBate → Model Path Replacements** with an enable switch and an editable
  Source/Target table. Saved replacement rules take effect immediately without restarting ComfyUI.
- Model-path override lines now accept one or more spaces and/or tabs between their whitespace-free
  source and target names.
- Added **Enable Workflows+ panel** under **Settings → CoachBate → Workflows+**. The panel remains
  enabled by default; reload ComfyUI after changing this setting.
- Added **Settings → CoachBate → Compatibility Patches** controls for the VHS Video Combine metadata
  protection, Gemma environment-key fallback, and VRGDG Whisper dtype correction. These patches
  remain enabled by default and require a ComfyUI restart after changing them.

### 2026-08-02

**Workflows+ tree ordering and panel-close reset**

- Fixed a visual bug where a folder's own direct files, listed right after
  a (possibly collapsed) subfolder at the same indent, could look like they
  were inside that subfolder. Files now always list before subfolders at
  the same level, so a folder row never appears to trail a file.
- Closing the panel now resets the search box, folder scope, and all
  expanded folders — reopening always starts from a clean, collapsed tree
  instead of wherever you left off. Sort order and the "Search subfolders"
  preference are unaffected.

**Workflows+ file filtering, .png opening, and row alignment**

- Only `.json` and `.png` files are listed now — `desktop.ini`, `.bak`
  files, `Thumbs.db`, and anything else ComfyUI's listing happens to return
  no longer show up as fake workflows.
- Clicking a `.png` entry now actually opens it (extracts the embedded
  workflow), instead of failing — it used to assume every file was raw
  JSON.
- File rows no longer carry two extra icon columns that folder rows don't
  have. A file's name now starts at the same x-position as a folder's name
  at the same depth, instead of sitting ~30px further right and wasting
  horizontal space.

### 2026-07-14

**Batch Prompter — sequential mode, prompt-based numbering, randomize**

- **`queue_all_at_once` toggle.** ON (default) keeps the existing behavior:
  Queue posts every prompt as a separate job up front. OFF runs prompts **one
  at a time** — each job fires only after the previous one finishes (same
  self-advance pattern as Shot Loader), so you can watch results come in and
  tweak the workflow between jobs.
- **`randomize` toggle.** Executes the prompts in random order, never
  repeating, until `max_prompts` is reached (reshuffled on every Run press).
  With `max_prompts = 1` it runs a single randomly chosen prompt — handy for
  injecting one random prompt into a workflow. Works in both queue modes;
  prompts before `starting_number` are excluded from the pool.
- **`starting_number` now counts prompts, not lines.** It matches the gutter
  numbering exactly — blank lines no longer count, so with 19 prompts the
  number can never run past 19 (it used to jump to total-lines + 1).
- **`max_prompts` is honored in sequential mode** as a true total across the
  whole run; when the cap stops mid-text, `starting_number` is left at the
  next prompt so another Run continues from there.
- **When a sequential run finishes, `starting_number` is restored** to the
  value you started with (not reset to 1), so Run again repeats the same range.
- **No more error after the last prompt.** Sequential mode used to queue one
  extra run past the end whose empty-string output crashed downstream nodes
  (image loaders etc.); the sequence now ends on the last real prompt. As a
  backstop, queuing with nothing left halts quietly (like pressing Interrupt)
  instead of emitting `""`.
- **Stop button no longer grows** when the node is resized — extra vertical
  space all goes to the prompts textarea.

**Video Combine — filename cleanup and format defaults**

- Default format changed to `h265-mp4`; default CRF changed to 16 for all
  formats that expose it.
- Leading underscores are stripped from the filename prefix before saving, so
  Mikey-node prefixes like `_my_shot` produce `my_shot.mp4` instead of
  `_my_shot_00001.mp4`.
- The VHS counter suffix (`_00001`) is removed from the saved filename unless a
  file with the desired name already exists on disk, in which case the counter
  is kept to avoid a collision.
- The `-audio` suffix is removed unless "audio" was requested in the filename
  prefix. The audio-muxed file always gets priority on the clean name; the
  silent intermediate keeps its counter if needed.

### 2026-07-13

**Strip API Key Metadata — reliability fixes and folder mode**

- ffmpeg is now found from the system `PATH` when VHS doesn't provide it, so
  the node no longer throws "ffmpeg not found" on systems where VHS isn't
  installed but ffmpeg is.
- Fixed a Windows `OSError` ("cannot move file to a different drive") when the
  ComfyUI temp directory and the output file are on different drives.
- **Folder mode** — pass a folder path to process every supported file inside
  it (`.png`, `.mp4`, `.mov`, `.mkv`, `.webm`) in one node execution.
- Double-quoted paths are accepted (leading/trailing `"` are stripped
  automatically).
- Fixed the Browse button returning 404 — the `/coachbate/browse_media` server
  route was missing.
- Fixed GemmaAPITextEncode `api_key` widget disappearing after a workflow
  reload: the scrubbing logic was clearing structural widget values in addition
  to actual secrets, causing ComfyUI to render the node without an input field.
  Now only genuine `ltxv_…` secrets are cleared.

**Text Preview and Edit — DOM widget rewrite**

- Replaced the string widget with a proper `addDOMWidget` textarea for a stable
  element reference that survives Vue re-renders in Nodes 2.0.
- Adaptive `canvasOnly` keeps the widget out of the sidebar Parameters tab in
  legacy mode while remaining visible in the Nodes 2.0 canvas renderer.
- Added a **Copy Text** button.

**Dialogs — replaced browser `confirm()` with ComfyUI dialog API**

- All confirmation prompts across Shot Loader, Batch Prompter, and Workflows+
  now use `app.extensionManager.dialog.confirm()` — styled consistently with the
  rest of the ComfyUI UI, with `type: "delete"` for destructive actions.
  The old `window.confirm()` calls were browser-native blocking alerts.

**Shot Loader — display fix**

- The "next shot" name shown in the node status box was incorrect in `decrement`
  mode (showed the forward neighbor instead of the backward one).

### 2026-07-05

**Workflows+ folder management, tab position, and clearer scope UX**

- Folders can now be renamed, moved (drag-and-drop or a dialog), and deleted
  (soft-delete to `_trash`, whole subtree preserved) — right-click any
  folder. Moving a folder into itself or a subfolder is rejected; the
  workflows root has no Delete option.
- The folder-scope indicator is now a prominent accent-colored bar with a
  clear **✕ Show All** button, and the scoped folder is highlighted in the
  tree. Clicking the scoped folder again also clears the scope.
- The Workflows+ tab now registers at the top of the sidebar, above Assets
  and the other built-in tabs.

### 2026-07-04

**Workflows+ folder-scoped search**

- Clicking a folder in the tree now scopes both Name and Contains search to
  that folder (with a label + clear button showing the active scope). The
  "Search subfolders" toggle applies within the scope — on = the folder and
  everything nested under it, off = just that folder's direct files.

### 2026-07-03

**Workflows+ file management** (new)

- Rename, move, duplicate, and delete workflows from the panel, with
  Ctrl/Shift multi-select and bulk actions in the right-click menu.
- Move via a folder-picker dialog (type to filter or create a new folder)
  or by dragging a file (or selection) onto a folder row.
- Delete soft-deletes to `_trash` by default; deleting an already-trashed
  file permanently removes it. Right-click `_trash` for Empty Trash.
- Pins and Recent-list entries automatically follow a file through
  rename/move, and are dropped on delete.

### 2026-07-02

**Workflows+ sidebar panel** (new)

- Fast, virtualized replacement for the stock Workflows tab — stays
  responsive with thousands of workflow files.
- Name search with `AND`/`OR` operators (uppercase only) and literal-phrase
  matching; a "Contains" mode searches inside workflow JSON server-side,
  with a progress bar and Cancel/Esc to stop mid-scan.
- "Search subfolders" toggle to scope search to the workflows root only.
- Sort by Name / Modified / Created.
- Windows-style per-folder pinning via right-click, with per-folder and
  global reset actions.
- 10-item Recent list and drag-and-drop of workflow JSON/PNG/MP4 files.

### 2026-06-24

**CoachBate Batch Prompter**

- **Runs from the normal Queue button.** The on-node "Queue All Prompts" button has
  been removed. Pressing ComfyUI's own **Queue** now fans the run out into one queued
  job per non-blank prompt line — no separate button to start a batch.
  - Each job is a full, independent snapshot of the graph taken at queue time (same
    semantics as a native queue), so editing the canvas afterward only affects the
    next run.
  - The fan-out posts to `/prompt` directly and never re-enters `app.queuePrompt`, so
    it cannot recurse; Auto Queue is suppressed while a batch is in flight.
- **Only one eligible node drives the batch.** A Batch Prompter participates only if it
  is **active** (not muted or bypassed) **and** has an output wired to something. An
  active-but-unconnected node is ignored. If two or more eligible nodes exist the run
  falls back to a normal single queue.
- **Stop button** is now narrow and centred so it isn't under the node's bottom-right
  resize handle (no more accidental stops while resizing).
- **Prompt-number gutter** margin is now correct at low canvas zoom (≤ ~73%).
