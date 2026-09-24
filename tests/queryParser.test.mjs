import { test } from "node:test";
import assert from "node:assert/strict";
import { tokenize, parse, buildPredicate } from "../web/js/workflows_plus/queryParser.js";

test("bare adjacency is a literal contiguous phrase, not implicit AND", () => {
    const pred = buildPredicate("black cat");
    assert.equal(pred("black cat"), true);
    assert.equal(pred("my black cat photo"), true);
    assert.equal(pred("black cute cat"), false); // words present but not contiguous
    assert.equal(pred("cat black"), false); // wrong order
});

test("quoted phrase behaves identically to the equivalent bare phrase", () => {
    const bare = buildPredicate("black cat");
    const quoted = buildPredicate('"black cat"');
    for (const s of ["black cat", "my black cat photo", "black cute cat", "cat black"]) {
        assert.equal(quoted(s), bare(s));
    }
});

test("uppercase AND requires both phrases independently, any order", () => {
    const pred = buildPredicate("black AND cat");
    assert.equal(pred("black cute cat"), true);
    assert.equal(pred("cat is black"), true);
    assert.equal(pred("black only"), false);
    assert.equal(pred("cat only"), false);
});

test("uppercase OR matches either phrase", () => {
    const pred = buildPredicate("qwen OR flux");
    assert.equal(pred("qwen_workflow"), true);
    assert.equal(pred("flux_workflow"), true);
    assert.equal(pred("sdxl_workflow"), false);
});

test("AND binds tighter than OR", () => {
    const pred = buildPredicate("alpha OR bravo AND charlie");
    assert.equal(pred("contains alpha only"), true);
    assert.equal(pred("contains bravo and charlie"), true);
    assert.equal(pred("contains bravo only"), false);
    assert.equal(pred("contains charlie only"), false);
});

test("lowercase 'and'/'or' are literal text, not operators", () => {
    const pred = buildPredicate("rock and roll");
    assert.equal(pred("rock and roll band"), true);
    assert.equal(pred("rock guitar"), false); // literal phrase "rock and roll" required
});

test("uppercase AND/OR are only operators when unquoted", () => {
    // A filename literally containing the word "AND" — force it literal with quotes.
    const pred = buildPredicate('"AND1 workflow"');
    assert.equal(pred("and1 workflow final"), true);
});

test("multi-word AND-joined phrases: each side is matched as its own contiguous phrase", () => {
    const pred = buildPredicate("hi res AND full body");
    assert.equal(pred("hi res render full body shot"), true);
    assert.equal(pred("hires render full body shot"), false); // "hi res" not contiguous
    assert.equal(pred("hi res render body full shot"), false); // "full body" not contiguous
});

test("empty or whitespace-only query returns null predicate (no filter)", () => {
    assert.equal(buildPredicate(""), null);
    assert.equal(buildPredicate("   "), null);
    assert.equal(buildPredicate(null), null);
    assert.equal(buildPredicate(undefined), null);
});

test("dangling operator is dropped leniently", () => {
    const pred1 = buildPredicate("qwen AND");
    assert.equal(pred1("qwen workflow"), true);
    assert.equal(pred1("other workflow"), false);

    const pred2 = buildPredicate("AND qwen");
    assert.equal(pred2("qwen workflow"), true);

    const pred3 = buildPredicate("qwen OR");
    assert.equal(pred3("qwen workflow"), true);
    assert.equal(pred3("other workflow"), false);

    const pred4 = buildPredicate("OR");
    assert.equal(pred4, null);
});

test("unbalanced quote consumes to end of string as part of the phrase", () => {
    const pred = buildPredicate('foo "bar baz');
    assert.equal(pred("foo bar baz qux"), true);
    assert.equal(pred("foo only"), false);
});

test("case-insensitive matching against lowercased haystack", () => {
    const pred = buildPredicate("QWEN");
    assert.equal(pred("qwen_workflow"), true);
});

test("tokenize merges consecutive non-operator words into one phrase token", () => {
    const tokens = tokenize('black cat AND "hi res" OR flux');
    assert.deepEqual(tokens, [
        { type: "phrase", value: "black cat" },
        { type: "op", value: "AND" },
        { type: "phrase", value: "hi res" },
        { type: "op", value: "OR" },
        { type: "phrase", value: "flux" },
    ]);
});

test("multiple consecutive OR operators are tolerated", () => {
    const pred = buildPredicate("alpha OR OR bravo");
    assert.equal(pred("has alpha"), true);
    assert.equal(pred("has bravo"), true);
    assert.equal(pred("has neither"), false);
});
