# ComfyUI-CoachBate

## What this is
A ComfyUI custom node pack for shot-by-shot batch video production with LTX Video 2.3. It covers:
- **ShotLoader** — drives a `shotlist.json` through ComfyUI's Auto Queue one shot per run
- **BatchPrompter** — fans one queue job per non-blank prompt line
- **LTX Director suite** — forked from WhatDreamsCost-ComfyUI; full timeline editor for image + audio segments. **WIP, not part of the public release** — see "LTX nodes: experimental, dev-only, local-only" below.
- **LTX-FreeFuse** — LoRA loading, phase-1 sampling, and mask-based spatial concept mixing for LTX Video. **WIP, not part of the public release** — same section.
- **Audio scheduling, lyrics JSON, metadata safety** — utility nodes for production workflows

This is our own project (no upstream to worry about). The `WhatDreamsCost-ComfyUI` copy is the old **v1** Director and is now just an archive — this fork is v2.x and has diverged heavily from it, so there is no ongoing sync obligation. It's kept around only in case a v1 feature is worth porting into v2 later.

## Repo layout

```
ComfyUI-CoachBate/
├── __init__.py                   # NODE_CLASS_MAPPINGS, registers ComfyExtension
├── nodes.py                      # CoachBateShotLoader, CoachBateLoadVideosWithAudio, CoachBateBatchPrompter
├── nodes_audio_schedule.py       # CoachBateAudioSchedule
├── nodes_lyrics_json.py          # CoachBateLyricsJSONParser
├── nodes_metadata_safety.py      # CoachBateStripAPIKeyMetadata, CoachBateVideoCombine
├── nodes_ltx_freefuse.py         # CoachBateLTXLoRALoader/ConceptMap/Phase1Sampler/MaskApplicator
├── nodes_text_preview_edit.py    # CoachBateTextPreviewEdit
├── routes.py                     # POST /coachbate/skip, GET /coachbate/workflows/grep, and other API endpoints
├── contains_search.py            # AND/OR/phrase grammar (Python port of queryParser.js) for the grep endpoint
├── metadata_safety.py            # VHS patch logic (strips API keys from video metadata)
├── migrate_workflows.py          # Workflow migration utility
├── ltx_director/                 # LTX Director suite (forked from WhatDreamsCost)
│   ├── __init__.py               # exports LTXDirector, LTXDirectorGuide, LTXTrimLatent
│   ├── ltx_director.py
│   ├── ltx_director_guide.py
│   ├── patches.py
│   └── prompt_relay.py
├── ltx_freefuse/                 # LTX-FreeFuse backend
│   ├── attention_replace.py
│   ├── lora_hook.py
│   ├── mask_utils.py
│   └── token_utils.py
└── web/                          # Frontend JS (WEB_DIRECTORY = "./web")
    ├── ltx_director.js           # LTX Director timeline UI (forked + customised)
    └── js/
        ├── coachBateBatchPrompter.js
        ├── coachBateShotLoader.js
        ├── coachBateMetadataSafety.js
        ├── workflowModelPathAutoFix.js
        ├── coachBateWorkflowsPlus.js   # Workflows+ sidebar tab (entry point)
        └── workflows_plus/             # pure, unit-tested helper modules
            ├── queryParser.js          # AND/OR/phrase search grammar
            ├── virtualList.js          # windowed DOM row pool
            ├── workflowIndex.js        # tree build / sort / search / pin grouping
            ├── mru.js                  # 10-item recently-used list, +migrateKey
            ├── pins.js                 # per-folder pinned-workflow list, +migrateKey
            └── pathUtils.js            # rename/move/copy/trash key-building + folder-prefix helpers
```

Pure JS helpers under `workflows_plus/` have `node --test` coverage in
`tests/` (run `node --test "tests/*.test.mjs"`). `contains_search.py` has a
plain-assertion parity test at `tests/test_contains_search.py` (run with the
embedded interpreter: `python_embeded\python.exe
custom_nodes\ComfyUI-CoachBate\tests\test_contains_search.py`) — keep it in
sync with `queryParser.js` if the grammar changes.

