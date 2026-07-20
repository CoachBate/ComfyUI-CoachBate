// Workflows+ — a fast workflow browser sidebar tab for ComfyUI.
//
// Motivation: the stock Workflows tab hangs the browser with thousands of
// workflows (client-side tree build + fuzzy index). This panel:
//   - fetches the listing once (async, cached), renders a virtualized tree,
//   - supports AND/OR/phrase search over filenames, with a "Contains" mode
//     that searches inside the workflow JSON (server-side, cancelable),
//   - has a "search subfolders" toggle (off = root folder only),
//   - lets you scope search to a clicked folder (prominent scope bar, easy
//     to clear — click the scoped folder again, or the "Show All" button),
//   - sorts by name / modified / created,
//   - supports Windows-style per-folder pinning (pinned items float to the
//     top of their own folder, right-click for pin/reset actions),
//   - keeps a 10-item MRU list,
//   - accepts drag-and-drop of workflow JSON / PNG / MP4 (canvas DnD is broken),
//   - supports rename / move / duplicate / (soft-)delete for both files and
//     whole folders, single or multi-select (Ctrl/Shift-click) for files,
//     including drag-a-file-or-folder-onto-a-folder to move.
//
// This is the only file in the feature that touches ComfyUI internals; the
// pure logic lives in ./workflows_plus/*.js (unit tested with `node --test`).

import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";
import { buildPredicate } from "./workflows_plus/queryParser.js";
import { VirtualList } from "./workflows_plus/virtualList.js";
import { buildTree, flattenVisible, searchFiles } from "./workflows_plus/workflowIndex.js";
import { loadMru, pushMru, migrateKey as mruMigrateKey } from "./workflows_plus/mru.js";
import {
    isPinned,
    togglePin,
    pinnedKeysForFolder,
    resetFolderPins,
    resetAllPins,
    migrateKey as pinsMigrateKey,
} from "./workflows_plus/pins.js";
import {
    folderOf,
    filenameOf,
    renameKey,
    moveKey,
    nextCopyKey,
    trashKeyFor,
    trashKeyForFolder,
    isDescendantOrSelf,
    rewritePrefix,
    isInTrash,
    TRASH_FOLDER,
} from "./workflows_plus/pathUtils.js";

const WF_ROOT = "workflows"; // userdata subdir
const FRESH_MS = 5 * 60 * 1000; // refetch listing on tab open if older than this
const ROW_H = 26;
const DRAG_MIME = "application/x-coachbate-wfp-drag";

// ── Module-singleton state (survives tab hide/show) ──────────────────────────
const S = {
    entries: [], // { key, filename, nameLower, size, modified, created }
    entryByKey: new Map(),
    tree: null,
    expanded: new Set(), // folder paths that are open
    searchText: "",
    searchMode: "name", // "name" | "contains"
    searchSubfolders: loadSearchSubfolders(), // true = recursive (default)
    scopeFolder: "", // "" = whole library (or workflows root if !searchSubfolders); else a folder path
    sortSpec: loadSortSpec(),
    lastFetchMs: 0,
    fetching: false,
    scrollTop: 0,
    mru: loadMru(),

    // Selection (Ctrl/Shift multi-select) + inline rename.
    selected: new Set(),
    lastClickedKey: null,
    renamingKey: null,
    renamingFolder: null,

    // Contains-search (server-side content grep) state.
    containsQuery: "",
    containsScanning: false,
    containsScanned: 0,
    containsTotal: 0,
    containsResults: null, // null = no scan run yet for the current query
    containsAbort: null,

    // Live DOM refs for the currently-mounted panel (null when hidden).
    el: null,
    scroller: null,
    vlist: null,
    countEl: null,
    searchInput: null,
    sortBtns: {},
    modeBtns: {},
    subfolderToggle: null,
    scopeRow: null,
    scopeLabel: null,
    scopeClearBtn: null,
    progressRow: null,
    progressFill: null,
    progressText: null,
    cancelBtn: null,
    openingKey: null, // guard against double-open
    _mruPopup: null,
    _ctxMenu: null,
    _moveDialog: null,
    _lastMode: null,
    _lastRows: [],
};

// ── Persisted sort spec / toggles ─────────────────────────────────────────────
const SORT_STORAGE_KEY = "coachbate.wfp.sort";
const SEARCH_SUBFOLDERS_STORAGE_KEY = "coachbate.wfp.searchSubfolders";

function loadSortSpec() {
    try {
        const raw = localStorage.getItem(SORT_STORAGE_KEY);
        if (raw) {
            const s = JSON.parse(raw);
            if (s && s.field && s.dir) return s;
        }
    } catch (_) {}
    return { field: "modified", dir: -1 };
}
function saveSortSpec() {
    try {
        localStorage.setItem(SORT_STORAGE_KEY, JSON.stringify(S.sortSpec));
    } catch (_) {}
}
function loadSearchSubfolders() {
    try {
        const raw = localStorage.getItem(SEARCH_SUBFOLDERS_STORAGE_KEY);
        return raw === null ? true : raw === "1"; // default ON (recursive)
    } catch (_) {
        return true;
    }
}
function saveSearchSubfolders() {
    try {
        localStorage.setItem(SEARCH_SUBFOLDERS_STORAGE_KEY, S.searchSubfolders ? "1" : "0");
    } catch (_) {}
}

