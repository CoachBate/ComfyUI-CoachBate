import { test } from "node:test";
import assert from "node:assert/strict";
import {
    folderOf,
    filenameOf,
    extOf,
    joinKey,
    renameKey,
    moveKey,
    nextCopyKey,
    trashKeyFor,
    trashKeyForFolder,
    isDescendantOrSelf,
    rewritePrefix,
    isInTrash,
    TRASH_FOLDER,
} from "../web/js/workflows_plus/pathUtils.js";

test("folderOf / filenameOf / extOf split a key correctly", () => {
    assert.equal(folderOf("MCB/sub/foo.json"), "MCB/sub");
    assert.equal(folderOf("root.json"), "");
    assert.equal(filenameOf("MCB/foo.json"), "foo");
    assert.equal(filenameOf("root.json"), "root");
    assert.equal(extOf("MCB/foo.json"), ".json");
    assert.equal(extOf("MCB/foo"), "");
});

test("joinKey handles root vs nested folders", () => {
    assert.equal(joinKey("", "foo.json"), "foo.json");
    assert.equal(joinKey("MCB", "foo.json"), "MCB/foo.json");
    assert.equal(joinKey("MCB/sub", "foo.json"), "MCB/sub/foo.json");
});

test("renameKey keeps folder and extension, changes base name", () => {
    assert.equal(renameKey("MCB/foo.json", "bar"), "MCB/bar.json");
    assert.equal(renameKey("root.json", "renamed"), "renamed.json");
});

test("moveKey keeps filename, changes folder", () => {
    assert.equal(moveKey("MCB/foo.json", "archive"), "archive/foo.json");
    assert.equal(moveKey("MCB/sub/foo.json", ""), "foo.json");
    assert.equal(moveKey("root.json", "NewFolder"), "NewFolder/root.json");
});

test("nextCopyKey picks 'name copy.json' when free", () => {
    const existing = new Set(["MCB/foo.json"]);
    assert.equal(nextCopyKey("MCB/foo.json", "MCB", existing), "MCB/foo copy.json");
});

test("nextCopyKey increments when 'copy' is taken", () => {
    const existing = new Set(["MCB/foo.json", "MCB/foo copy.json", "MCB/foo copy 2.json"]);
    assert.equal(nextCopyKey("MCB/foo.json", "MCB", existing), "MCB/foo copy 3.json");
});

test("nextCopyKey targets a different destination folder independently", () => {
    const existing = new Set(["archive/foo copy.json"]);
    assert.equal(nextCopyKey("MCB/foo.json", "archive", existing), "archive/foo copy 2.json");
});

test("nextCopyKey gives up after maxAttempts and returns null", () => {
    const existing = new Set(["MCB/foo copy.json"]);
    for (let n = 2; n <= 5; n++) existing.add(`MCB/foo copy ${n}.json`);
    assert.equal(nextCopyKey("MCB/foo.json", "MCB", existing, 5), null);
});

test("trashKeyFor is collision-proof and flattens the original folder into the name", () => {
    const now = new Date(2026, 5, 15, 9, 30, 5).getTime(); // 2026-06-15 09:30:05
    const key = trashKeyFor("MCB/sub/foo.json", now);
    assert.equal(key, `${TRASH_FOLDER}/20260615-093005__MCB__sub__foo.json`);
});

test("trashKeyFor handles a root-level file (no folder prefix)", () => {
    const now = new Date(2026, 0, 1, 0, 0, 0).getTime();
    const key = trashKeyFor("root.json", now);
    assert.equal(key, `${TRASH_FOLDER}/20260101-000000__root.json`);
});

test("isInTrash detects the trash folder at any depth", () => {
    assert.equal(isInTrash("_trash/foo.json"), true);
    assert.equal(isInTrash("_trash"), true);
    assert.equal(isInTrash("MCB/foo.json"), false);
    assert.equal(isInTrash("_trashy/foo.json"), false); // must be an exact segment match
});

test("trashKeyForFolder flattens the whole folder path and prefixes a timestamp", () => {
    const now = new Date(2026, 5, 15, 9, 30, 5).getTime();
    assert.equal(trashKeyForFolder("MCB/sub", now), `${TRASH_FOLDER}/20260615-093005__MCB__sub`);
    assert.equal(trashKeyForFolder("MCB", now), `${TRASH_FOLDER}/20260615-093005__MCB`);
});

test("isDescendantOrSelf rejects moving a folder into itself or its own descendant", () => {
    assert.equal(isDescendantOrSelf("MCB", "MCB"), true); // same folder
    assert.equal(isDescendantOrSelf("MCB/sub", "MCB"), true); // dest is a child of src
    assert.equal(isDescendantOrSelf("MCB/sub/deep", "MCB"), true); // dest is a deeper descendant
    assert.equal(isDescendantOrSelf("archive", "MCB"), false); // unrelated
    assert.equal(isDescendantOrSelf("MCBX", "MCB"), false); // prefix collision, not a real descendant
});

test("rewritePrefix rewrites an exact match or a nested path onto the new prefix", () => {
    assert.equal(rewritePrefix("MCB", "MCB", "renamed"), "renamed");
    assert.equal(rewritePrefix("MCB/sub", "MCB", "renamed"), "renamed/sub");
    assert.equal(rewritePrefix("MCB/sub/foo.json", "MCB", "archive/MCB"), "archive/MCB/sub/foo.json");
});

test("rewritePrefix leaves unrelated keys untouched", () => {
    assert.equal(rewritePrefix("archive/foo.json", "MCB", "renamed"), "archive/foo.json");
    assert.equal(rewritePrefix("MCBX/foo.json", "MCB", "renamed"), "MCBX/foo.json"); // prefix collision guard
});