## Node inventory

| Node class | Display name | Purpose |
|---|---|---|
| `CoachBateShotLoader` | CoachBate Shot Loader | Iterates a shotlist.json one shot per Auto Queue run |
| `CoachBateLoadVideosWithAudio` | CoachBate Load Videos With Audio | Loads video + audio pairs |
| `CoachBateBatchPrompter` | CoachBate Batch Prompter | Fans one queue run per prompt line |
| `CoachBateLyricsJSONParser` | Lyrics JSON Parser | Parses lyrics/timing JSON for audio sync workflows |
| `CoachBateVideoCombine` | CoachBate Video Combine | VHS wrapper that strips API key metadata before saving |
| `CoachBateStripAPIKeyMetadata` | CoachBate Strip API Key Metadata | Removes API keys from video metadata |
| `CoachBateTextPreviewEdit` | CoachBate Text Preview and Edit | Editable text display node |
| `CoachBateAudioSchedule` | CoachBate Audio Schedule | Schedules audio segments to timeline frames |
| `CoachBateLTXLoRALoader` ⚠️ | CoachBate LTX LoRA Loader | LTX-FreeFuse LoRA loader — WIP, local-only, not released |
| `CoachBateLTXConceptMap` ⚠️ | CoachBate LTX Concept Map | Maps LoRA concepts to spatial masks — WIP, local-only, not released |
| `CoachBateLTXPhase1Sampler` ⚠️ | CoachBate LTX Phase 1 Sampler | FreeFuse phase 1 sampling — WIP, local-only, not released |
| `CoachBateLTXMaskApplicator` ⚠️ | CoachBate LTX Mask Applicator | Applies concept masks during sampling — WIP, local-only, not released |
| `CoachBateLTXDirector` ⚠️ | LTX Director | Timeline editor (forked from WhatDreamsCost) — WIP, local-only, not released |
| `CoachBateLTXDirectorGuide` ⚠️ | LTX Director Guide | Guide frame node — WIP, local-only, not released |
| `CoachBateLTXTrimLatent` ⚠️ | LTX Trim Latent | Latent trimming with audio_latent_length output — WIP, local-only, not released |

New-API nodes (LTXDirector, LTXDirectorGuide, LTXTrimLatent) are registered via `CoachBateExtension(ComfyExtension)` in `__init__.py`.

⚠️ = only registers when `.coachbate_enable_ltx` exists locally — see next section. These 7 rows do not exist in a fresh public clone.

## LTX nodes: experimental, dev-only, local-only

The 7 LTX Director/FreeFuse nodes above don't currently work reliably and are
**not part of the public release**. Three independent layers, from outermost
to innermost:

1. **Registration gate (`.coachbate_enable_ltx`)** — a gitignored, empty
   marker file in the package root, checked in `__init__.py` via
   `_LTX_ENABLED = (Path(__file__).parent / ".coachbate_enable_ltx").exists()`.
   When absent (any fresh clone, including the public repo), the LTX imports
   never execute, the 7 entries are never added to `NODE_CLASS_MAPPINGS`/
   `NODE_DISPLAY_NAME_MAPPINGS`, and `CoachBateExtension.get_node_list()`
   effectively doesn't exist (the whole `CoachBateExtension` class + module-level
   `comfy_entrypoint()` are defined only inside the `if _LTX_ENABLED:` block).
   Result: the nodes are completely absent from `/object_info` — not just
   hidden from search. Marc's local machine has the marker file so his install
   keeps them; recreate it (`New-Item .coachbate_enable_ltx` — empty file is
   fine, only existence is checked) after a fresh clone if needed.
2. **`EXPERIMENTAL` / `is_experimental`** — set on all 7 node classes even when
   enabled, so ComfyUI's frontend shows the experimental/beta badge as a
   reminder these are unstable, on the one machine where they do run.
3. **`DEV_ONLY` / `is_dev_only`** — also set on all 7. This hides a node from
   node search / the Add-Node menu unless the viewer has ComfyUI's "Dev mode"
   setting turned on. Belt-and-suspenders on top of (1); by itself `DEV_ONLY`
   would **not** be enough to keep the nodes out of the public release — it
   only affects discoverability, the node still fully registers, still shows
   in `/object_info`, and an old workflow JSON referencing it would still run.