// ── Styles (injected once) ───────────────────────────────────────────────────
function ensureStyles() {
    if (document.getElementById("coachbate-wfp-css")) return;
    const style = document.createElement("style");
    style.id = "coachbate-wfp-css";
    style.textContent = `
.wfp-root { display:flex; flex-direction:column; height:100%; overflow:hidden;
    color: var(--fg-color); font-size:12px; position:relative; }
.wfp-header { display:flex; align-items:center; gap:4px; padding:6px 6px 4px;
    border-bottom:1px solid var(--border-color); flex:0 0 auto; }
.wfp-sortgroup { display:flex; gap:2px; }
.wfp-btn { background: var(--comfy-input-bg); color: var(--fg-color);
    border:1px solid var(--border-color); border-radius:4px; padding:2px 6px;
    cursor:pointer; font-size:11px; line-height:1.4; white-space:nowrap; }
.wfp-btn:hover { filter:brightness(1.2); }
.wfp-btn.active { border-color: var(--p-primary-color, #4a9eff);
    color: var(--p-primary-color, #4a9eff); }
.wfp-btn:disabled { opacity:0.4; cursor:default; }
.wfp-count { margin-left:auto; opacity:0.6; font-size:11px; padding:0 4px;
    white-space:nowrap; }
.wfp-searchrow { display:flex; padding:4px 6px; gap:4px; flex:0 0 auto;
    flex-wrap:wrap; }
.wfp-search { flex:1 1 auto; min-width:80px; background: var(--comfy-input-bg);
    color: var(--fg-color); border:1px solid var(--border-color);
    border-radius:4px; padding:3px 6px; font-size:12px; outline:none; }
.wfp-modegroup { display:flex; gap:2px; flex:0 0 auto; }
.wfp-subfolderrow { display:flex; align-items:center; gap:6px; padding:4px 6px;
    margin:0 6px 4px; flex:0 0 auto; font-size:11px; user-select:none;
    cursor:pointer; border-radius:4px; border:1px solid transparent; }
.wfp-subfolderrow.wfp-scoped-active { background: rgba(74,158,255,0.15);
    border-color: var(--p-primary-color, #4a9eff); }
.wfp-subfolderrow input { cursor:pointer; flex:0 0 auto; }
.wfp-scopelabel { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; flex:1 1 auto; }
.wfp-scopelabel strong { color: var(--p-primary-color, #4a9eff); }
.wfp-scopeclear { flex:0 0 auto; padding:2px 8px; font-size:11px; font-weight:600; }
.wfp-progressrow { display:flex; align-items:center; gap:6px; padding:0 6px 4px;
    flex:0 0 auto; font-size:11px; }
.wfp-progressbar { flex:1 1 auto; height:6px; border-radius:3px;
    background: var(--comfy-input-bg); border:1px solid var(--border-color);
    overflow:hidden; }
.wfp-progressfill { height:100%; width:0%; background: var(--p-primary-color, #4a9eff);
    transition: width 0.15s linear; }
.wfp-progresstext { opacity:0.7; white-space:nowrap; }
.wfp-mrurow { display:flex; align-items:center; padding:0 6px 4px; flex:0 0 auto;
    position:relative; }
.wfp-scroller { flex:1 1 auto; overflow-y:auto; overflow-x:hidden;
    contain: strict; }
.wfp-row { display:flex; align-items:center; cursor:pointer; padding:0 6px;
    white-space:nowrap; overflow:hidden; }
.wfp-row:hover { background: var(--border-color); }
.wfp-row.wfp-selected { background: rgba(74,158,255,0.25); }
.wfp-row.wfp-scoped-folder { background: rgba(74,158,255,0.15); }
.wfp-row.wfp-scoped-folder .wfp-name { font-weight:700; color: var(--p-primary-color, #4a9eff); }
.wfp-row.wfp-drop-target { outline:2px solid var(--p-primary-color, #4a9eff);
    outline-offset:-2px; }
.wfp-chevron { width:14px; flex:0 0 auto; opacity:0.7; text-align:center; }
.wfp-icon { width:16px; flex:0 0 auto; opacity:0.6; text-align:center; }
.wfp-pin { width:14px; flex:0 0 auto; text-align:center; opacity:0.85; }
.wfp-name { overflow:hidden; text-overflow:ellipsis; flex:1 1 auto; }
.wfp-rename-input { flex:1 1 auto; background: var(--comfy-input-bg);
    color: var(--fg-color); border:1px solid var(--p-primary-color, #4a9eff);
    border-radius:3px; padding:1px 4px; font-size:12px; outline:none;
    min-width:40px; }
.wfp-folder-secondary { opacity:0.45; font-size:10px; margin-left:6px;
    overflow:hidden; text-overflow:ellipsis; direction:rtl; text-align:left;
    flex:0 1 auto; max-width:45%; }
.wfp-badge { opacity:0.5; font-size:10px; margin-left:6px; flex:0 0 auto; }
.wfp-date { opacity:0.45; font-size:10px; margin-left:8px; flex:0 0 auto;
    font-variant-numeric:tabular-nums; }
.wfp-folder .wfp-name { font-weight:600; }
.wfp-overlay { position:absolute; inset:0; display:flex; align-items:center;
    justify-content:center; background: var(--comfy-menu-bg); opacity:0.85;
    z-index:5; font-size:12px; }
.wfp-mrupopup { position:absolute; top:100%; left:6px; right:6px; z-index:10;
    background: var(--comfy-menu-bg); border:1px solid var(--border-color);
    border-radius:4px; max-height:280px; overflow-y:auto;
    box-shadow:0 4px 12px rgba(0,0,0,0.4); }
.wfp-mruitem { display:flex; align-items:center; padding:4px 8px; cursor:pointer;
    overflow:hidden; }
.wfp-mruitem:hover { background: var(--border-color); }
.wfp-ctxmenu { position:fixed; z-index:20; background: var(--comfy-menu-bg);
    border:1px solid var(--border-color); border-radius:4px; min-width:180px;
    box-shadow:0 4px 12px rgba(0,0,0,0.4); padding:3px 0; }
.wfp-ctxitem { padding:5px 12px; cursor:pointer; white-space:nowrap; font-size:12px; }
.wfp-ctxitem:hover { background: var(--border-color); }
.wfp-ctxsep { height:1px; margin:3px 0; background: var(--border-color); }
.wfp-dragover { outline:2px dashed var(--p-primary-color, #4a9eff);
    outline-offset:-4px; }
.wfp-drophint { position:absolute; inset:0; display:flex; align-items:center;
    justify-content:center; background: rgba(0,0,0,0.5); z-index:8;
    pointer-events:none; font-size:13px; }
.wfp-empty { padding:16px; opacity:0.6; text-align:center; }
.wfp-modal-overlay { position:fixed; inset:0; z-index:30; background:rgba(0,0,0,0.5);
    display:flex; align-items:center; justify-content:center; }
.wfp-modal { background: var(--comfy-menu-bg); border:1px solid var(--border-color);
    border-radius:6px; width:320px; max-width:90vw; padding:12px;
    box-shadow:0 8px 24px rgba(0,0,0,0.5); display:flex; flex-direction:column; gap:8px; }
.wfp-modal-title { font-size:13px; font-weight:600; }
.wfp-modal-input { width:100%; box-sizing:border-box; background: var(--comfy-input-bg);
    color: var(--fg-color); border:1px solid var(--border-color); border-radius:4px;
    padding:5px 7px; font-size:12px; outline:none; }
.wfp-modal-list { max-height:180px; overflow-y:auto; border:1px solid var(--border-color);
    border-radius:4px; }
.wfp-modal-item { padding:5px 8px; cursor:pointer; font-size:12px; white-space:nowrap;
    overflow:hidden; text-overflow:ellipsis; }
.wfp-modal-item:hover { background: var(--border-color); }
.wfp-modal-newfolder { opacity:0.8; font-style:italic; }
.wfp-modal-buttons { display:flex; justify-content:flex-end; gap:6px; }
`;
    document.head.appendChild(style);
}

// ── Index fetch / build ──────────────────────────────────────────────────────
async function fetchIndex() {
    const resp = await api.fetchApi(
        `/userdata?dir=${WF_ROOT}&recurse=true&split=false&full_info=true`,
        { cache: "no-store" }
    );
    if (!resp.ok) throw new Error(`userdata listing failed: ${resp.status}`);
    const raw = await resp.json(); // [{ path, size, modified, created }]
    return raw.map(rawToEntry).filter(Boolean);
}

function rawToEntry(item) {
    const key = item.path; // relative to workflows dir, e.g. "MCB/foo.json"
    const base = key.split("/").pop();
    if (base.startsWith(".")) return null; // skip .index.json etc.
    const filename = base.replace(/\.[^.]+$/, "");
    return {
        key,
        filename,
        nameLower: filename.toLowerCase(),
        size: item.size,
        modified: item.modified ?? 0,
        created: item.created ?? 0,
    };
}

async function rebuildIndex({ sync = false } = {}) {
    if (S.fetching) return;
    S.fetching = true;
    showOverlay(sync ? "Refreshing…" : "Loading workflows…");
    try {
        if (sync) {
            try {
                await app.extensionManager.workflow.syncWorkflows();
            } catch (_) {
                // Non-fatal: store sync is a bonus for open-association freshness.
            }
        }
        S.entries = await fetchIndex();
        S.entryByKey = new Map(S.entries.map((e) => [e.key, e]));
        S.tree = buildTree(S.entries);
        S.lastFetchMs = Date.now();
        // Drop selection entries for files that no longer exist.
        for (const k of [...S.selected]) {
            if (!S.entryByKey.has(k)) S.selected.delete(k);
        }
        // If the scoped folder was renamed/deleted out from under us, fall back to whole-library search.
        if (S.scopeFolder && !S.entries.some((e) => folderOf(e.key) === S.scopeFolder || folderOf(e.key).startsWith(`${S.scopeFolder}/`))) {
            S.scopeFolder = "";
        }
        renderRows();
        updateScopeUI();
    } catch (err) {
        console.error("[Workflows+] index build failed", err);
        toast("error", "Workflows+ failed to load", String(err?.message || err));
        showEmpty("Failed to load workflows. Click Refresh to retry.");
    } finally {
        S.fetching = false;
        hideOverlay();
    }
}

// ── Rendering ────────────────────────────────────────────────────────────────
function getPinnedKeysForFolder(folderPath) {
    return pinnedKeysForFolder(folderPath);
}

function findTreeNode(node, path) {
    if (!node) return null;
    if (node.path === path) return node;
    for (const f of node.folders) {
        const found = findTreeNode(f, path);
        if (found) return found;
    }
    return null;
}

function collectFolderPaths(node, out) {
    out.push(node.path);
    for (const f of node.folders) collectFolderPaths(f, out);
}
function allFolderPaths() {
    if (!S.tree) return [""];
    const out = [];
    collectFolderPaths(S.tree, out);
    return out.sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
}

function currentRows() {
    if (S.searchMode === "contains") {
        if (S.containsScanning) return { mode: "contains-busy", rows: [] };
        if (S.containsResults) {
            return { mode: "search", rows: S.containsResults };
        }
        // No scan run yet (or it was cleared/cancelled) — fall through to tree.
    } else {
        const predicate = buildPredicate(S.searchText);
        if (predicate !== null || S.searchText.trim() !== "") {
            const rows = searchFiles(S.entries, predicate, S.sortSpec, {
                scopeFolder: S.scopeFolder,
                includeSubfolders: S.searchSubfolders,
                isPinnedFn: (key) => isPinned(key),
            });
            return { mode: "search", rows };
        }
    }

    if (!S.tree) return { mode: "tree", rows: [] };
    return { mode: "tree", rows: flattenVisible(S.tree, S.expanded, S.sortSpec, getPinnedKeysForFolder) };
}

function renderRows() {
    if (!S.vlist) return;
    const { mode, rows } = currentRows();
    S._lastMode = mode;
    S._lastRows = rows;

    updateProgressUI();

    if (S.countEl) {
        if (mode === "search") {
            S.countEl.textContent = `${rows.length} match${rows.length === 1 ? "" : "es"}`;
        } else if (mode === "contains-busy") {
            S.countEl.textContent = "";
        } else {
            S.countEl.textContent = `${S.entries.length} workflow${S.entries.length === 1 ? "" : "s"}`;
        }
    }

    if (mode === "contains-busy") {
        S.vlist.setItems([]);
        return;
    }

    S.vlist.renderRow = mode === "search" ? renderSearchRow : renderTreeRow;
    S.vlist.setItems(rows);
}

