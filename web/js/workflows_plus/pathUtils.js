// Pure path helpers for Workflows+ file operations (rename / move / copy /
// delete). No ComfyUI/DOM/network imports — safe to unit test with
// `node --test`. Keys are always "/"-joined, relative to the workflows
// root, e.g. "MCB/foo.json".

const TRASH_FOLDER = "_trash";

export function folderOf(key) {
    const idx = key.lastIndexOf("/");
    return idx === -1 ? "" : key.slice(0, idx);
}

export function filenameOf(key) {
    // Base name without extension.
    const base = key.slice(key.lastIndexOf("/") + 1);
    return base.replace(/\.[^.]+$/, "");
}

export function extOf(key) {
    const base = key.slice(key.lastIndexOf("/") + 1);
    const m = base.match(/\.[^.]+$/);
    return m ? m[0] : "";
}

/**
 * Join a folder path ("" = root) and a filename (with extension) into a key.
 */
export function joinKey(folder, filenameWithExt) {
    return folder ? `${folder}/${filenameWithExt}` : filenameWithExt;
}

/**
 * Build the key for renaming a file in-place (same folder, new base name,
 * extension preserved).
 */
export function renameKey(key, newBaseName) {
    return joinKey(folderOf(key), `${newBaseName}${extOf(key)}`);
}

/**
 * Build the key for moving a file to a different folder (same filename).
 */
export function moveKey(key, destFolder) {
    const base = key.slice(key.lastIndexOf("/") + 1);
    return joinKey(destFolder, base);
}

/**
 * Given a set of existing keys (or a predicate) in the destination folder,
 * find the first available "<name> copy.json", "<name> copy 2.json", ...
 * for a duplicate operation. `existingKeysInFolder` is a Set of full keys
 * already present in `destFolder`.
 */
export function nextCopyKey(key, destFolder, existingKeysInFolder, maxAttempts = 200) {
    const base = filenameOf(key);
    const ext = extOf(key);

    let candidate = joinKey(destFolder, `${base} copy${ext}`);
    if (!existingKeysInFolder.has(candidate)) return candidate;

    for (let n = 2; n <= maxAttempts; n++) {
        candidate = joinKey(destFolder, `${base} copy ${n}${ext}`);
        if (!existingKeysInFolder.has(candidate)) return candidate;
    }
    return null; // give up — caller should surface an error
}

/**
 * Build a collision-proof destination key for soft-deleting a file into the
 * trash folder. Flattens the original path into the filename (with "__"
 * separators) and prefixes a timestamp, so it can never collide and the
 * original location stays visible from the name alone.
 */
export function trashKeyFor(key, now = Date.now()) {
    const folder = folderOf(key);
    const base = key.slice(key.lastIndexOf("/") + 1);
    const flatFolder = folder ? folder.replace(/\//g, "__") + "__" : "";
    const d = new Date(now);
    const pad = (n) => String(n).padStart(2, "0");
    const stamp = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
    return joinKey(TRASH_FOLDER, `${stamp}__${flatFolder}${base}`);
}

/**
 * True if `key` is already inside the trash folder (top-level or nested) —
 * used to decide whether "Delete" should soft-delete (move to trash) or
 * hard-delete (permanent removal, since it's already trashed).
 */
export function isInTrash(key) {
    return key === TRASH_FOLDER || key.startsWith(`${TRASH_FOLDER}/`);
}

/**
 * Build a collision-proof destination path for soft-deleting an entire
 * FOLDER into the trash: the whole directory is moved in one operation
 * (subtree preserved for recovery), named `<stamp>__<flattened path>`.
 */
export function trashKeyForFolder(folderPath, now = Date.now()) {
    const flat = folderPath.replace(/\//g, "__");
    const d = new Date(now);
    const pad = (n) => String(n).padStart(2, "0");
    const stamp = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
    return joinKey(TRASH_FOLDER, `${stamp}__${flat}`);
}

/**
 * True if `dest` is the same folder as `src` or nested anywhere under it —
 * used to reject moving a folder into itself or its own descendant.
 */
export function isDescendantOrSelf(dest, src) {
    return dest === src || dest.startsWith(`${src}/`);
}

/**
 * If `key` (a file key or folder path) is `oldPrefix` itself or lives under
 * `oldPrefix/`, return it rewritten onto `newPrefix`; otherwise return it
 * unchanged. Used to migrate pins/MRU/expansion/scope state after a folder
 * rename or move.
 */
export function rewritePrefix(key, oldPrefix, newPrefix) {
    if (key === oldPrefix) return newPrefix;
    if (key.startsWith(`${oldPrefix}/`)) return newPrefix + key.slice(oldPrefix.length);
    return key;
}

export { TRASH_FOLDER };
