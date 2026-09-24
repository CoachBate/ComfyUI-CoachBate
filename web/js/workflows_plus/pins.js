// Per-folder pinned-workflow list for the Workflows+ panel.
//
// Pins are Windows-style: pinning a workflow floats it to the top of its
// OWN folder (root files pin within the root "" folder), in a separate
// group from the rest — the active sort applies independently within each
// group. Persisted in localStorage as { [folderPath]: [key, ...] }.
//
// Pure module: no ComfyUI imports, safe to unit test with `node --test`
// (tests inject a fake storage object).

export const PINS_STORAGE_KEY = "coachbate.wfp.pins";

function getStorage(storage) {
    if (storage) return storage;
    if (typeof localStorage !== "undefined") return localStorage;
    return null;
}

function folderOf(key) {
    const idx = key.lastIndexOf("/");
    return idx === -1 ? "" : key.slice(0, idx);
}

/**
 * Load the full pin map from storage: { folderPath: [key, ...] }.
 * Returns {} if storage is unavailable or the stored value is invalid.
 */
export function loadPins(storage) {
    const store = getStorage(storage);
    if (!store) return {};

    let raw;
    try {
        raw = store.getItem(PINS_STORAGE_KEY);
    } catch (_) {
        return {};
    }
    if (!raw) return {};

    let parsed;
    try {
        parsed = JSON.parse(raw);
    } catch (_) {
        return {};
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};

    const out = {};
    for (const [folder, keys] of Object.entries(parsed)) {
        if (typeof folder === "string" && Array.isArray(keys)) {
            out[folder] = keys.filter((k) => typeof k === "string");
        }
    }
    return out;
}

function savePins(map, storage) {
    const store = getStorage(storage);
    if (!store) return;
    try {
        store.setItem(PINS_STORAGE_KEY, JSON.stringify(map));
    } catch (_) {
        // Storage full/unavailable — pins degrade to in-memory only for this call.
    }
}

/**
 * Return a Set of pinned keys for a given folder path ("" = root).
 */
export function pinnedKeysForFolder(folderPath, storage) {
    const map = loadPins(storage);
    return new Set(map[folderPath] || []);
}

export function isPinned(key, storage) {
    const map = loadPins(storage);
    const keys = map[folderOf(key)];
    return !!keys && keys.includes(key);
}

/**
 * Toggle a workflow's pinned state within its own folder. Returns the new
 * pinned boolean.
 */
export function togglePin(key, storage) {
    const map = loadPins(storage);
    const folder = folderOf(key);
    const keys = map[folder] || [];
    const idx = keys.indexOf(key);

    let nowPinned;
    if (idx === -1) {
        map[folder] = [...keys, key];
        nowPinned = true;
    } else {
        const next = keys.filter((k) => k !== key);
        if (next.length) map[folder] = next;
        else delete map[folder];
        nowPinned = false;
    }

    savePins(map, storage);
    return nowPinned;
}

/**
 * Remove all pins for a single folder path.
 */
export function resetFolderPins(folderPath, storage) {
    const map = loadPins(storage);
    delete map[folderPath];
    savePins(map, storage);
}

/**
 * Remove all pins everywhere.
 */
export function resetAllPins(storage) {
    savePins({}, storage);
}

/**
 * Migrate a pinned key after a rename/move/delete. If `oldKey` was pinned,
 * removes it from its old folder and — unless `newKey` is null (delete) —
 * re-adds it under `newKey`'s folder, preserving pinned state. No-op if
 * `oldKey` wasn't pinned. Returns true if a migration happened.
 */
export function migrateKey(oldKey, newKey, storage) {
    const map = loadPins(storage);
    const oldFolder = folderOf(oldKey);
    const keys = map[oldFolder];
    if (!keys || !keys.includes(oldKey)) return false;

    const remaining = keys.filter((k) => k !== oldKey);
    if (remaining.length) map[oldFolder] = remaining;
    else delete map[oldFolder];

    if (newKey) {
        const newFolder = folderOf(newKey);
        const newKeys = map[newFolder] || [];
        if (!newKeys.includes(newKey)) map[newFolder] = [...newKeys, newKey];
    }

    savePins(map, storage);
    return true;
}