function fmtDate(ms) {
    if (!ms) return "";
    const d = new Date(ms);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function dateForSort(item) {
    return S.sortSpec.field === "created" ? item.created : item.modified;
}

function pinIconHtml(pinned) {
    return `<span class="wfp-pin">${pinned ? "📌" : ""}</span>`;
}

function renderRenameInput(rowEl, currentName, onCommit, onCancel) {
    const input = rowEl.querySelector(".wfp-rename-input");
    input.value = currentName;
    input.onclick = (e) => e.stopPropagation();
    input.onmousedown = (e) => e.stopPropagation();
    input.onkeydown = (e) => {
        e.stopPropagation();
        if (e.key === "Enter") {
            e.preventDefault();
            onCommit(input.value);
        } else if (e.key === "Escape") {
            e.preventDefault();
            onCancel();
        }
    };
    input.onblur = () => onCommit(input.value);
    requestAnimationFrame(() => {
        input.focus();
        input.select();
    });
}

function renderTreeRow(item, rowEl) {
    const isFileRenaming = item.kind === "file" && S.renamingKey === item.key;
    const isFolderRenaming = item.kind === "folder" && S.renamingFolder === item.path;
    const isScopedFolder = item.kind === "folder" && S.scopeFolder === item.path;
    rowEl.className =
        "wfp-row" +
        (item.kind === "folder" ? " wfp-folder" : "") +
        (isScopedFolder ? " wfp-scoped-folder" : "") +
        (item.kind === "file" && S.selected.has(item.key) ? " wfp-selected" : "");
    rowEl.style.paddingLeft = `${6 + item.depth * 14}px`;
    rowEl.oncontextmenu = null;
    rowEl.draggable = false;
    rowEl.ondragstart = null;
    rowEl.ondragover = null;
    rowEl.ondragleave = null;
    rowEl.ondrop = null;

    if (item.kind === "folder") {
        if (isFolderRenaming) {
            rowEl.innerHTML = `<span class="wfp-chevron"></span><input class="wfp-rename-input" />`;
            rowEl.onclick = null;
            const currentName = item.path.includes("/") ? item.path.slice(item.path.lastIndexOf("/") + 1) : item.path;
            renderRenameInput(rowEl, currentName, (v) => commitFolderRename(item.path, v), cancelFolderRename);
            return;
        }

        const open = S.expanded.has(item.path);
        rowEl.innerHTML =
            `<span class="wfp-chevron">${open ? "▾" : "▸"}</span>` +
            `<span class="wfp-name"></span>` +
            `<span class="wfp-badge">${item.count}</span>`;
        rowEl.querySelector(".wfp-name").textContent = item.name;
        rowEl.title = item.path;
        rowEl.onclick = () => toggleFolder(item.path);
        rowEl.oncontextmenu = (e) => openFolderContextMenu(e, item.path);
        rowEl.draggable = true;
        rowEl.ondragstart = (e) => {
            e.dataTransfer.setData(DRAG_MIME, JSON.stringify({ kind: "folder", path: item.path }));
            e.dataTransfer.effectAllowed = "move";
        };
        rowEl.ondragover = (e) => {
            if (e.dataTransfer.types.includes(DRAG_MIME)) {
                e.preventDefault();
                e.stopPropagation();
                rowEl.classList.add("wfp-drop-target");
            }
        };
        rowEl.ondragleave = () => rowEl.classList.remove("wfp-drop-target");
        rowEl.ondrop = (e) => {
            if (!e.dataTransfer.types.includes(DRAG_MIME)) return;
            e.preventDefault();
            e.stopPropagation();
            rowEl.classList.remove("wfp-drop-target");
            let payload = null;
            try { payload = JSON.parse(e.dataTransfer.getData(DRAG_MIME) || "null"); } catch (_) {}
            if (!payload) return;
            if (payload.kind === "folder" && payload.path) {
                if (payload.path === item.path) return; // dropped onto itself
                if (isDescendantOrSelf(item.path, payload.path)) {
                    toast("error", "Move failed", "Can't move a folder into itself or one of its own subfolders");
                    return;
                }
                applyFolderMove(payload.path, item.path);
            } else if (payload.kind === "files" && payload.keys?.length) {
                applyMove(payload.keys, item.path);
            }
        };
        return;
    }

    if (isFileRenaming) {
        rowEl.innerHTML =
            `<span class="wfp-chevron"></span>` +
            pinIconHtml(item.pinned) +
            `<span class="wfp-icon">◈</span>` +
            `<input class="wfp-rename-input" />`;
        rowEl.onclick = null;
        renderRenameInput(rowEl, filenameOf(item.key), (v) => commitRename(item.key, v), cancelRename);
        return;
    }

    rowEl.innerHTML =
        `<span class="wfp-chevron"></span>` +
        pinIconHtml(item.pinned) +
        `<span class="wfp-icon">◈</span>` +
        `<span class="wfp-name"></span>` +
        `<span class="wfp-date">${fmtDate(dateForSort(item))}</span>`;
    rowEl.querySelector(".wfp-name").textContent = item.name;
    rowEl.title = titleFor(item);
    rowEl.onclick = (e) => handleFileRowClick(e, item.key);
    rowEl.oncontextmenu = (e) => openContextMenu(e, item.key);
    rowEl.draggable = true;
    rowEl.ondragstart = (e) => {
        const keys = S.selected.has(item.key) && S.selected.size > 1 ? [...S.selected] : [item.key];
        e.dataTransfer.setData(DRAG_MIME, JSON.stringify({ kind: "files", keys }));
        e.dataTransfer.effectAllowed = "move";
    };
}

function renderSearchRow(item, rowEl) {
    const isRenaming = S.renamingKey === item.key;
    rowEl.className = "wfp-row" + (S.selected.has(item.key) ? " wfp-selected" : "");
    rowEl.style.paddingLeft = "6px";
    rowEl.draggable = false;
    rowEl.ondragstart = null;

    if (isRenaming) {
        rowEl.innerHTML = pinIconHtml(item.pinned) + `<span class="wfp-icon">◈</span>` + `<input class="wfp-rename-input" />`;
        rowEl.onclick = null;
        rowEl.oncontextmenu = null;
        renderRenameInput(rowEl, filenameOf(item.key), (v) => commitRename(item.key, v), cancelRename);
        return;
    }

    const folder = item.key.includes("/") ? item.key.slice(0, item.key.lastIndexOf("/")) : "";
    rowEl.innerHTML =
        pinIconHtml(item.pinned) +
        `<span class="wfp-icon">◈</span>` +
        `<span class="wfp-name"></span>` +
        `<span class="wfp-folder-secondary"></span>` +
        `<span class="wfp-date">${fmtDate(dateForSort(item))}</span>`;
    rowEl.querySelector(".wfp-name").textContent = item.filename;
    rowEl.querySelector(".wfp-folder-secondary").textContent = folder;
    rowEl.title = titleFor(item);
    rowEl.onclick = (e) => handleFileRowClick(e, item.key);
    rowEl.oncontextmenu = (e) => openContextMenu(e, item.key);
    rowEl.draggable = true;
    rowEl.ondragstart = (e) => {
        const keys = S.selected.has(item.key) && S.selected.size > 1 ? [...S.selected] : [item.key];
        e.dataTransfer.setData(DRAG_MIME, JSON.stringify({ kind: "files", keys }));
        e.dataTransfer.effectAllowed = "move";
    };
}

function titleFor(item) {
    return `${item.key}\nModified: ${fmtDate(item.modified)}   Created: ${fmtDate(item.created)}`;
}

function toggleFolder(path) {
    const opening = !S.expanded.has(path);
    if (opening) S.expanded.add(path);
    else S.expanded.delete(path);

    if (opening) {
        // Opening a folder makes it the active search scope.
        setScopeFolder(path);
    } else if (S.scopeFolder === path) {
        // Collapsing the folder that's currently scoped also clears the
        // scope — the quick way back to "search everything".
        clearScopeFolder();
    } else {
        renderRows();
    }
}

function setScopeFolder(path) {
    S.scopeFolder = path;
    updateScopeUI();
    renderRows();
}
function clearScopeFolder() {
    setScopeFolder("");
}

// ── Selection (Ctrl/Shift multi-select) ───────────────────────────────────────
function fileKeysInOrder() {
    if (S._lastMode === "tree") {
        return S._lastRows.filter((r) => r.kind === "file").map((r) => r.key);
    }
    return S._lastRows.map((r) => r.key);
}

function handleFileRowClick(e, key) {
    if (e.ctrlKey || e.metaKey) {
        toggleSelect(key);
        return;
    }
    if (e.shiftKey && S.lastClickedKey) {
        selectRange(S.lastClickedKey, key);
        return;
    }
    // Plain click: clear any multi-selection and open.
    S.selected.clear();
    S.lastClickedKey = key;
    renderRows();
    openWorkflowByKey(key);
}

function toggleSelect(key) {
    if (S.selected.has(key)) S.selected.delete(key);
    else S.selected.add(key);
    S.lastClickedKey = key;
    renderRows();
}

function selectRange(fromKey, toKey) {
    const keys = fileKeysInOrder();
    const i1 = keys.indexOf(fromKey);
    const i2 = keys.indexOf(toKey);
    if (i1 === -1 || i2 === -1) {
        S.selected.add(toKey);
    } else {
        const [lo, hi] = i1 < i2 ? [i1, i2] : [i2, i1];
        for (let i = lo; i <= hi; i++) S.selected.add(keys[i]);
    }
    S.lastClickedKey = toKey;
    renderRows();
}

// ── Opening a workflow (with correct save-path association) ──────────────────
async function openWorkflowByKey(key) {
    if (S.openingKey === key) return;
    S.openingKey = key;
    try {
        const store = app.extensionManager.workflow;
        const fullPath = `${WF_ROOT}/${key}`;
        const wf = store.getWorkflowByPath?.(fullPath);
        if (wf) {
            if (store.isActive(wf)) return;
            if (!wf.isLoaded) await wf.load();
            await app.loadGraphData(wf.activeState, true, true, wf);
        } else {
            // File newer than last store sync, or store surface changed:
            // fall back to the string-path branch of loadGraphData.
            const resp = await api.getUserData(fullPath);
            if (!resp.ok) throw new Error(`load failed: ${resp.status}`);
            await app.loadGraphData(await resp.json(), true, true, key);
        }
        recordMru(key);
    } catch (err) {
        console.error("[Workflows+] open failed", key, err);
        toast("error", "Could not open workflow", String(err?.message || err));
    } finally {
        S.openingKey = null;
    }
}

// ── MRU ──────────────────────────────────────────────────────────────────────
function recordMru(key) {
    S.mru = pushMru(key);
}

function stripRoot(path) {
    if (!path) return null;
    return path.startsWith(WF_ROOT + "/") ? path.slice(WF_ROOT.length + 1) : path;
}

// Global watcher: captures workflows opened by any means (topbar, menu, drop)
// so the MRU reflects real usage, not just clicks inside this panel.
let _lastActivePath = null;
function startActiveWatcher() {
    setInterval(() => {
        try {
            const wf = app.extensionManager?.workflow?.activeWorkflow;
            const path = wf?.path || null;
            if (path && path !== _lastActivePath && wf?.isPersisted !== false) {
                _lastActivePath = path;
                const key = stripRoot(path);
                if (key) recordMru(key);
            }
        } catch (_) {}
    }, 3000);
}

function openMruPopup(anchorRow) {
    closeMruPopup();
    const present = S.mru.filter((m) => S.entryByKey.has(m.key));
    const list = present.length ? present : S.mru;

    const popup = document.createElement("div");
    popup.className = "wfp-mrupopup";
    if (!list.length) {
        popup.innerHTML = `<div class="wfp-empty">No recent workflows yet</div>`;
    } else {
        for (const m of list) {
            const entry = S.entryByKey.get(m.key);
            const name = entry ? entry.filename : m.key.split("/").pop().replace(/\.[^.]+$/, "");
            const folder = m.key.includes("/") ? m.key.slice(0, m.key.lastIndexOf("/")) : "";
            const row = document.createElement("div");
            row.className = "wfp-mruitem";
            row.innerHTML =
                `<span class="wfp-icon">◈</span>` +
                `<span class="wfp-name"></span>` +
                `<span class="wfp-folder-secondary"></span>`;
            row.querySelector(".wfp-name").textContent = name;
            row.querySelector(".wfp-folder-secondary").textContent = folder;
            row.title = m.key;
            row.onclick = () => {
                closeMruPopup();
                openWorkflowByKey(m.key);
            };
            popup.appendChild(row);
        }
    }
    anchorRow.appendChild(popup);
    S._mruPopup = popup;

    // Close on outside click / Esc.
    setTimeout(() => {
        document.addEventListener("pointerdown", onMruOutside, true);
        document.addEventListener("keydown", onMruKey, true);
    }, 0);
}
function onMruOutside(e) {
    if (S._mruPopup && !S._mruPopup.contains(e.target) && !e.target.closest?.(".wfp-mrubtn")) {
        closeMruPopup();
    }
}
function onMruKey(e) {
    if (e.key === "Escape") closeMruPopup();
}
function closeMruPopup() {
    if (S._mruPopup) {
        S._mruPopup.remove();
        S._mruPopup = null;
        document.removeEventListener("pointerdown", onMruOutside, true);
        document.removeEventListener("keydown", onMruKey, true);
    }
}

// ── Context menu (pin + file/folder operations) ───────────────────────────────
function showMenuAt(menu, e) {
    document.body.appendChild(menu);
    const rect = menu.getBoundingClientRect();
    const x = Math.min(e.clientX, window.innerWidth - rect.width - 4);
    const y = Math.min(e.clientY, window.innerHeight - rect.height - 4);
    menu.style.left = `${Math.max(0, x)}px`;
    menu.style.top = `${Math.max(0, y)}px`;
    S._ctxMenu = menu;

    setTimeout(() => {
        document.addEventListener("pointerdown", onCtxOutside, true);
        document.addEventListener("keydown", onCtxKey, true);
    }, 0);
}
function buildMenu(items) {
    const menu = document.createElement("div");
    menu.className = "wfp-ctxmenu";
    for (const it of items) {
        if (it.sep) {
            const sep = document.createElement("div");
            sep.className = "wfp-ctxsep";
            menu.appendChild(sep);
            continue;
        }
        const row = document.createElement("div");
        row.className = "wfp-ctxitem";
        row.textContent = it.label;
        row.onclick = () => {
            closeContextMenu();
            it.action();
        };
        menu.appendChild(row);
    }
    return menu;
}

function openContextMenu(e, key) {
    e.preventDefault();
    e.stopPropagation();
    closeContextMenu();
    closeMruPopup();

    if (!S.selected.has(key)) {
        S.selected = new Set([key]);
        S.lastClickedKey = key;
        renderRows();
    }
    const keys = [...S.selected];
    const items = keys.length > 1 ? buildBulkMenuItems(keys) : buildSingleMenuItems(keys[0]);
    showMenuAt(buildMenu(items), e);
}

function buildSingleMenuItems(key) {
    const folder = folderOf(key);
    const pinned = isPinned(key);
    const inTrash = isInTrash(key);
    return [
        { label: "Rename", action: () => { S.renamingKey = key; renderRows(); } },
        { label: "Duplicate", action: () => duplicateKeys([key]) },
        { label: "Move to…", action: () => openMoveDialog([key]) },
        { sep: true },
        { label: pinned ? "Unpin" : "Pin", action: () => { togglePin(key); renderRows(); } },
        { sep: true },
        { label: inTrash ? "Delete Permanently" : "Delete", action: () => deleteKeys([key]) },
        { sep: true },
        {
            label: `Reset all pins for "${folder || "(root)"}"`,
            action: () => { resetFolderPins(folder); renderRows(); },
        },
        { label: "Reset all pins", action: () => { resetAllPins(); renderRows(); } },
    ];
}

function buildBulkMenuItems(keys) {
    const n = keys.length;
    const allPinned = keys.every((k) => isPinned(k));
    const allInTrash = keys.every(isInTrash);
    const folders = new Set(keys.map(folderOf));
    const sameFolder = folders.size === 1 ? [...folders][0] : null;

    const items = [
        { label: `Duplicate ${n} files`, action: () => duplicateKeys(keys) },
        { label: `Move ${n} files to…`, action: () => openMoveDialog(keys) },
        { sep: true },
        {
            label: allPinned ? `Unpin ${n} files` : `Pin ${n} files`,
            action: () => bulkTogglePin(keys, !allPinned),
        },
        { sep: true },
        {
            label: allInTrash ? `Delete ${n} files Permanently` : `Delete ${n} files`,
            action: () => deleteKeys(keys),
        },
        { sep: true },
    ];
    if (sameFolder !== null) {
        items.push({
            label: `Reset all pins for "${sameFolder || "(root)"}"`,
            action: () => { resetFolderPins(sameFolder); renderRows(); },
        });
    }
    items.push({ label: "Reset all pins", action: () => { resetAllPins(); renderRows(); } });
    return items;
}

function openFolderContextMenu(e, folderPath) {
    e.preventDefault();
    e.stopPropagation();
    closeContextMenu();
    closeMruPopup();

    let items;
    if (folderPath === TRASH_FOLDER) {
        const trashKeys = S.entries.filter((en) => isInTrash(en.key)).map((en) => en.key);
        items = trashKeys.length
            ? [{ label: `Empty Trash (${trashKeys.length} files)`, action: () => deleteKeys(trashKeys) }]
            : [{ label: "Trash is empty", action: () => {} }];
    } else if (!folderPath) {
        return; // no row represents the root folder; nothing to show
    } else {
        items = [
            { label: "Rename", action: () => { S.renamingFolder = folderPath; renderRows(); } },
            { label: "Move to…", action: () => openMoveDialog(null, folderPath) },
            { sep: true },
            { label: "Delete", action: () => deleteFolder(folderPath) },
            { sep: true },
            {
                label: `Reset all pins for "${folderPath}"`,
                action: () => { resetFolderPins(folderPath); renderRows(); },
            },
        ];
    }
    showMenuAt(buildMenu(items), e);
}

function onCtxOutside(e) {
    if (S._ctxMenu && !S._ctxMenu.contains(e.target)) closeContextMenu();
}
function onCtxKey(e) {
    if (e.key === "Escape") closeContextMenu();
}
function closeContextMenu() {
    if (S._ctxMenu) {
        S._ctxMenu.remove();
        S._ctxMenu = null;
        document.removeEventListener("pointerdown", onCtxOutside, true);
        document.removeEventListener("keydown", onCtxKey, true);
    }
}

// ── Inline rename (files) ─────────────────────────────────────────────────────
async function commitRename(key, newBaseNameRaw) {
    if (S.renamingKey !== key) return; // already resolved (e.g. Enter then blur)
    const newBase = (newBaseNameRaw || "").trim();
    S.renamingKey = null;

    if (!newBase || newBase === filenameOf(key)) {
        renderRows();
        return;
    }
    const newKey = renameKey(key, newBase);
    if (S.entryByKey.has(newKey)) {
        toast("error", "Rename failed", `"${newBase}" already exists in this folder`);
        renderRows();
        return;
    }

    showOverlay("Renaming…");
    try {
        await moveOneFile(key, newKey);
        pinsMigrateKey(key, newKey);
        S.mru = mruMigrateKey(key, newKey);
        if (S.selected.has(key)) {
            S.selected.delete(key);
            S.selected.add(newKey);
        }
        S.lastClickedKey = newKey;
        await rebuildIndex({ sync: false });
        toast("success", "Renamed", newBase);
    } catch (err) {
        console.error("[Workflows+] rename failed", key, err);
        toast("error", "Rename failed", String(err?.message || err));
        renderRows();
    } finally {
        hideOverlay();
    }
}
function cancelRename() {
    S.renamingKey = null;
    renderRows();
}

// ── Inline rename (folders) ───────────────────────────────────────────────────
async function commitFolderRename(folderPath, newNameRaw) {
    if (S.renamingFolder !== folderPath) return;
    const newName = (newNameRaw || "").trim();
    S.renamingFolder = null;

    const currentName = folderPath.includes("/") ? folderPath.slice(folderPath.lastIndexOf("/") + 1) : folderPath;
    if (!newName || newName === currentName) {
        renderRows();
        return;
    }
    const parent = folderOf(folderPath); // folderOf on a folder path returns its parent
    const newPath = parent ? `${parent}/${newName}` : newName;

    if (allFolderPaths().includes(newPath)) {
        toast("error", "Rename failed", `"${newName}" already exists`);
        renderRows();
        return;
    }

    showOverlay("Renaming folder…");
    try {
        await moveOneFolder(folderPath, newPath);
        migrateFolderPrefixEverywhere(folderPath, newPath);
        await rebuildIndex({ sync: true });
        toast("success", "Renamed folder", newName);
    } catch (err) {
        console.error("[Workflows+] folder rename failed", folderPath, err);
        toast("error", "Rename failed", String(err?.message || err));
        renderRows();
    } finally {
        hideOverlay();
    }
}
function cancelFolderRename() {
    S.renamingFolder = null;
    renderRows();
}

// ── File operation primitives (network) ───────────────────────────────────────
async function moveOneFile(from, to, { overwrite = false } = {}) {
    const store = app.extensionManager.workflow;
    const fullFrom = `${WF_ROOT}/${from}`;
    const fullTo = `${WF_ROOT}/${to}`;
    const wf = store?.getWorkflowByPath?.(fullFrom);
    if (wf && typeof store.renameWorkflow === "function") {
        try {
            await store.renameWorkflow(wf, fullTo);
            return;
        } catch (err) {
            console.warn("[Workflows+] store.renameWorkflow failed, falling back to raw move", err);
        }
    }
    const params = new URLSearchParams({ overwrite: overwrite ? "true" : "false" });
    const resp = await api.fetchApi(
        `/userdata/${encodeURIComponent(fullFrom)}/move/${encodeURIComponent(fullTo)}?${params}`,
        { method: "POST" }
    );
    if (resp.status === 409) throw new Error(`"${to.split("/").pop()}" already exists at the destination`);
    if (!resp.ok) throw new Error(`move failed: ${resp.status}`);
}

async function hardDeleteOneFile(key) {
    const store = app.extensionManager.workflow;
    const full = `${WF_ROOT}/${key}`;
    const wf = store?.getWorkflowByPath?.(full);
    if (wf && typeof store.deleteWorkflow === "function") {
        try {
            await store.deleteWorkflow(wf);
            return;
        } catch (err) {
            console.warn("[Workflows+] store.deleteWorkflow failed, falling back to raw delete", err);
        }
    }
    const resp = await api.fetchApi(`/userdata/${encodeURIComponent(full)}`, { method: "DELETE" });
    if (!resp.ok && resp.status !== 404) throw new Error(`delete failed: ${resp.status}`);
}

async function duplicateOneFile(key, newKey) {
    const full = `${WF_ROOT}/${key}`;
    const resp = await api.getUserData(full);
    if (!resp.ok) throw new Error(`read failed: ${resp.status}`);
    const blob = await resp.blob();
    const newFull = `${WF_ROOT}/${newKey}`;
    const putResp = await api.fetchApi(`/userdata/${encodeURIComponent(newFull)}?overwrite=false`, {
        method: "POST",
        body: blob,
    });
    if (!putResp.ok) throw new Error(`duplicate failed: ${putResp.status}`);
}

// ── Folder operation primitives (network) ─────────────────────────────────────
// `full_info` is deliberately omitted here: ComfyUI's get_file_info() calls
// os.path.getsize() unconditionally when full_info=true, which throws
// IsADirectoryError for a folder path. Without it the endpoint just returns
// the relative path string, which we don't need anyway.
async function moveOneFolder(from, to) {
    const fullFrom = `${WF_ROOT}/${from}`;
    const fullTo = `${WF_ROOT}/${to}`;
    const resp = await api.fetchApi(
        `/userdata/${encodeURIComponent(fullFrom)}/move/${encodeURIComponent(fullTo)}?overwrite=false`,
        { method: "POST" }
    );
    if (resp.status === 409) throw new Error(`"${to.split("/").pop()}" already exists at the destination`);
    if (!resp.ok) throw new Error(`move failed: ${resp.status}`);
}

// Rewrite every path/key under `oldPrefix` (the folder itself and everything
// nested inside it) onto `newPrefix`, across all local + persisted state:
// expansion, scope, selection, pins, and MRU. Called after any folder
// rename/move/delete, since those operations affect every file underneath
// in one move, not just a single key.
function migrateFolderPrefixEverywhere(oldPrefix, newPrefix) {
    S.expanded = new Set([...S.expanded].map((p) => rewritePrefix(p, oldPrefix, newPrefix)));
    if (S.scopeFolder) S.scopeFolder = rewritePrefix(S.scopeFolder, oldPrefix, newPrefix);
    S.selected = new Set([...S.selected].map((k) => rewritePrefix(k, oldPrefix, newPrefix)));
    if (S.lastClickedKey) S.lastClickedKey = rewritePrefix(S.lastClickedKey, oldPrefix, newPrefix);

    for (const entry of S.entries) {
        if (entry.key === oldPrefix || entry.key.startsWith(`${oldPrefix}/`)) {
            const newKey = rewritePrefix(entry.key, oldPrefix, newPrefix);
            pinsMigrateKey(entry.key, newKey);
            S.mru = mruMigrateKey(entry.key, newKey);
        }
    }
}

function normalizeFolderInput(raw) {
    return String(raw || "")
        .replace(/\\/g, "/")
        .trim()
        .replace(/^\/+|\/+$/g, "")
        .replace(/\/{2,}/g, "/");
}

async function applyFolderMove(folderPath, destFolderRaw) {
    closeMoveDialog();
    const destFolder = normalizeFolderInput(destFolderRaw);

    if (isDescendantOrSelf(destFolder, folderPath)) {
        toast("error", "Move failed", "Can't move a folder into itself or one of its own subfolders");
        return;
    }
    const folderName = folderPath.includes("/") ? folderPath.slice(folderPath.lastIndexOf("/") + 1) : folderPath;
    const newPath = destFolder ? `${destFolder}/${folderName}` : folderName;
    if (newPath === folderPath) return; // already there

    if (allFolderPaths().includes(newPath)) {
        toast("error", "Move failed", `"${folderName}" already exists at the destination`);
        return;
    }

    showOverlay("Moving folder…");
    try {
        await moveOneFolder(folderPath, newPath);
        migrateFolderPrefixEverywhere(folderPath, newPath);
        await rebuildIndex({ sync: true });
        toast("success", "Moved folder", `"${folderName}" to "${destFolder || "(root)"}"`);
    } catch (err) {
        console.error("[Workflows+] folder move failed", folderPath, err);
        toast("error", "Move failed", String(err?.message || err));
    } finally {
        hideOverlay();
    }
}

async function deleteFolder(folderPath) {
    // Defensive: no row ever represents the workflows root ("") and _trash
    // has its own Empty Trash flow, so neither should reach here — but
    // guard explicitly since this is a destructive operation.
    if (!folderPath || folderPath === TRASH_FOLDER) return;

    const node = findTreeNode(S.tree, folderPath);
    const count = node ? node.count : 0;
    const ok = await app.extensionManager.dialog.confirm({
        title: "Move folder to trash",
        message: `Move "${folderPath.split("/").pop()}" and its ${count} file${count === 1 ? "" : "s"} to _trash?`,
        type: "delete",
    });
    if (!ok) return;

    showOverlay("Moving folder to trash…");
    try {
        const trashPath = trashKeyForFolder(folderPath);
        await moveOneFolder(folderPath, trashPath);
        migrateFolderPrefixEverywhere(folderPath, trashPath);
        await rebuildIndex({ sync: true });
        toast("success", "Moved to trash", `Folder "${folderPath.split("/").pop()}"`);
    } catch (err) {
        console.error("[Workflows+] folder delete failed", folderPath, err);
        toast("error", "Delete failed", String(err?.message || err));
    } finally {
        hideOverlay();
    }
}

// ── Bulk file operations (rebuild the index once at the end) ─────────────────
async function duplicateKeys(keys) {
    closeContextMenu();
    showOverlay(keys.length > 1 ? `Duplicating ${keys.length} files…` : "Duplicating…");
    const createdKeys = [];
    const failures = [];
    for (const key of keys) {
        try {
            const destFolder = folderOf(key);
            const existingInFolder = new Set(
                S.entries.filter((e) => folderOf(e.key) === destFolder).map((e) => e.key)
            );
            const newKey = nextCopyKey(key, destFolder, existingInFolder);
            if (!newKey) throw new Error("too many copies already exist");
            await duplicateOneFile(key, newKey);
            createdKeys.push(newKey);
        } catch (err) {
            console.error("[Workflows+] duplicate failed", key, err);
            failures.push(key);
        }
    }
    S.selected = new Set(createdKeys);
    await rebuildIndex({ sync: false });
    hideOverlay();
    if (failures.length) toast("error", "Some duplicates failed", failures.join(", "));
    else toast("success", "Duplicated", `${createdKeys.length} file${createdKeys.length === 1 ? "" : "s"}`);
}

function bulkTogglePin(keys, pinTo) {
    closeContextMenu();
    for (const key of keys) {
        if (isPinned(key) !== pinTo) togglePin(key);
    }
    renderRows();
}

async function applyMove(keys, destFolderRaw) {
    closeMoveDialog();
    const destFolder = normalizeFolderInput(destFolderRaw);

    showOverlay(keys.length > 1 ? `Moving ${keys.length} files…` : "Moving…");
    const failures = [];
    let movedCount = 0;
    for (const key of keys) {
        const newKey = moveKey(key, destFolder);
        if (newKey === key) continue; // already at destination
        if (S.entryByKey.has(newKey)) {
            failures.push(`${key} (already exists at destination)`);
            continue;
        }
        try {
            await moveOneFile(key, newKey);
            pinsMigrateKey(key, newKey);
            S.mru = mruMigrateKey(key, newKey);
            if (S.selected.has(key)) {
                S.selected.delete(key);
                S.selected.add(newKey);
            }
            movedCount++;
        } catch (err) {
            console.error("[Workflows+] move failed", key, err);
            failures.push(key);
        }
    }
    await rebuildIndex({ sync: false });
    hideOverlay();
    if (failures.length) toast("error", "Some moves failed", failures.join(", "));
    else if (movedCount) toast("success", "Moved", `${movedCount} file${movedCount === 1 ? "" : "s"} to "${destFolder || "(root)"}"`);
}

async function deleteKeys(keys) {
    const allInTrash = keys.every(isInTrash);
    const n = keys.length;
    closeContextMenu();
    const ok = await app.extensionManager.dialog.confirm({
        title: allInTrash ? "Permanently delete" : "Move to trash",
        message: allInTrash
            ? `Permanently delete ${n} file${n === 1 ? "" : "s"}?`
            : `Move ${n} file${n === 1 ? "" : "s"} to _trash?`,
        type: "delete",
        itemList: n > 1 ? keys.map(k => k.split("/").pop()) : undefined,
        hint: allInTrash ? "This cannot be undone." : undefined,
    });
    if (!ok) return;

    showOverlay(allInTrash ? "Deleting…" : "Moving to trash…");
    const failures = [];
    for (const key of keys) {
        try {
            if (allInTrash) {
                await hardDeleteOneFile(key);
                pinsMigrateKey(key, null);
                S.mru = mruMigrateKey(key, null);
            } else {
                const trashKey = trashKeyFor(key);
                await moveOneFile(key, trashKey);
                pinsMigrateKey(key, trashKey);
                S.mru = mruMigrateKey(key, trashKey);
            }
            S.selected.delete(key);
        } catch (err) {
            console.error("[Workflows+] delete failed", key, err);
            failures.push(key);
        }
    }
    await rebuildIndex({ sync: false });
    hideOverlay();
    if (failures.length) toast("error", "Some deletes failed", failures.join(", "));
    else toast("success", allInTrash ? "Deleted permanently" : "Moved to trash", `${n} file${n === 1 ? "" : "s"}`);
}

// ── Move-to-folder dialog (files or a single folder) ──────────────────────────
function openMoveDialog(keys, folderPath = null) {
    closeContextMenu();
    closeMoveDialog();

    const isFolder = !!folderPath;
    let folders = allFolderPaths();
    if (isFolder) folders = folders.filter((f) => !isDescendantOrSelf(f, folderPath));

    const folderSet = isFolder ? null : new Set(keys.map(folderOf));
    const commonFolder = isFolder ? folderOf(folderPath) : (folderSet.size === 1 ? [...folderSet][0] : "");

    const overlay = document.createElement("div");
    overlay.className = "wfp-modal-overlay";
    const dialog = document.createElement("div");
    dialog.className = "wfp-modal";
    dialog.innerHTML =
        `<div class="wfp-modal-title"></div>` +
        `<input class="wfp-modal-input" type="text" placeholder="Folder path (blank = root), or type a new one…" />` +
        `<div class="wfp-modal-list"></div>` +
        `<div class="wfp-modal-buttons">` +
        `<button class="wfp-btn wfp-modal-cancel">Cancel</button>` +
        `<button class="wfp-btn wfp-modal-ok">Move</button>` +
        `</div>`;
    dialog.querySelector(".wfp-modal-title").textContent = isFolder
        ? `Move folder "${folderPath.split("/").pop()}" to…`
        : `Move ${keys.length} file${keys.length === 1 ? "" : "s"} to…`;

    const input = dialog.querySelector(".wfp-modal-input");
    input.value = commonFolder;
    const list = dialog.querySelector(".wfp-modal-list");

    const renderList = () => {
        const filter = input.value.trim().toLowerCase();
        list.innerHTML = "";
        const matches = folders.filter((f) => f.toLowerCase().includes(filter));
        for (const f of matches.slice(0, 200)) {
            const row = document.createElement("div");
            row.className = "wfp-modal-item";
            row.textContent = f || "(root)";
            row.onclick = () => { input.value = f; };
            list.appendChild(row);
        }
        if (!matches.length && input.value.trim()) {
            const row = document.createElement("div");
            row.className = "wfp-modal-item wfp-modal-newfolder";
            row.textContent = `+ Create "${input.value.trim()}"`;
            list.appendChild(row);
        }
    };
    input.addEventListener("input", renderList);
    renderList();

    const doMove = () => (isFolder ? applyFolderMove(folderPath, input.value) : applyMove(keys, input.value));
    dialog.querySelector(".wfp-modal-ok").onclick = doMove;
    dialog.querySelector(".wfp-modal-cancel").onclick = closeMoveDialog;
    input.addEventListener("keydown", (e) => {
        e.stopPropagation();
        if (e.key === "Enter") { e.preventDefault(); doMove(); }
        else if (e.key === "Escape") { e.preventDefault(); closeMoveDialog(); }
    });
    overlay.onclick = (e) => { if (e.target === overlay) closeMoveDialog(); };

    overlay.appendChild(dialog);
    document.body.appendChild(overlay);
    S._moveDialog = overlay;
    requestAnimationFrame(() => input.focus());
}
function closeMoveDialog() {
    if (S._moveDialog) {
        S._moveDialog.remove();
        S._moveDialog = null;
    }
}

// ── Overlay / empty helpers ──────────────────────────────────────────────────
function showOverlay(text) {
    if (!S.el) return;
    hideOverlay();
    const o = document.createElement("div");
    o.className = "wfp-overlay";
    o.textContent = text;
    S.el.appendChild(o);
    S._overlay = o;
}
function hideOverlay() {
    if (S._overlay) {
        S._overlay.remove();
        S._overlay = null;
    }
}
function showEmpty(text) {
    if (S.vlist) S.vlist.setItems([]);
    if (S.scroller) {
        const e = document.createElement("div");
        e.className = "wfp-empty";
        e.textContent = text;
        S.scroller.appendChild(e);
    }
}

function toast(severity, summary, detail) {
    try {
        app.extensionManager.toast.add({ severity, summary, detail, life: severity === "error" ? undefined : 4000 });
    } catch (_) {
        console.log(`[Workflows+] ${severity}: ${summary} — ${detail}`);
    }
}

// ── Sort header ──────────────────────────────────────────────────────────────
function setSort(field) {
    if (S.sortSpec.field === field) {
        S.sortSpec = { field, dir: S.sortSpec.dir * -1 };
    } else {
        // Sensible default direction: names A→Z, dates newest first.
        S.sortSpec = { field, dir: field === "name" ? 1 : -1 };
    }
    saveSortSpec();
    updateSortButtons();
    renderRows();
}
function updateSortButtons() {
    for (const [field, btn] of Object.entries(S.sortBtns)) {
        const active = S.sortSpec.field === field;
        btn.classList.toggle("active", active);
        const label = field.charAt(0).toUpperCase() + field.slice(1);
        btn.textContent = active ? `${label} ${S.sortSpec.dir === 1 ? "▲" : "▼"}` : label;
    }
}

// ── Search mode (Name / Contains) ─────────────────────────────────────────────
function setSearchMode(mode) {
    if (S.searchMode === mode) return;
    cancelContainsSearch();
    S.searchMode = mode;
    S.containsResults = null;
    updateModeButtons();
    updateSearchPlaceholder();
    if (S.searchInput) S.searchInput.value = mode === "contains" ? S.containsQuery : S.searchText;
    renderRows();
}
function updateModeButtons() {
    for (const [mode, btn] of Object.entries(S.modeBtns)) {
        btn.classList.toggle("active", S.searchMode === mode);
    }
}
function updateSearchPlaceholder() {
    if (!S.searchInput) return;
    S.searchInput.placeholder =
        S.searchMode === "contains"
            ? "Search inside workflows (press Enter)…"
            : 'Search names: "black cat", qwen AND inpaint…';
}

// ── Contains (server-side content) search ─────────────────────────────────────
function updateProgressUI() {
    if (!S.progressRow) return;
    const show = S.searchMode === "contains" && (S.containsScanning || S.containsTotal > 0);
    S.progressRow.style.display = show ? "flex" : "none";
    if (!show) return;

    const pct = S.containsTotal > 0 ? Math.round((S.containsScanned / S.containsTotal) * 100) : 0;
    if (S.progressFill) S.progressFill.style.width = `${pct}%`;
    if (S.progressText) {
        S.progressText.textContent = S.containsScanning
            ? `Scanning ${S.containsScanned}/${S.containsTotal}…`
            : `Scanned ${S.containsTotal} file${S.containsTotal === 1 ? "" : "s"}`;
    }
    if (S.cancelBtn) S.cancelBtn.style.display = S.containsScanning ? "" : "none";
}

// Global fallback so Esc cancels an in-progress scan even when focus isn't
// in the search input (e.g. the user clicked onto the canvas while it runs).
function onGlobalEscapeDuringScan(e) {
    if (e.key === "Escape" && S.containsScanning) {
        cancelContainsSearch();
        renderRows();
    }
}

async function runContainsSearch(query) {
    cancelContainsSearch();
    if (!query || !query.trim()) {
        S.containsResults = null;
        S.containsTotal = 0;
        S.containsScanned = 0;
        renderRows();
        return;
    }

    const controller = new AbortController();
    S.containsAbort = controller;
    S.containsScanning = true;
    S.containsScanned = 0;
    S.containsTotal = 0;
    S.containsResults = null;
    document.addEventListener("keydown", onGlobalEscapeDuringScan, true);
    renderRows();

    const params = new URLSearchParams({ q: query });
    if (S.scopeFolder) params.set("folder", S.scopeFolder);
    if (!S.searchSubfolders) params.set("recurse", "0");

    try {
        const resp = await api.fetchApi(`/coachbate/workflows/grep?${params.toString()}`, {
            signal: controller.signal,
        });
        if (!resp.ok || !resp.body) {
            const body = await resp.json().catch(() => ({}));
            throw new Error(body.error || `grep failed: ${resp.status}`);
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = buffer.indexOf("\n")) !== -1) {
                const line = buffer.slice(0, idx);
                buffer = buffer.slice(idx + 1);
                if (!line.trim()) continue;
                handleGrepLine(JSON.parse(line));
            }
        }
        if (buffer.trim()) handleGrepLine(JSON.parse(buffer));
    } catch (err) {
        if (err?.name === "AbortError") {
            // User cancelled — leave the panel in a clean idle state.
            S.containsResults = null;
        } else {
            console.error("[Workflows+] contains search failed", err);
            toast("error", "Contains search failed", String(err?.message || err));
            S.containsResults = [];
        }
    } finally {
        S.containsScanning = false;
        if (S.containsAbort === controller) S.containsAbort = null;
        document.removeEventListener("keydown", onGlobalEscapeDuringScan, true);
        renderRows();
    }
}

