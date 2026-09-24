// Pure find/replace helpers for the CoachBate in-node text toolbar.
//
// No DOM, no ComfyUI imports — everything here is a plain string function so it
// can be unit-tested under `node --test` (tests/findReplace.test.mjs), the same
// arrangement the workflows_plus/ helpers use.
//
// There is deliberately NO regex mode: the find box is always a literal string.
// Prompts are full of regex metacharacters ("<Subject 1>", "(masterpiece:1.2)",
// "a|b"), so treating the box as a pattern would surprise far more often than it
// would help. Everything below escapes the query before it ever reaches RegExp.

// Guard against a pathological query on a huge buffer (e.g. a single space in a
// 200 KB prompt). Well past any real use; keeps the counter from freezing the UI.
export const MAX_MATCHES = 20000;

/** Escape every RegExp metacharacter so `query` matches literally. */
export function escapeRegExp(s) {
    return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Build the global RegExp used for a search, or null if `query` is empty.
 *
 * Whole-word only constrains the side whose own edge character is a word
 * character — the rule VS Code uses. A blanket \b on both ends would make
 * "<Subject 1>" unmatchable, since \b before "<" demands a word character
 * immediately to its left.
 */
export function buildPattern(query, { caseSensitive = false, wholeWord = false } = {}) {
    if (!query) return null;
    let src = escapeRegExp(query);
    if (wholeWord) {
        if (/^\w/.test(query)) src = "(?<!\\w)" + src;
        if (/\w$/.test(query)) src = src + "(?!\\w)";
    }
    try {
        return new RegExp(src, caseSensitive ? "g" : "gi");
    } catch {
        return null;   // lookbehind unsupported on some ancient engine
    }
}

/**
 * All non-overlapping matches of `query` in `text`, in document order.
 * @returns {{start:number,end:number}[]}
 */
export function findMatches(text, query, opts = {}) {
    const re = buildPattern(query, opts);
    if (!re || !text) return [];
    const out = [];
    let m;
    while ((m = re.exec(text)) !== null) {
        out.push({ start: m.index, end: m.index + m[0].length });
        // An escaped non-empty query can't match empty, but a zero-width match
        // would spin re.exec forever — cheap insurance.
        if (m[0].length === 0) re.lastIndex++;
        if (out.length >= MAX_MATCHES) break;
    }
    return out;
}

/** Index of the first match starting at or after `pos`, wrapping to 0. -1 if none. */
export function matchIndexAfter(matches, pos) {
    for (let i = 0; i < matches.length; i++) {
        if (matches[i].start >= pos) return i;
    }
    return matches.length ? 0 : -1;
}

/** Index of the last match ending at or before `pos`, wrapping to the end. -1 if none. */
export function matchIndexBefore(matches, pos) {
    for (let i = matches.length - 1; i >= 0; i--) {
        if (matches[i].end <= pos) return i;
    }
    return matches.length ? matches.length - 1 : -1;
}

/**
 * Index of the match exactly covering [start, end) — i.e. the one currently
 * selected in the textarea — or -1. Lets "Replace" act on what the user can see
 * highlighted instead of re-searching from the caret.
 */
export function matchIndexAt(matches, start, end) {
    for (let i = 0; i < matches.length; i++) {
        if (matches[i].start === start && matches[i].end === end) return i;
    }
    return -1;
}

/**
 * Replace every match in one pass.
 *
 * `replacement` is inserted literally: the callback form of String.replace means
 * "$1", "$&" and friends in the user's replace box stay as typed rather than
 * being interpreted as backreferences.
 *
 * @returns {{text:string, count:number}}
 */
export function replaceAllIn(text, query, replacement, opts = {}) {
    const re = buildPattern(query, opts);
    if (!re) return { text, count: 0 };
    let count = 0;
    const out = String(text).replace(re, () => {
        count++;
        return replacement;
    });
    return { text: out, count };
}
