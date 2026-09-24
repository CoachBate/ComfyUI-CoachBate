import { test } from "node:test";
import assert from "node:assert/strict";
import { loadMru, pushMru, migrateKey, MAX_ENTRIES } from "../web/js/workflows_plus/mru.js";

// Minimal in-memory localStorage-alike for headless testing.
function fakeStorage() {
    const map = new Map();
    return {
        getItem: (k) => (map.has(k) ? map.get(k) : null),
        setItem: (k, v) => map.set(k, v),
        removeItem: (k) => map.delete(k),
    };
}

test("loadMru returns empty array when storage is empty", () => {
    const storage = fakeStorage();
    assert.deepEqual(loadMru(storage), []);
});

test("pushMru adds an entry to the front", () => {
    const storage = fakeStorage();
    pushMru("MCB/a.json", storage, 1000);
    const list = loadMru(storage);
    assert.equal(list.length, 1);
    assert.equal(list[0].key, "MCB/a.json");
    assert.equal(list[0].ts, 1000);
});

test("pushMru moves an existing entry to the front instead of duplicating", () => {
    const storage = fakeStorage();
    pushMru("a.json", storage, 1000);
    pushMru("b.json", storage, 2000);
    pushMru("a.json", storage, 3000);

    const list = loadMru(storage);
    assert.equal(list.length, 2);
    assert.equal(list[0].key, "a.json");
    assert.equal(list[0].ts, 3000);
    assert.equal(list[1].key, "b.json");
});

test("pushMru caps the list at MAX_ENTRIES", () => {
    const storage = fakeStorage();
    for (let i = 0; i < MAX_ENTRIES + 5; i++) {
        pushMru(`wf-${i}.json`, storage, i);
    }
    const list = loadMru(storage);
    assert.equal(list.length, MAX_ENTRIES);
    // Newest entries survive, oldest were evicted.
    assert.equal(list[0].key, `wf-${MAX_ENTRIES + 4}.json`);
    assert.equal(list[list.length - 1].key, `wf-5.json`);
});

test("pushMru with no key is a no-op that returns the current list", () => {
    const storage = fakeStorage();
    pushMru("a.json", storage, 1000);
    const before = loadMru(storage);
    const after = pushMru(null, storage, 2000);
    assert.deepEqual(after, before);
});

test("migrateKey renames an MRU entry in place, preserving its timestamp/position", () => {
    const storage = fakeStorage();
    pushMru("MCB/foo.json", storage, 1000);
    pushMru("archive/bar.json", storage, 2000);

    const updated = migrateKey("MCB/foo.json", "MCB/renamed.json", storage);
    assert.equal(updated[1].key, "MCB/renamed.json");
    assert.equal(updated[1].ts, 1000);
    assert.equal(updated[0].key, "archive/bar.json");
});

test("migrateKey with newKey=null removes the entry (delete)", () => {
    const storage = fakeStorage();
    pushMru("MCB/foo.json", storage, 1000);
    pushMru("archive/bar.json", storage, 2000);

    const updated = migrateKey("MCB/foo.json", null, storage);
    assert.equal(updated.length, 1);
    assert.equal(updated[0].key, "archive/bar.json");
});

test("migrateKey is a no-op when oldKey isn't in the MRU list", () => {
    const storage = fakeStorage();
    pushMru("archive/bar.json", storage, 1000);
    const before = loadMru(storage);
    const after = migrateKey("MCB/foo.json", "MCB/renamed.json", storage);
    assert.deepEqual(after, before);
});

test("loadMru ignores malformed JSON", () => {
    const storage = fakeStorage();
    storage.setItem("coachbate.wfp.mru", "not json");
    assert.deepEqual(loadMru(storage), []);
});

test("loadMru filters out malformed entries", () => {
    const storage = fakeStorage();
    storage.setItem(
        "coachbate.wfp.mru",
        JSON.stringify([{ key: "ok.json", ts: 1 }, { key: 5, ts: 2 }, { ts: 3 }, null])
    );
    const list = loadMru(storage);
    assert.equal(list.length, 1);
    assert.equal(list[0].key, "ok.json");
});