function handleGrepLine(obj) {
    if (obj.type === "total") {
        S.containsTotal = obj.total;
        updateProgressUI();
    } else if (obj.type === "progress") {
        S.containsScanned = obj.scanned;
        S.containsTotal = obj.total;
        updateProgressUI();
    } else if (obj.type === "done") {
        const entries = obj.matches.map(rawToEntry).filter(Boolean);
        S.containsResults = searchFiles(entries, null, S.sortSpec, {
            isPinnedFn: (key) => isPinned(key),
        });
    }
}

function cancelContainsSearch() {
    if (S.containsAbort) {
        S.containsAbort.abort();
        S.containsAbort = null;
    }
    S.containsScanning = false;
}

// ── Subfolder scope toggle + scope bar ────────────────────────────────────────
function setSearchSubfolders(value) {
    S.searchSubfolders = value;
    saveSearchSubfolders();
    if (S.searchMode === "contains") {
        // Re-run the active scan under the new scope if there is one.
        if (S.containsQuery.trim()) runContainsSearch(S.containsQuery);
    } else {
        renderRows();
    }
}

function updateScopeUI() {
    if (!S.scopeLabel) return;
    const scoped = !!S.scopeFolder;

    S.scopeLabel.textContent = "";
    if (scoped) {
        S.scopeLabel.append("📁 Searching in ");
        const strong = document.createElement("strong");
        strong.textContent = `"${S.scopeFolder}"`;
        S.scopeLabel.appendChild(strong);
    } else {
        S.scopeLabel.textContent = "Search subfolders";
    }

    if (S.scopeClearBtn) S.scopeClearBtn.style.display = scoped ? "" : "none";
    if (S.scopeRow) S.scopeRow.classList.toggle("wfp-scoped-active", scoped);
}

