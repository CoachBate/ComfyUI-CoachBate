import { test } from "node:test";
import assert from "node:assert/strict";
import {
    escapeRegExp,
    buildPattern,
    findMatches,
    matchIndexAfter,
    matchIndexBefore,
    matchIndexAt,
    replaceAllIn,
} from "../web/js/text_tools/findReplace.js";

// ── escapeRegExp ───────────────────────────────────────────────────────────

test("escapeRegExp neutralises every metacharacter", () => {
    assert.equal(escapeRegExp("a.b*c"), "a\\.b\\*c");
    assert.equal(escapeRegExp("(masterpiece:1.2)"), "\\(masterpiece:1\\.2\\)");
    assert.equal(escapeRegExp("a|b"), "a\\|b");
});

// ── literal matching (no regex mode) ───────────────────────────────────────

test("the find box is literal, never a pattern", () => {
    // "." must not match an arbitrary character.
    assert.equal(findMatches("cat cot", ".").length, 0);
    assert.equal(findMatches("a.b", ".").length, 1);
});

test("prompt-style tokens full of metacharacters match literally", () => {
    const text = "<Subject 1> flirts with <Subject 2> and <Subject 1> laughs.";
    const m = findMatches(text, "<Subject 1>");
    assert.equal(m.length, 2);
    assert.equal(text.slice(m[0].start, m[0].end), "<Subject 1>");
});

test("buildPattern returns null for an empty query", () => {
    assert.equal(buildPattern(""), null);
    assert.equal(buildPattern(null), null);
    assert.deepEqual(findMatches("anything", ""), []);
});

// ── case sensitivity ───────────────────────────────────────────────────────

test("search is case-insensitive by default and exact when caseSensitive", () => {
    const text = "Cat cat CAT";
    assert.equal(findMatches(text, "cat").length, 3);
    assert.equal(findMatches(text, "cat", { caseSensitive: true }).length, 1);
    assert.equal(findMatches(text, "CAT", { caseSensitive: true }).length, 1);
});

// ── whole word ─────────────────────────────────────────────────────────────

test("wholeWord excludes substrings inside larger words", () => {
    const text = "cat category concat cat.";
    assert.equal(findMatches(text, "cat").length, 4);
    const ww = findMatches(text, "cat", { wholeWord: true });
    assert.equal(ww.length, 2);                       // "cat " and "cat."
    assert.equal(text.slice(ww[0].start, ww[0].end), "cat");
    assert.equal(ww[0].start, 0);
    assert.equal(ww[1].start, text.lastIndexOf("cat"));
});

test("wholeWord only constrains the side whose edge char is a word char", () => {
    // A blanket \b on both ends would make this unmatchable: \b before "<"
    // demands a word character immediately to its left.
    const text = "a <Subject 1> b";
    assert.equal(findMatches(text, "<Subject 1>", { wholeWord: true }).length, 1);
});

test("wholeWord combines with caseSensitive", () => {
    const text = "Cat category";
    assert.equal(findMatches(text, "cat", { wholeWord: true }).length, 1);
    assert.equal(
        findMatches(text, "cat", { wholeWord: true, caseSensitive: true }).length,
        0,
    );
});

// ── match geometry ─────────────────────────────────────────────────────────

test("findMatches reports non-overlapping [start,end) spans in order", () => {
    const m = findMatches("aaaa", "aa");
    assert.deepEqual(m, [{ start: 0, end: 2 }, { start: 2, end: 4 }]);
});

test("findMatches handles an empty haystack", () => {
    assert.deepEqual(findMatches("", "cat"), []);
});

// ── navigation with wrap-around ────────────────────────────────────────────

const NAV = findMatches("cat dog cat dog cat", "cat");   // starts 0, 8, 16

test("matchIndexAfter finds the next match and wraps past the last", () => {
    assert.equal(matchIndexAfter(NAV, 0), 0);
    assert.equal(matchIndexAfter(NAV, 1), 1);
    assert.equal(matchIndexAfter(NAV, 9), 2);
    assert.equal(matchIndexAfter(NAV, 17), 0);   // wraps
});

test("matchIndexBefore finds the previous match and wraps past the first", () => {
    assert.equal(matchIndexBefore(NAV, 19), 2);
    assert.equal(matchIndexBefore(NAV, 16), 1);
    assert.equal(matchIndexBefore(NAV, 8), 0);
    assert.equal(matchIndexBefore(NAV, 0), 2);   // wraps
});

test("navigation helpers return -1 when there are no matches", () => {
    assert.equal(matchIndexAfter([], 0), -1);
    assert.equal(matchIndexBefore([], 0), -1);
});

test("matchIndexAt identifies the currently selected match", () => {
    assert.equal(matchIndexAt(NAV, 8, 11), 1);
    assert.equal(matchIndexAt(NAV, 8, 10), -1);   // partial selection
    assert.equal(matchIndexAt(NAV, 4, 7), -1);    // a different word
});

// ── replace all ────────────────────────────────────────────────────────────

test("replaceAllIn replaces every match and reports the count", () => {
    const r = replaceAllIn("cat dog cat", "cat", "fox");
    assert.equal(r.text, "fox dog fox");
    assert.equal(r.count, 2);
});

test("replaceAllIn honours caseSensitive and wholeWord", () => {
    assert.deepEqual(
        replaceAllIn("Cat cat", "cat", "fox", { caseSensitive: true }),
        { text: "Cat fox", count: 1 },
    );
    assert.deepEqual(
        replaceAllIn("cat category", "cat", "fox", { wholeWord: true }),
        { text: "fox category", count: 1 },
    );
});

test("an empty replacement deletes every occurrence", () => {
    assert.deepEqual(
        replaceAllIn("a, b, c", ", ", ""),
        { text: "abc", count: 2 },
    );
});

test("$-sequences in the replacement stay literal", () => {
    // The callback form of String.replace means "$&" is not a backreference.
    assert.deepEqual(
        replaceAllIn("cost", "cost", "$&100"),
        { text: "$&100", count: 1 },
    );
    assert.deepEqual(
        replaceAllIn("price", "price", "$1"),
        { text: "$1", count: 1 },
    );
});

test("replaceAllIn is a no-op for an empty query", () => {
    assert.deepEqual(replaceAllIn("cat", "", "fox"), { text: "cat", count: 0 });
});

test("replacement text containing the query does not re-match", () => {
    // One pass only: "cat" → "cat cat" must not loop.
    assert.deepEqual(
        replaceAllIn("cat", "cat", "cat cat"),
        { text: "cat cat", count: 1 },
    );
});
