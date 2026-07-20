import { test } from "node:test";
import assert from "node:assert/strict";
import { buildTree, flattenVisible, searchFiles, keyInScope } from "../web/js/workflows_plus/workflowIndex.js";

function makeEntries() {
    return [
        { key: "root.json", filename: "root", nameLower: "root", size: 1, modified: 300, created: 30 },
        { key: "MCB/b.json", filename: "b", nameLower: "b", size: 1, modified: 100, created: 10 },
        { key: "MCB/a.json", filename: "a", nameLower: "a", size: 1, modified: 200, created: 20 },
        { key: "MCB/sub/c.json", filename: "c", nameLower: "c", size: 1, modified: 400, created: 40 },
        { key: "archive/d.json", filename: "d", nameLower: "d", size: 1, modified: 500, created: 50 },
    ];
}

test("buildTree nests folders and aggregates descendant file counts", () => {
    const tree = buildTree(makeEntries());

    assert.equal(tree.files.length, 1); // root.json
    assert.equal(tree.count, 5); // total across the whole tree

    const mcb = tree.folders.find((f) => f.name === "MCB");
    assert.ok(mcb);
    assert.equal(mcb.files.length, 2); // a.json, b.json
    assert.equal(mcb.count, 3); // a, b, and sub/c

    const sub = mcb.folders.find((f) => f.name === "sub");
    assert.ok(sub);
    assert.equal(sub.files.length, 1);
    assert.equal(sub.count, 1);
    assert.equal(sub.path, "MCB/sub");

    const archive = tree.folders.find((f) => f.name === "archive");
    assert.equal(archive.count, 1);
});

test("flattenVisible only descends into expanded folders", () => {
    const tree = buildTree(makeEntries());
    const rows = flattenVisible(tree, new Set(), { field: "name", dir: 1 });

    // Root file(s) always show; unexpanded folders show as a single row, no children.
    const folderRows = rows.filter((r) => r.kind === "folder");
    const fileRows = rows.filter((r) => r.kind === "file");

    assert.equal(folderRows.length, 2); // MCB, archive (top-level only)
    assert.equal(fileRows.length, 1); // root.json only
    assert.equal(fileRows[0].name, "root");
});

test("flattenVisible expands nested folders and sorts folders A-Z, files by sort spec", () => {
    const tree = buildTree(makeEntries());
    const expanded = new Set(["MCB", "MCB/sub", "archive"]);
    const rows = flattenVisible(tree, expanded, { field: "name", dir: 1 });

    // Top-level folders are alphabetical: MCB before archive is wrong alphabetically
    // (case-insensitive localeCompare) -- verify actual order via names.
    const topFolderNames = rows.filter((r) => r.depth === 0 && r.kind === "folder").map((r) => r.name);
    const sortedNames = [...topFolderNames].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
    assert.deepEqual(topFolderNames, sortedNames);

    // Files directly within MCB (depth 1, key under MCB/) sorted by name ascending: a, b
    const mcbFileNames = rows
        .filter((r) => r.kind === "file" && r.depth === 1 && r.key.startsWith("MCB/"))
        .map((r) => r.name);
    assert.deepEqual(mcbFileNames, ["a", "b"]);

    // sub/c.json shows up at depth 2 since MCB/sub is expanded
    const deepFile = rows.find((r) => r.kind === "file" && r.key === "MCB/sub/c.json");
    assert.ok(deepFile);
    assert.equal(deepFile.depth, 2);
});

test("flattenVisible sorts files by modified descending when requested", () => {
    const tree = buildTree(makeEntries());
    const rows = flattenVisible(tree, new Set(["MCB"]), { field: "modified", dir: -1 });
    const mcbFileNames = rows.filter((r) => r.kind === "file" && r.depth === 1).map((r) => r.name);
    // a has modified=200, b has modified=100 -> descending: a, b
    assert.deepEqual(mcbFileNames, ["a", "b"]);
});

test("flattenVisible sorts files by created ascending when requested", () => {
    const tree = buildTree(makeEntries());
    const rows = flattenVisible(tree, new Set(["MCB"]), { field: "created", dir: 1 });
    const mcbFileNames = rows.filter((r) => r.kind === "file" && r.depth === 1).map((r) => r.name);
    // b created=10, a created=20 -> ascending: b, a
    assert.deepEqual(mcbFileNames, ["b", "a"]);
});

test("searchFiles filters by predicate and applies sort spec", () => {
    const entries = makeEntries();
    const predicate = (nameLower) => nameLower === "a" || nameLower === "b";
    const results = searchFiles(entries, predicate, { field: "modified", dir: 1 });

    assert.equal(results.length, 2);
    // ascending by modified: b(100), a(200)
    assert.deepEqual(results.map((r) => r.filename), ["b", "a"]);
});