// ── Drag and drop (external OS files) ─────────────────────────────────────────
function wireDragAndDrop(root) {
    let hint = null;
    const showHint = () => {
        root.classList.add("wfp-dragover");
        if (!hint) {
            hint = document.createElement("div");
            hint.className = "wfp-drophint";
            hint.textContent = "Drop workflow, image, or video to open";
            root.appendChild(hint);
        }
    };
    const clearHint = () => {
        root.classList.remove("wfp-dragover");
        if (hint) { hint.remove(); hint = null; }
    };

    root.addEventListener("dragover", (e) => {
        if (e.dataTransfer?.types?.includes("Files")) {
            e.preventDefault();
            e.dataTransfer.dropEffect = "copy";
            showHint();
        }
    });
    root.addEventListener("dragleave", (e) => {
        if (e.target === root && !root.contains(e.relatedTarget)) clearHint();
    });
    root.addEventListener("drop", async (e) => {
        // Internal file/folder-row → folder-row moves are handled (and
        // stopPropagation'd) by the folder row's own drop handler; this
        // only sees external OS drops.
        e.preventDefault();
        e.stopPropagation();
        clearHint();
        const files = Array.from(e.dataTransfer?.files || []);
        if (!files.length) return;
        if (typeof app.handleFile !== "function") {
            toast("error", "Drag-and-drop unavailable", "app.handleFile is missing in this frontend version.");
            return;
        }
        for (const f of files) {
            try {
                await app.handleFile(f, "file_drop", { deferWarnings: true });
            } catch (err) {
                console.error("[Workflows+] drop handleFile failed", f?.name, err);
                toast("error", `Could not open ${f?.name || "file"}`, String(err?.message || err));
            }
        }
    });
}