`web/ltx_director.js` (the Director timeline UI) is still served publicly via
`WEB_DIRECTORY` — a single file inside that directory can't be conditionally
excluded from static serving. This is harmless: its `app.registerExtension`
hooks self-gate on `nodeData.name === "CoachBateLTXDirector"`
(`web/ltx_director.js:4288-4291`), which never matches when the node isn't
registered, so the JS is inert on a public install.

### ⚠️ Before adding or renaming a node — read this first
A prior session lost track of this exact node: WIP was **stashed** under one
name (`CoachBateTextPreviewEdit`) while a divergent copy got committed under
another (`CoachBatePromptBuffer`), and this table disagreed with the code. A
fresh session started blind and rebuilt the wrong thing. To avoid a repeat:

1. **Check for loose work first.** Run `git stash list` and `git status` at
   the start of node work. If a stash or untracked file touches the node you're
   about to build, inspect it (`git show "stash@{0}:path"`) before writing new
   code — don't re-implement from scratch.
2. **Commit, don't stash.** Unfinished node work goes on a branch as a real
   commit (`git commit -m "WIP: <node>"`), never `git stash`. Stashes are
   nameless, invisible to the next session, and the trap that caused this.
3. **The class key is a contract.** The `NODE_CLASS_MAPPINGS` key (e.g.
   `CoachBateTextPreviewEdit`) is what saved workflow JSON references — renaming
   it makes every existing workflow show "missing node." Change the *display*
   name freely; treat the key as immutable once workflows exist.
4. **Update this table in the same change.** Renaming/adding a node and editing
   this Node inventory row + the `__init__.py` mappings is **one** atomic edit.
   A fresh session trusts this table to orient itself; if it lies, the session
   builds the wrong thing.

## shotlist.json format
```json
[
  {
    "shot_id": "001",
    "video_filename_prefix": "001-SCENE NAME",
    "duration_seconds": 8,
    "video_prompt": "...",
    "start_image": "C:/path/to/image.png",
    "start_image_strength": 0.85,
    "end_image": "",
    "status": "DONE"
  }
]
```
- Shots with `"status": "DONE"` are skipped automatically.
- Accepts bare array OR `{ "shots": [...] }` wrapper.
- Unknown fields are loaded and ignored — safe to add production tracking fields.

## Auto Queue workflow
1. Author `shotlist.json` with all shots.
2. Wire `CoachBateShotLoader` outputs into the workflow.
3. Set mode to `increment`, open ComfyUI queue dropdown → **Auto Queue**, press Queue once.
4. Node advances one shot per run; stops when all non-DONE shots complete.

## HTTP API (routes.py)
- `POST /coachbate/skip` — advances `stored_index` past the current shot without re-running the full workflow. Body: `{ "current_index": N, "total": M }`.
- `GET /coachbate/workflows/grep?q=<query>[&folder=<path>][&recurse=0]` — Workflows+ "Contains" search. `folder` scopes the scan to a subfolder (default "" = workflows root, path-traversal-checked via `os.path.commonpath`); `recurse=0` restricts to that folder's direct files only (default is recursive). Streams newline-delimited JSON (`{"type":"total"|"progress"|"done", ...}`) so the frontend can show scan progress; cancel by aborting the fetch (checked via `request.transport.is_closing()` between files). Query grammar via `contains_search.build_predicate()`.

## LTX Director fork (web/ltx_director.js)
This is our customised copy of the WhatDreamsCost LTX Director JS. Our additions:
- **Photopea integration**: `PhotopeaDialog` class + CSS + "Edit in Photopea" in the right-click menu
- **Chunk markers**: vertical timeline markers for multi-chunk generation boundaries

These additions survive git pulls on CoachBate (we control this repo), but the equivalent file in `WhatDreamsCost-ComfyUI` gets overwritten on their pulls. Keep both files in sync.