test("searchFiles with null predicate returns all entries, sorted", () => {
    const entries = makeEntries();
    const results = searchFiles(entries, null, { field: "name", dir: 1 });
    assert.equal(results.length, entries.length);
    const names = results.map((r) => r.filename);
    const sortedNames = [...names].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
    assert.deepEqual(names, sortedNames);
});

test("searchFiles with scopeFolder='' and includeSubfolders=false excludes nested entries (root-only)", () => {
    const entries = makeEntries();
    const results = searchFiles(entries, null, { field: "name", dir: 1 }, {
        scopeFolder: "",
        includeSubfolders: false,
    });
    assert.deepEqual(results.map((r) => r.key), ["root.json"]);
});

test("searchFiles scoped to a specific folder without subfolders returns only its direct files", () => {
    const entries = makeEntries();
    const results = searchFiles(entries, null, { field: "name", dir: 1 }, {
        scopeFolder: "MCB",
        includeSubfolders: false,
    });
    assert.deepEqual(results.map((r) => r.key).sort(), ["MCB/a.json", "MCB/b.json"]);
});

test("searchFiles scoped to a specific folder with subfolders includes nested descendants", () => {
    const entries = makeEntries();
    const results = searchFiles(entries, null, { field: "name", dir: 1 }, {
        scopeFolder: "MCB",
        includeSubfolders: true,
    });
    assert.deepEqual(
        results.map((r) => r.key).sort(),
        ["MCB/a.json", "MCB/b.json", "MCB/sub/c.json"]
    );
});

test("searchFiles defaults to scopeFolder='' + includeSubfolders=true (whole library)", () => {
    const entries = makeEntries();
    const results = searchFiles(entries, null, { field: "name", dir: 1 });
    assert.equal(results.length, entries.length);
});

test("keyInScope: root scope with subfolders matches everything", () => {
    assert.equal(keyInScope("root.json", "", true), true);
    assert.equal(keyInScope("MCB/sub/c.json", "", true), true);
});

test("keyInScope: root scope without subfolders matches only root-level keys", () => {
    assert.equal(keyInScope("root.json", "", false), true);
    assert.equal(keyInScope("MCB/a.json", "", false), false);
});

test("keyInScope: folder scope matches direct children and, if enabled, descendants", () => {
    assert.equal(keyInScope("MCB/a.json", "MCB", false), true);
    assert.equal(keyInScope("MCB/sub/c.json", "MCB", false), false);
    assert.equal(keyInScope("MCB/sub/c.json", "MCB", true), true);
    assert.equal(keyInScope("archive/d.json", "MCB", true), false);
});

test("keyInScope: a folder never matches a differently-named prefix (e.g. 'MCB' vs 'MCBX')", () => {
    assert.equal(keyInScope("MCBX/foo.json", "MCB", true), false);
});

test("searchFiles annotates pinned when isPinnedFn is provided, without reordering", () => {
    const entries = makeEntries();
    const isPinnedFn = (key) => key === "MCB/b.json";
    const results = searchFiles(entries, null, { field: "name", dir: 1 }, { isPinnedFn });

    // Still sorted purely by the requested sort spec (name asc) — pins don't float in flat mode.
    const names = results.map((r) => r.filename);
    const sortedNames = [...names].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
    assert.deepEqual(names, sortedNames);

    const bEntry = results.find((r) => r.key === "MCB/b.json");
    const aEntry = results.find((r) => r.key === "MCB/a.json");
    assert.equal(bEntry.pinned, true);
    assert.equal(aEntry.pinned, false);
});

test("flattenVisible groups pinned files above unpinned within each folder, each sorted independently", () => {
    const tree = buildTree(makeEntries());
    // Pin "b" (modified=100) in MCB even though sort-by-modified-desc would normally put "a" (200) first.
    const getPinnedKeys = (folderPath) => (folderPath === "MCB" ? new Set(["MCB/b.json"]) : new Set());
    const rows = flattenVisible(tree, new Set(["MCB"]), { field: "modified", dir: -1 }, getPinnedKeys);

    const mcbFiles = rows.filter((r) => r.kind === "file" && r.depth === 1 && r.key.startsWith("MCB/"));
    assert.deepEqual(mcbFiles.map((r) => r.name), ["b", "a"]); // pinned "b" floats above unpinned "a"
    assert.deepEqual(mcbFiles.map((r) => r.pinned), [true, false]);
});

test("flattenVisible with no getPinnedKeys treats all files as unpinned", () => {
    const tree = buildTree(makeEntries());
    const rows = flattenVisible(tree, new Set(["MCB"]), { field: "name", dir: 1 });
    const mcbFiles = rows.filter((r) => r.kind === "file" && r.depth === 1 && r.key.startsWith("MCB/"));
    assert.deepEqual(mcbFiles.map((r) => r.pinned), [false, false]);
});