// ── Panel build (called every time the tab is shown) ─────────────────────────
function renderPanel(el) {
    ensureStyles();
    S.el = el;
    el.classList.add("wfp-root");
    el.innerHTML = "";
    closeMruPopup();
    closeContextMenu();
    closeMoveDialog();

    // Header: sort buttons + count + refresh.
    const header = document.createElement("div");
    header.className = "wfp-header";
    const sortGroup = document.createElement("div");
    sortGroup.className = "wfp-sortgroup";
    S.sortBtns = {};
    for (const field of ["name", "modified", "created"]) {
        const btn = document.createElement("button");
        btn.className = "wfp-btn";
        btn.onclick = () => setSort(field);
        S.sortBtns[field] = btn;
        sortGroup.appendChild(btn);
    }
    const count = document.createElement("span");
    count.className = "wfp-count";
    S.countEl = count;
    const refresh = document.createElement("button");
    refresh.className = "wfp-btn";
    refresh.textContent = "⟳";
    refresh.title = "Refresh workflow list";
    refresh.onclick = () => rebuildIndex({ sync: true });
    header.append(sortGroup, count, refresh);

    // Search row: mode toggle + input.
    const searchRow = document.createElement("div");
    searchRow.className = "wfp-searchrow";

    const modeGroup = document.createElement("div");
    modeGroup.className = "wfp-modegroup";
    S.modeBtns = {};
    for (const [mode, label] of [["name", "Name"], ["contains", "Contains"]]) {
        const btn = document.createElement("button");
        btn.className = "wfp-btn";
        btn.textContent = label;
        btn.title = mode === "contains"
            ? "Search inside workflow JSON (slower, server-side)"
            : "Search workflow filenames";
        btn.onclick = () => setSearchMode(mode);
        S.modeBtns[mode] = btn;
        modeGroup.appendChild(btn);
    }

    const search = document.createElement("input");
    search.className = "wfp-search";
    search.type = "text";
    search.value = S.searchMode === "contains" ? S.containsQuery : S.searchText;
    S.searchInput = search;

    let debounce = null;
    search.addEventListener("input", () => {
        if (S.searchMode === "contains") return; // contains mode runs on Enter only
        clearTimeout(debounce);
        debounce = setTimeout(() => {
            S.searchText = search.value;
            renderRows();
        }, 150);
    });
    search.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && S.searchMode === "contains") {
            S.containsQuery = search.value;
            runContainsSearch(S.containsQuery);
            return;
        }
        if (e.key === "Escape") {
            if (S.searchMode === "contains" && S.containsScanning) {
                cancelContainsSearch();
                renderRows();
                return;
            }
            search.value = "";
            if (S.searchMode === "contains") {
                S.containsQuery = "";
                S.containsResults = null;
                S.containsTotal = 0;
            } else {
                S.searchText = "";
            }
            renderRows();
        }
    });

    searchRow.append(modeGroup, search);

    // Subfolder-scope toggle + current folder scope indicator (prominent bar
    // when scoped — click the scoped folder again in the tree, or the "Show
    // All" button here, to go back to searching the whole library).
    const subRow = document.createElement("label");
    subRow.className = "wfp-subfolderrow";
    S.scopeRow = subRow;
    const subCheckbox = document.createElement("input");
    subCheckbox.type = "checkbox";
    subCheckbox.checked = S.searchSubfolders;
    subCheckbox.onchange = () => setSearchSubfolders(subCheckbox.checked);
    S.subfolderToggle = subCheckbox;
    const subLabel = document.createElement("span");
    subLabel.className = "wfp-scopelabel";
    S.scopeLabel = subLabel;
    const clearScopeBtn = document.createElement("button");
    clearScopeBtn.className = "wfp-btn wfp-scopeclear";
    clearScopeBtn.textContent = "✕ Show All";
    clearScopeBtn.title = "Clear folder scope — search the whole library again";
    clearScopeBtn.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        clearScopeFolder();
    };
    S.scopeClearBtn = clearScopeBtn;
    subRow.append(subCheckbox, subLabel, clearScopeBtn);

    // Contains-search progress row (hidden unless scanning / just finished).
    const progressRow = document.createElement("div");
    progressRow.className = "wfp-progressrow";
    progressRow.style.display = "none";
    const progressBar = document.createElement("div");
    progressBar.className = "wfp-progressbar";
    const progressFill = document.createElement("div");
    progressFill.className = "wfp-progressfill";
    progressBar.appendChild(progressFill);
    const progressText = document.createElement("span");
    progressText.className = "wfp-progresstext";
    const cancelBtn = document.createElement("button");
    cancelBtn.className = "wfp-btn";
    cancelBtn.textContent = "Cancel";
    cancelBtn.onclick = () => { cancelContainsSearch(); renderRows(); };
    progressRow.append(progressBar, progressText, cancelBtn);
    S.progressRow = progressRow;
    S.progressFill = progressFill;
    S.progressText = progressText;
    S.cancelBtn = cancelBtn;

    // MRU row.
    const mruRow = document.createElement("div");
    mruRow.className = "wfp-mrurow";
    const mruBtn = document.createElement("button");
    mruBtn.className = "wfp-btn wfp-mrubtn";
    mruBtn.textContent = "Recent ▾";
    mruBtn.title = "Recently opened workflows";
    mruBtn.onclick = () => {
        if (S._mruPopup) closeMruPopup();
        else openMruPopup(mruRow);
    };
    mruRow.appendChild(mruBtn);

    // Scroller (virtual list host).
    const scroller = document.createElement("div");
    scroller.className = "wfp-scroller";
    S.scroller = scroller;

    el.append(header, searchRow, subRow, progressRow, mruRow, scroller);

    wireDragAndDrop(el);

    S.vlist = new VirtualList({
        scroller,
        rowHeight: ROW_H,
        renderRow: renderTreeRow,
    });

    updateSortButtons();
    updateModeButtons();
    updateSearchPlaceholder();
    updateScopeUI();

    // Populate: use cache if fresh, else fetch.
    if (S.entries.length && Date.now() - S.lastFetchMs < FRESH_MS) {
        renderRows();
        // Restore prior scroll position after layout settles.
        requestAnimationFrame(() => { if (S.scroller) S.scroller.scrollTop = S.scrollTop; });
    } else {
        rebuildIndex({ sync: false });
    }
}

