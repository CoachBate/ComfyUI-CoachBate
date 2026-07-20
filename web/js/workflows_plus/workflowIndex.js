// Pure indexing/tree/search/sort helpers for the Workflows+ panel.
//
// Entries are plain objects: { key, filename, nameLower, size, modified, created }
//   - key:      path relative to the workflows root, e.g. "MCB/foo.json"
//   - filename: file name without extension, e.g. "foo"
//   - nameLower: filename.toLowerCase(), precomputed for fast search
//   - modified/created: epoch milliseconds
//
// Pure module: no ComfyUI/DOM imports, safe to unit test with `node --test`.

/**
 * Build a nested folder tree from a flat entry list.
 * Returns a root node: { name: "", path: "", folders: [...], files: [...], count }
 * Folders and files are populated but NOT sorted here (sorting is applied
 * by flattenVisible/searchFiles so a single sort spec can be swapped without
 * rebuilding the tree).
 */
export function buildTree(entries) {
    const root = { name: "", path: "", folders: [], files: [], count: 0, _folderMap: new Map() };

    for (const entry of entries) {
        const parts = entry.key.split("/");
        const fileName = parts.pop();
        let node = root;
        let pathSoFar = "";

        for (const part of parts) {
            pathSoFar = pathSoFar ? `${pathSoFar}/${part}` : part;
            let child = node._folderMap.get(part);
            if (!child) {
                child = { name: part, path: pathSoFar, folders: [], files: [], count: 0, _folderMap: new Map() };
                node._folderMap.set(part, child);
                node.folders.push(child);
            }
            node = child;
        }

        node.files.push(entry);
        void fileName; // entry already carries filename; fileName only used for traversal
    }

    // Compute descendant file counts bottom-up, then drop the temporary map.
    function finalize(node) {
        let count = node.files.length;
        for (const folder of node.folders) {
            count += finalize(folder);
        }
        node.count = count;
        delete node._folderMap;
        return count;
    }
    finalize(root);

    return root;
}

function compareBy(field, dir) {
    if (field === "modified" || field === "created") {
        return (a, b) => (a[field] - b[field]) * dir;
    }
    // name
    return (a, b) => a.filename.localeCompare(b.filename, undefined, { sensitivity: "base" }) * dir;
}

function sortedFolders(folders) {
    return [...folders].sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));
}

function sortedFiles(files, sortSpec) {
    const { field = "modified", dir = -1 } = sortSpec || {};
    return [...files].sort(compareBy(field, dir));
}

/**
 * Split files into a pinned group and the rest, each independently sorted
 * by sortSpec, pinned group first (Windows-style "pinned items float to
 * the top of their own folder, sorted like everything else within the
 * pinned group").
 */
function sortedFilesGrouped(files, sortSpec, pinnedSet) {
    if (!pinnedSet || pinnedSet.size === 0) {
        return sortedFiles(files, sortSpec).map((f) => ({ file: f, pinned: false }));
    }
    const pinned = [];
    const rest = [];
    for (const f of files) {
        (pinnedSet.has(f.key) ? pinned : rest).push(f);
    }
    return [
        ...sortedFiles(pinned, sortSpec).map((f) => ({ file: f, pinned: true })),
        ...sortedFiles(rest, sortSpec).map((f) => ({ file: f, pinned: false })),
    ];
}

/**
 * Flatten the visible rows of a tree in display order: for each expanded
 * folder (folders always sorted A-Z), emit a folder row followed by its
 * pinned files (sorted by sortSpec), then its unpinned files (sorted by
 * sortSpec), then recursively its expanded subfolders.
 *
 * @param {object} tree - result of buildTree()
 * @param {Set<string>} expandedSet - set of folder `path` values that are expanded
 * @param {{field: 'name'|'modified'|'created', dir: 1|-1}} sortSpec
 * @param {(folderPath: string) => Set<string>} [getPinnedKeys] - returns the
 *   set of pinned file keys for a given folder path ("" = root). Omit for
 *   no pinning (all files render as one unpinned group).
 * @returns {Array<{kind:'folder'|'file', depth:number, name:string, path?:string, key?:string, count?:number, modified?:number, created?:number, pinned?:boolean}>}
 */
export function flattenVisible(tree, expandedSet, sortSpec, getPinnedKeys) {
    const rows = [];

    function walk(node, depth) {
        const folders = sortedFolders(node.folders);
        const pinnedSet = getPinnedKeys ? getPinnedKeys(node.path) : null;
        const files = sortedFilesGrouped(node.files, sortSpec, pinnedSet);

        for (const folder of folders) {
            rows.push({ kind: "folder", depth, name: folder.name, path: folder.path, count: folder.count });
            if (expandedSet.has(folder.path)) {
                walk(folder, depth + 1);
            }
        }

        for (const { file, pinned } of files) {
            rows.push({
                kind: "file",
                depth,
                name: file.filename,
                key: file.key,
                modified: file.modified,
                created: file.created,
                pinned,
            });
        }
    }

    walk(tree, 0);
    return rows;
}

function folderOf(key) {
    const idx = key.lastIndexOf("/");
    return idx === -1 ? "" : key.slice(0, idx);
}

/**
 * True if `key` falls within `scopeFolder` per the given scope rules.
 *   scopeFolder = ""  -> scope is the workflows root (the whole tree, when
 *                        combined with includeSubfolders=true).
 *   scopeFolder = "MCB" -> scope is that folder specifically.
 *   includeSubfolders=false -> only direct children of scopeFolder match
 *     (mirrors the "search subfolders" toggle set to OFF).
 *   includeSubfolders=true  -> direct children AND everything nested under
 *     scopeFolder match (scopeFolder="" + true means literally every file).
 */
export function keyInScope(key, scopeFolder = "", includeSubfolders = true) {
    const folder = folderOf(key);
    if (folder === scopeFolder) return true;
    if (!includeSubfolders) return false;
    if (scopeFolder === "") return true;
    return folder.startsWith(`${scopeFolder}/`);
}

/**
 * Return a flat, sorted list of entries matching `predicate` against each
 * entry's search text. If predicate is null/undefined, all entries match.
 *
 * @param {Array} entries
 * @param {(text: string) => boolean | null} predicate
 * @param {{field:string, dir:number}} sortSpec
 * @param {object} [opts]
 * @param {string} [opts.scopeFolder=""] - restrict the search to this folder
 *   (and, if includeSubfolders, everything nested under it). "" = the
 *   workflows root.
 * @param {boolean} [opts.includeSubfolders=true] - the "search subfolders"
 *   toggle; false restricts to files directly inside scopeFolder only.
 * @param {(key: string) => boolean} [opts.isPinnedFn] - when provided, each
 *   result row is annotated with `pinned` (icon only; flat search results
 *   are NOT grouped/reordered by pin state).
 * @param {(entry: object) => string} [opts.textFor] - text to match the
 *   predicate against (defaults to entry.nameLower, i.e. filename search).
 */
export function searchFiles(entries, predicate, sortSpec, opts = {}) {
    const {
        scopeFolder = "",
        includeSubfolders = true,
        isPinnedFn = null,
        textFor = (e) => e.nameLower,
    } = opts;

    let pool = entries.filter((e) => keyInScope(e.key, scopeFolder, includeSubfolders));
    const matched = predicate ? pool.filter((e) => predicate(textFor(e))) : pool;
    const sorted = sortedFiles(matched, sortSpec);

    if (!isPinnedFn) return sorted;
    return sorted.map((f) => ({ ...f, pinned: isPinnedFn(f.key) }));
}