## Workflows+ sidebar tab (web/js/coachBateWorkflowsPlus.js)
A fast replacement browser for the workflows sidebar, built because the stock
ComfyUI Workflows tab hangs the browser with thousands of workflows (this user
has ~7.4k under `--user-directory`). Registered as sidebar tab id
`coachbate-workflows-plus`, title "Workflows+".

Features:
- **Virtualized tree** — DOM row count stays ~28 regardless of folder size
  (verified with a 3,465-file folder). Listing comes from
  `GET /userdata?dir=workflows&recurse=true&split=false&full_info=true`
  (~1.3 s once, cached; Refresh button re-fetches and calls
  `store.syncWorkflows()`).
- **AND/OR/phrase search over filenames** — bare adjacency (`black cat`) is a
  literal, contiguous substring phrase (does NOT match "black cute cat");
  `"black cat"` quoted behaves identically. `black AND cat` / `black OR cat`
  match words independently — `AND`/`OR` are recognized **only in
  uppercase**, unquoted; lowercase "and"/"or" is ordinary text. AND binds
  tighter than OR. Grammar + predicate in `workflows_plus/queryParser.js`,
  ported 1:1 to Python in `contains_search.py` for the grep endpoint — keep
  both in sync.
- **Contains mode** (Name ⇄ Contains toggle) — searches inside each
  workflow's raw JSON text server-side via `GET /coachbate/workflows/grep`,
  using the same grammar. Runs on Enter (not live), streams NDJSON progress
  lines the panel renders as a progress bar, and is cancelable via the
  Cancel button or Esc (aborts the `fetch`; the server notices the closed
  connection between files and stops scanning further files).
- **Folder-scoped search** — clicking a folder row in the tree sets
  `S.scopeFolder` (via `toggleFolder` → `setScopeFolder`) and shows a
  "Search subfolders of ..." label with a ✕ (`clearScopeFolder`) next to the
  toggle. Both Name (`workflowIndex.searchFiles`'s `scopeFolder`/
  `includeSubfolders` opts, built on the pure `keyInScope` helper) and
  Contains (`folder`/`recurse` query params on the grep endpoint) search are
  restricted to that folder. The **Search subfolders** checkbox
  (`S.searchSubfolders`, persisted in localStorage
  `coachbate.wfp.searchSubfolders`, default ON) controls whether descendants
  of the scope are included — with no folder selected, `scopeFolder=""`
  means the checkbox controls whole-library vs. workflows-root-only, exactly
  as before this feature (root is just scope="" with `keyInScope` treating
  `startsWith(scope + "/")` as "any nested file" when `scope === ""`).
  `rebuildIndex` clears a stale scope if the scoped folder no longer exists
  after a rename/move/delete. The scope indicator is a dedicated accent-
  styled bar (`.wfp-scoped-active`) with a `✕ Show All` button
  (`clearScopeFolder`) — never a subtle label — and the scoped folder's own
  tree row gets a `.wfp-scoped-folder` highlight. `toggleFolder` only
  re-scopes on the *opening* click; clicking an already-scoped, expanded
  folder again both collapses it and clears the scope (`clearScopeFolder`) —
  the fast way back to "search everything" besides the button.
- **Sort** by name / modified / created (clickable header, persisted in
  localStorage `coachbate.wfp.sort`).
- **Per-folder pinning** (`workflows_plus/pins.js`, localStorage
  `coachbate.wfp.pins`, keyed by folder path) — right-click a workflow row for
  a context menu with Pin/Unpin plus "Reset all pins for this folder" and
  "Reset all pins". In tree mode (`flattenVisible`'s `getPinnedKeys` param)
  pinned files float to the top of their own folder as a separate group,
  each group independently sorted by the active sort spec. In flat search
  results pins are annotated (icon only) but do NOT reorder — grouping is
  tree-mode only.
- **MRU** — last 10 opened workflows in a `Recent ▾` popup; captured globally
  via a 3 s poll of `store.activeWorkflow.path` (localStorage
  `coachbate.wfp.mru`).
- **Drag-and-drop** of workflow JSON / PNG / MP4 onto the panel — forwards each
  file to `app.handleFile(f, "file_drop", {deferWarnings:true})`, which runs the
  frontend's own PNG/WebM/MP4/JSON workflow-metadata extraction (this is the
  canvas DnD that has been broken; the panel drop path works).