function destroyPanel() {
    if (S.scroller) S.scrollTop = S.scroller.scrollTop;
    closeMruPopup();
    closeContextMenu();
    closeMoveDialog();
    hideOverlay();
    S.selected.clear();
    S.lastClickedKey = null;
    S.renamingKey = null;
    S.renamingFolder = null;
    if (S.vlist) { S.vlist.destroy(); S.vlist = null; }
    S.el = null;
    S.scroller = null;
    S.countEl = null;
    S.searchInput = null;
    S.sortBtns = {};
    S.modeBtns = {};
    S.subfolderToggle = null;
    S.scopeRow = null;
    S.scopeLabel = null;
    S.scopeClearBtn = null;
    S.progressRow = null;
    S.progressFill = null;
    S.progressText = null;
    S.cancelBtn = null;
}

// ── Registration ─────────────────────────────────────────────────────────────
app.registerExtension({
    name: "CoachBate.WorkflowsPlus",
    async setup() {
        startActiveWatcher();

        const tabDef = {
            id: "coachbate-workflows-plus",
            icon: "pi pi-folder-open",
            title: "Workflows+",
            tooltip: "Fast workflow browser (thousands-safe) with AND/OR search",
            type: "custom",
            render: renderPanel,
            destroy: destroyPanel,
        };

        // Register at the top of the sidebar, before the built-in tabs
        // (Assets, Node Library, ...). The extension-manager wrapper doesn't
        // expose the `prepend` option, so we go straight to the underlying
        // store, which does.
        try {
            const sidebarStore = app.extensionManager?.sidebarTab;
            if (sidebarStore && typeof sidebarStore.registerSidebarTab === "function") {
                sidebarStore.registerSidebarTab(tabDef, { prepend: true });
            } else {
                app.extensionManager.registerSidebarTab(tabDef);
            }
        } catch (err) {
            console.warn("[Workflows+] prepend registration failed, falling back", err);
            try { app.extensionManager.registerSidebarTab(tabDef); } catch (_) {}
        }

        // Defensive fallback: if a core tab still ended up registering after
        // ours (registration order isn't strictly guaranteed across
        // extensions), move ours back to the front once things settle.
        setTimeout(() => {
            try {
                const tabs = app.extensionManager?.sidebarTab?.sidebarTabs;
                if (!tabs || !tabs.length) return;
                const idx = tabs.findIndex((t) => t.id === "coachbate-workflows-plus");
                if (idx > 0) {
                    const [tab] = tabs.splice(idx, 1);
                    tabs.unshift(tab);
                }
            } catch (err) {
                console.warn("[Workflows+] tab reorder fallback failed", err);
            }
        }, 800);
    },
});
