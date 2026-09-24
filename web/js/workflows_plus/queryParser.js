// Boolean search query parser for the Workflows+ panel.
//
// Grammar:
//   orExpr  := andExpr ( OR andExpr )*
//   andExpr := phrase ( AND phrase )*
//   phrase  := one or more consecutive non-operator words (quoted or not),
//              joined with single spaces — matched as a literal, contiguous
//              substring. Bare adjacency is NOT an implicit AND: "black cat"
//              only matches filenames containing the literal substring
//              "black cat", not "black cute cat".
//
// - AND / OR are recognized ONLY as exact uppercase, unquoted, whitespace-
//   bounded words. Lowercase "and"/"or" (or anything inside quotes, even
//   uppercase AND/OR) are treated as literal text.
// - AND binds tighter than OR.
// - Malformed input (dangling operators, unbalanced quotes) is handled
//   leniently rather than throwing.
//
// Pure module: no ComfyUI imports, safe to unit test with `node --test`.

/**
 * Split the raw query into words, treating quoted "..." sections as a
 * single unit (their content is never treated as an operator). Returns an
 * array of { text: string, quoted: boolean }.
 */
function splitWords(input) {
    const words = [];
    let i = 0;
    const n = input.length;

    while (i < n) {
        const ch = input[i];

        if (ch === " " || ch === "\t" || ch === "\n" || ch === "\r") {
            i++;
            continue;
        }

        if (ch === '"') {
            let j = i + 1;
            while (j < n && input[j] !== '"') j++;
            const phrase = input.slice(i + 1, j);
            if (phrase.length > 0) words.push({ text: phrase, quoted: true });
            i = j < n ? j + 1 : n;
            continue;
        }

        let j = i;
        while (j < n && input[j] !== " " && input[j] !== "\t" && input[j] !== "\n" && input[j] !== "\r" && input[j] !== '"') {
            j++;
        }
        const word = input.slice(i, j);
        i = j;
        if (word.length > 0) words.push({ text: word, quoted: false });
    }

    return words;
}

/**
 * Tokenize a query string into an array of tokens:
 *   { type: "phrase", value: "lowercased literal substring" }
 *   { type: "op", value: "AND" | "OR" }
 * Consecutive non-operator words are merged into a single phrase token
 * (space-joined), since bare adjacency is a literal phrase, not AND.
 */
export function tokenize(input) {
    if (!input) return [];

    const words = splitWords(input);
    const tokens = [];
    let buffer = [];

    const flush = () => {
        if (buffer.length > 0) {
            tokens.push({ type: "phrase", value: buffer.join(" ").toLowerCase() });
            buffer = [];
        }
    };

    for (const w of words) {
        if (!w.quoted && (w.text === "AND" || w.text === "OR")) {
            flush();
            tokens.push({ type: "op", value: w.text });
        } else {
            buffer.push(w.text);
        }
    }
    flush();

    return tokens;
}

/**
 * Parse a token stream into an AST.
 *   { op: "or", children: [...] }
 *   { op: "and", children: [...] }
 *   { term: "lowercased literal substring" }
 * Returns null for an empty/degenerate query with no terms.
 */
export function parse(input) {
    const tokens = tokenize(input);
    if (tokens.length === 0) return null;

    let pos = 0;

    function peek() {
        return pos < tokens.length ? tokens[pos] : null;
    }

    function parseOr() {
        const children = [parseAnd()].filter(Boolean);

        while (true) {
            const t = peek();
            if (t && t.type === "op" && t.value === "OR") {
                pos++; // consume OR
                const next = parseAnd();
                if (next) children.push(next);
                continue;
            }
            break;
        }

        if (children.length === 0) return null;
        if (children.length === 1) return children[0];
        return { op: "or", children };
    }

    function parseAnd() {
        const children = [];

        while (true) {
            const t = peek();
            if (!t) break;

            if (t.type === "op") {
                if (t.value === "OR") break; // let parseOr handle it
                if (t.value === "AND") {
                    pos++; // consume, keep collecting phrases
                    continue;
                }
            }

            if (t.type === "phrase") {
                pos++;
                children.push({ term: t.value });
                continue;
            }

            pos++; // unreachable with current token types, but stay lenient
        }

        if (children.length === 0) return null;
        if (children.length === 1) return children[0];
        return { op: "and", children };
    }

    return parseOr();
}

/**
 * Compile an AST node into a predicate function over a lowercased string.
 */
function compile(node) {
    if (!node) return null;

    if (node.term !== undefined) {
        const term = node.term;
        return (haystack) => haystack.includes(term);
    }

    const compiledChildren = node.children.map(compile).filter(Boolean);

    if (node.op === "and") {
        return (haystack) => compiledChildren.every((fn) => fn(haystack));
    }

    if (node.op === "or") {
        return (haystack) => compiledChildren.some((fn) => fn(haystack));
    }

    return null;
}

/**
 * Build a predicate function `(textLower) => boolean` from a raw query
 * string. Returns null for an empty/degenerate query (meaning: no filter,
 * show everything).
 */
export function buildPredicate(input) {
    const ast = parse(input);
    if (!ast) return null;
    return compile(ast);
}