- **File management** (rename / move / duplicate / delete, single or
  multi-select) — uses ComfyUI's built-in, already-registered userdata
  endpoints (no new backend, no restart needed):
  - `POST /userdata/{file}/move/{dest}` for rename and move (both file and
    dest are `encodeURIComponent`-ed full paths, e.g.
    `workflows/MCB/foo.json` — a raw "/" would split into extra path
    segments and 404; the aiohttp route only accepts a single dynamic
    segment, so the "/" must stay percent-encoded). The move endpoint's
    destination directory is auto-created (`get_request_user_filepath`
    defaults `create_dir=True`), so moving to a folder that doesn't exist
    yet just creates it — no separate "create folder" step needed.
  - `DELETE /userdata/{file}` for permanent delete; `GET`/`POST
    /userdata/{file}` (read then write elsewhere, `overwrite=false`) for
    duplicate.
  - Each op first checks `store.getWorkflowByPath(fullPath)`; if the file is
    a tracked/open workflow it prefers `store.renameWorkflow(wf, newPath)` /
    `store.deleteWorkflow(wf)` (keeps any open tab's title/save-target in
    sync), falling back to the raw endpoint on failure or when the file
    isn't tracked.
  - **Selection**: Ctrl/Cmd-click toggles, Shift-click selects a contiguous
    range (order comes from `S._lastRows`, captured each `renderRows()` —
    tree mode filters to `kind==='file'`, search/contains mode is already
    flat). Right-click on a row not in the current selection replaces the
    selection with just that row (standard explorer behavior).
  - **Rename**: inline `<input>` swapped into the row (`S.renamingKey`);
    Enter/blur commits via `pathUtils.renameKey` + the move endpoint, Esc
    cancels. Local pins/MRU are migrated (`pins.migrateKey` /
    `mru.migrateKey`) before a full `rebuildIndex({sync:false})` — a fresh
    listing is refetched rather than hand-patched, trading ~1.3 s for
    guaranteed correctness (occasional action, not a hot path).
  - **Move**: right-click → "Move to…" opens a folder-picker modal (type to
    filter existing folders from `S.tree`, or type a path that doesn't
    exist yet — shown as "+ Create ..."), **or** drag a file/selection onto
    a folder row (`draggable` + a custom `application/x-coachbate-wfp-drag`
    DataTransfer type carrying a JSON payload `{kind:"files", keys:[...]}`,
    kept separate from the external-OS-file drop handler on the panel root
    so the two don't collide).
  - **Duplicate**: `pathUtils.nextCopyKey` picks `"name copy.json"`,
    incrementing to `"copy 2"`, `"copy 3"`, … against the destination
    folder's existing entries.
  - **Delete**: soft-deletes by moving into `_trash/` via
    `pathUtils.trashKeyFor` (timestamp + flattened original path, so it's
    collision-proof and traceable — `_trash/20260703-090013__MCB__foo.json`
    for what was `MCB/foo.json`). Deleting a file that's already under
    `_trash/` (`pathUtils.isInTrash`) hard-deletes it instead — this is how
    you empty individual items. Right-click the `_trash` folder row itself
    for **Empty Trash** (hard-deletes everything inside). All deletes go
    through a native `window.confirm()` — deliberately not a custom modal,
    since a destructive action deserves a blocking dialog that doesn't
    depend on the frontend's own dialog service being available.
- **Folder management** (rename / move / delete a whole folder) — right-
  click any folder row except `_trash` (which keeps its Empty-Trash-only
  menu) and, defensively, the workflows root (which has no row at all, so
  it's structurally undeletable; `openFolderContextMenu`/`deleteFolder` also
  early-return on an empty path as a backstop):
  - **Moving a directory works on the same `/userdata/{file}/move/{dest}`
    endpoint used for files** — `shutil.move` has no file-type check, verified
    by reading `user_manager.py` this session. The one gotcha:
    `full_info=true` must be omitted, since `get_file_info()` calls
    `os.path.getsize()` unconditionally and throws `IsADirectoryError` on a
    directory. `moveOneFolder` in `coachBateWorkflowsPlus.js` calls the
    endpoint directly (no store branch — `getWorkflowByPath` only tracks
    individual files).
  - **Hard-deleting a directory does NOT work** the same way — `DELETE
    /userdata/{file}` calls `os.remove`, which raises on directories. Not
    needed here: folder delete is always a single directory *move* into
    `_trash` (`pathUtils.trashKeyForFolder`), so the existing move endpoint
    covers it; there's no dedicated folder-delete endpoint. Emptying Trash
    still deletes the individual *files* inside `_trash` one at a time
    (existing per-file delete path) — it leaves the now-empty directory
    skeleton behind, which is harmless since the tree is built purely from
    file paths (`workflowIndex.buildTree`) and never renders empty folders.
  - **Cycle guard**: `pathUtils.isDescendantOrSelf(dest, src)` rejects
    dropping/moving a folder into itself or one of its own subfolders,
    checked both in the drop handler and in `applyFolderMove`. The move
    dialog also filters the folder-picker list with the same check when
    moving a folder (not shown as a valid destination).
  - **Drag payload**: folder rows are `draggable` too, using the same
    `application/x-coachbate-wfp-drag` MIME with `{kind:"folder", path}`; a
    folder row's own drop handler branches on `payload.kind` to call either
    `applyFolderMove` or the existing file `applyMove`.
  - **State migration**: `migrateFolderPrefixEverywhere(oldPrefix,
    newPrefix)` rewrites every local/persisted path under the moved/renamed/
    deleted folder in one pass — `S.expanded`, `S.scopeFolder`, `S.selected`,
    `S.lastClickedKey` (via `pathUtils.rewritePrefix`), plus a per-file
    `pins.migrateKey`/`mru.migrateKey` call for every entry whose key falls
    under the old prefix. Always followed by `rebuildIndex({sync:true})`.

Frontend contract (⚠️ re-verify on `comfyui_frontend_package` upgrades — these
are minified-internal surfaces, not public API):
- Open with save-path association: `app.loadGraphData(wf.activeState, true, true, wf)`
  where `wf = app.extensionManager.workflow.getWorkflowByPath("workflows/"+key)`.
  Fallback (file newer than store sync): `app.loadGraphData(json, true, true, key)`
  (string 4th arg → `afterLoadNewGraph` string branch). Result: `isModified`
  stays false, Ctrl+S saves back to the same file (verified).
- Sidebar activation store lives at `app.extensionManager.sidebarTab`
  (`.toggleSidebarTab(id)`); the render callback receives the panel element and
  fires on every show, `destroy` on every hide — panel state is a module
  singleton so it survives hide/show.
- **Tab position**: `app.extensionManager.registerSidebarTab` (the wrapper)
  doesn't expose an ordering option, but the underlying store does —
  `app.extensionManager.sidebarTab.registerSidebarTab(tabDef, {prepend:
  true})` puts ours first. Verified live: `coachbate-workflows-plus` lands
  at index 0, before `assets`, immediately (no fallback needed in practice —
  core tabs are already registered by the time extension `setup()` runs).
  A defensive `setTimeout` splice-to-front fallback exists in case
  registration order ever changes, but has not been exercised.
- `app.handleFile` must exist for drop to work (guarded; toasts if missing).

Browser verification: use `tools/preview-proxy.mjs` (8189 → 8188) + Claude
Preview. The proxy injects a boot shim into the HTML `<head>` because the
preview tab is hidden (`document.hidden`, `innerWidth=0`), which pauses
`requestAnimationFrame` and otherwise **stalls ComfyUI boot at the splash
screen**. The shim only affects proxied (8189) access, never normal 8188 use.

**Verification status (2026-07-03):** fully live-verified in-browser via the
proxy above, against the real ~7.4k-workflow library plus disposable dummy
files created and cleaned up for the destructive operations (never touched
real workflows):
- Virtualized tree/search, sort, MRU, drag-and-drop, save-path association.
- Uppercase-only AND/OR + phrase-match semantics (lowercase `qwen and
  inpaint` → 0 matches vs `qwen AND inpaint` → 20).
- Per-folder pinning (context-menu Pin/Unpin, pinned group floats to folder
  top and sorts independently, per-folder + global reset) and its migration
  through rename/move/delete (a pinned file's pin followed it through a
  rename and a soft-delete into `_trash` in the same run).
- "Search subfolders" toggle (302 → 1 match root-only).
- **Contains mode**: root-only scan (35/36 matches for "KSampler"), a
  progress bar during a full 7.4k-file scan, and a completed scan showing
  the correct final "Scanned N files" / match count. (The endpoint 404'd
  earlier in development until the next ComfyUI restart picked up the new
  route — expected, aiohttp registers routes at startup — and has been live
  since.)
- **File management**: multi-select (Ctrl-click toggle, Shift-click range),
  bulk context menu ("Duplicate/Move/Pin/Delete N files", folder-scoped
  pin-reset only shown when the selection shares one folder), rename
  (inline input, commits on Enter, on-disk confirmed), duplicate
  (`name copy.json` → `copy 2` on collision), move via both the folder-
  picker dialog (including auto-created brand-new subfolders) and drag-a-
  file-onto-a-folder-row, soft-delete to `_trash` with pin/MRU migration,
  hard-delete of an already-trashed file, and "Empty Trash" on the `_trash`
  folder's context menu.
- **Folder-scoped search (2026-07-04)**: Name-mode scoping fully
  live-verified — clicking a folder sets the scope label/clear-button
  correctly, results are restricted to that folder's `key`s, and toggling
  "Search subfolders" while scoped correctly includes/excludes nested files
  (verified against a real folder with subfolders: 21 matches recursive vs.
  2 matches direct-children-only for the same query). Contains-mode scoping
  (`folder`/`recurse` params on the grep endpoint) is implemented and
  unit/syntax-checked but **could NOT be live-verified**: the same
  aiohttp-registers-routes-at-startup gotcha as before — the server was
  still running the pre-refactor handler (which silently ignores the new
  `folder` param and falls back to whole-library scanning) and hadn't been
  restarted since this edit. ⚠️ Restart ComfyUI, then confirm a Contains
  scan while a folder is scoped returns only matches from that folder
  (e.g. scope to a small folder, search a common term, check every result's
  `path` starts with the scoped folder).
- **Folder management, scope-UX overhaul, tab position (2026-07-05)**: fully
  live-verified against disposable `_wfp_dirtest*` folders (created and
  cleaned up; real workflows untouched). Tab ordering confirmed at boot
  (`coachbate-workflows-plus` first, no fallback needed). Scope bar/clear-
  button/scoped-row-highlight all confirmed, plus both ways back to
  "search everything" (the ✕ button and re-clicking the scoped, expanded
  folder). Folder rename confirmed on disk with subtree intact. Drag-drop
  folder-onto-folder move confirmed on disk (including the folder's own
  drop-onto-itself no-op and the descendant-cycle rejection — dragging a
  parent onto its own child left the tree unchanged). Folder delete
  confirmed: confirmation dialog showed the correct file count, the entire
  subtree landed under `_trash/<stamp>__Name/...` in one move, and a pin on
  a file two levels deep inside the deleted folder correctly migrated to
  its new trashed path (`migrateFolderPrefixEverywhere` proven on a real
  nested case, not just the unit tests). Empty Trash still worked afterward
  (removed the files; left a harmless empty dir skeleton as documented
  above).

## Metadata safety (metadata_safety.py / nodes_metadata_safety.py)
`CoachBateVideoCombine` wraps the VideoHelperSuite `VHS_VideoCombine` node and strips `api_key`, `civitai_token`, and similar fields from video metadata before the file is written. If VHS is not installed the patch is skipped with a warning (not an error).

## ComfyUI API
Uses Nodes 2.0 API (`comfy_api.latest`, `io.ComfyNode`) for LTX Director nodes. Legacy nodes (ShotLoader etc.) use the V1 API with a V3 shim in `nodes.py`.
