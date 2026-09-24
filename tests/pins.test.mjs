import { test } from "node:test";
import assert from "node:assert/strict";
import {
    loadPins,
    isPinned,
    togglePin,
    pinnedKeysForFolder,
    resetFolderPins,
    resetAllPins,
    migrateKey,
} from "../web/js/workflows_plus/pins.js";

function fakeStorage() {
    const map = new Map();
    return {
        getItem: (k) => (map.has(k) ? map.get(k) : null),
        setItem: (k, v) => map.set(k, v),
        removeItem: (k) => map.delete(k),
    };
}

test("loadPins returns empty object when storage is empty", () => {
    const storage = fakeStorage();
    assert.deepEqual(loadPins(storage), {});
});

test("togglePin pins a root-level file under the '' folder", () => {
    const storage = fakeStorage();
    const result = togglePin("root.json", storage);
    assert.equal(result, true);
    assert.equal(isPinned("root.json", storage), true);

    const map = loadPins(storage);
    assert.deepEqual(map[""], ["root.json"]);
});

test("togglePin pins a nested file under its own folder", () => {
    const storage = fakeStorage();
    togglePin("MCB/foo.json", storage);
    togglePin("MCB/sub/bar.json", storage);

    const map = loadPins(storage);
    assert.deepEqual(map["MCB"], ["MCB/foo.json"]);
    assert.deepEqual(map["MCB/sub"], ["MCB/sub/bar.json"]);
});

test("togglePin unpins an already-pinned file", () => {
    const storage = fakeStorage();
    togglePin("MCB/foo.json", storage);
    const result = togglePin("MCB/foo.json", storage);
    assert.equal(result, false);
    assert.equal(isPinned("MCB/foo.json", storage), false);
    assert.deepEqual(loadPins(storage), {});
});

test("pinnedKeysForFolder returns a Set scoped to that folder only", () => {
    const storage = fakeStorage();
    togglePin("MCB/a.json", storage);
    togglePin("MCB/b.json", storage);
    togglePin("archive/c.json", storage);

    const mcbSet = pinnedKeysForFolder("MCB", storage);
    assert.equal(mcbSet.size, 2);
    assert.ok(mcbSet.has("MCB/a.json"));
    assert.ok(mcbSet.has("MCB/b.json"));
    assert.ok(!mcbSet.has("archive/c.json"));

    const rootSet = pinnedKeysForFolder("", storage);
    assert.equal(rootSet.size, 0);
});

test("resetFolderPins clears only the given folder", () => {
    const storage = fakeStorage();
    togglePin("MCB/a.json", storage);
    togglePin("archive/c.json", storage);

    resetFolderPins("MCB", storage);

    assert.equal(pinnedKeysForFolder("MCB", storage).size, 0);
    assert.equal(pinnedKeysForFolder("archive", storage).size, 1);
});

test("resetAllPins clears every folder", () => {
    const storage = fakeStorage();
    togglePin("MCB/a.json", storage);
    togglePin("archive/c.json", storage);
    togglePin("root.json", storage);

    resetAllPins(storage);

    assert.deepEqual(loadPins(storage), {});
});

test("migrateKey moves a pin from its old folder to the new folder on rename/move", () => {
    const storage = fakeStorage();
    togglePin("MCB/foo.json", storage);

    const moved = migrateKey("MCB/foo.json", "archive/foo.json", storage);
    assert.equal(moved, true);
    assert.equal(isPinned("MCB/foo.json", storage), false);
    assert.equal(isPinned("archive/foo.json", storage), true);
    assert.equal(pinnedKeysForFolder("MCB", storage).size, 0);
    assert.equal(pinnedKeysForFolder("archive", storage).size, 1);
});

test("migrateKey with newKey=null drops the pin (delete)", () => {
    const storage = fakeStorage();
    togglePin("MCB/foo.json", storage);

    const moved = migrateKey("MCB/foo.json", null, storage);
    assert.equal(moved, true);
    assert.equal(isPinned("MCB/foo.json", storage), false);
    assert.deepEqual(loadPins(storage), {});
});

test("migrateKey is a no-op when oldKey wasn't pinned", () => {
    const storage = fakeStorage();
    togglePin("MCB/other.json", storage);

    const moved = migrateKey("MCB/foo.json", "archive/foo.json", storage);
    assert.equal(moved, false);
    assert.equal(isPinned("MCB/other.json", storage), true);
    assert.equal(isPinned("archive/foo.json", storage), false);
});

test("loadPins ignores malformed JSON and non-object shapes", () => {
    const storage = fakeStorage();
    storage.setItem("coachbate.wfp.pins", "not json");
    assert.deepEqual(loadPins(storage), {});

    storage.setItem("coachbate.wfp.pins", JSON.stringify(["a", "b"]));
    assert.deepEqual(loadPins(storage), {});

    storage.setItem("coachbate.wfp.pins", JSON.stringify({ MCB: "not-an-array", ok: ["x.json"] }));
    assert.deepEqual(loadPins(storage), { ok: ["x.json"] });
});
