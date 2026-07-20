// Most-recently-used workflow list for the Workflows+ panel.
//
// Persisted in localStorage as an array of { key, ts }, newest first,
// capped at MAX_ENTRIES. Pure module: no ComfyUI imports, safe to unit
// test with `node --test` (tests inject a fake storage object).

export const MRU_STORAGE_KEY = "coachbate.wfp.mru";
export const MAX_ENTRIES = 10;

function getStorage(storage) {
    if (storage) return storage;
    if (typeof localStorage !== "undefined") return localStorage;
    return null;
}

/**
 * Load the MRU list from storage, newest first.
 * Returns [] if storage is unavailable or the stored value is invalid.
 */
export function loadMru(storage) {
    const store = getStorage(storage);
    if (!store) return [];

    let raw;
    try {
        raw = store.getItem(MRU_STORAGE_KEY);
    } catch (_) {
        return [];
    }
    if (!raw) return [];

    let parsed;
    try {
        parsed = JSON.parse(raw);
    } catch (_) {
        return [];
    }
    if (!Array.isArray(parsed)) return [];

    return parsed
        .filter((e) => e && typeof e.key === "string" && typeof e.ts === "number")
        .slice(0, MAX_ENTRIES);
}

function saveMru(list, storage) {
    const store = getStorage(storage);
    if (!store) return;
    try {
        store.setItem(MRU_STORAGE_KEY, JSON.stringify(list));
    } catch (_) {
        // Storage full/unavailable — MRU degrades to in-memory only for this call.
    }
}

/**
 * Record that `key` was opened just now. Moves it to the front if already
 * present (dedupe), trims to MAX_ENTRIES, and persists.
 * Returns the updated list.
 */
export function pushMru(key, storage, now = Date.now()) {
    if (!key) return loadMru(storage);

    const existing = loadMru(storage).filter((e) => e.key !== key);
    const updated = [{ key, ts: now }, ...existing].slice(0, MAX_ENTRIES);

    saveMru(updated, storage);
    return updated;
}

/**
 * Migrate an MRU entry after a rename/move (preserves its timestamp/
 * position) or drop it after a delete (`newKey` = null). No-op if `oldKey`
 * wasn't in the list. Returns the updated list.
 */
export function migrateKey(oldKey, newKey, storage) {
    const list = loadMru(storage);
    const idx = list.findIndex((e) => e.key === oldKey);
    if (idx === -1) return list;

    const updated = newKey
        ? list.map((e) => (e.key === oldKey ? { ...e, key: newKey } : e))
        : list.filter((e) => e.key !== oldKey);

    saveMru(updated, storage);
    return updated;
}
