# comfyui-coachbate

Quality-of-life ComfyUI nodes built primarily for CoachBate's own production use — **alpha
stage**, use at your own risk. Should run on any standard ComfyUI setup.

Highlights:
- **Workflows+** — a fast sidebar panel for searching workflows by name *and* by contents, with
  move/rename/delete for both files and folders. Tested against a library of nearly 8,000
  workflows across folders and subfolders. **Drag and drop** your json, audio, image, of video files here and it will open any embedded workflow like it used to
- **Workflow Model Path Auto-Fix** — finds models and LoRAs saved under folder names that don't
  match your local setup and fixes the widget automatically. A configurable override list lets
  you redirect specific files, e.g. swapping an fp8 model for an int8 convRot build.
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
| [Batch Prompter](#coachbate-batch-prompter)                 | Queue one job per prompt line from a multiline text block — all at once, one at a time, or in random order                                                                                                                         |
| [Text Preview and Edit](#coachbate-text-preview-and-edit)   | Editable text node that also displays and passes through any connected value                                                                                                                                                       |
| [Numbered Text](#coachbate-numbered-text)                   | Multiline text input with a line-number gutter; passes the full text as a STRING                                                                                                                                                   |
| [Video Combine](#coachbate-video-combine)                   | VHS Video Combine wrapper that strips API keys from video metadata before saving                                                                                                                                                   |
| [Strip API Key Metadata](#coachbate-strip-api-key-metadata) | Removes API key fields from video metadata                                                                                                                                                                                         |
| [Load Videos With Audio](#coachbate-load-videos-with-audio) | Load all the videos in a folder including their audio so they can be edited or saved to new video.                                                                                                                                 |
| [Lyrics JSON Parser](#coachbate-lyrics-json-parser)         | Parses a custom json format for lyrics/timing that an LLM creates                                                                                                                                                                  |
| [Shot Loader](#coachbate-shot-loader)                       | Drives a `shotlist.json` through Auto Queue one shot per run                                                                                                                                                                       |

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
  list of `search<TAB>replacement` rules (one per line; blank lines and lines starting with `#`
  are ignored). Rules are applied first-match-wins, then validated against that widget's actual
  local choices, so a rule that doesn't resolve to a real file is simply skipped. Use this to
  redirect specific files on purpose — e.g. always substituting an `int8_convrot` transformer
  build for the `fp8_scaled` one a workflow was authored with, or mapping an old folder layout
  (`LTXVideo\v2\...`) to a new one.
- **Disabling it** — open ComfyUI's **Settings → Extension**, filter for
  `CoachBate.WorkflowModelPathAutoFix`, and turn it off if you'd rather leave paths untouched.
- Every replacement it makes is logged server-side (`POST /coachbate/workflow_path_autofix/log`)
  so you can see what changed on load.

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
