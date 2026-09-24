"""
contains_search.py — AND/OR/phrase query grammar for the Workflows+ "Contains"
search (scans inside workflow JSON files server-side).

This mirrors the JS grammar in
web/js/workflows_plus/queryParser.js exactly, so filename search and
content search behave identically:

  - Bare adjacency ("black cat") is a literal, contiguous substring phrase —
    NOT an implicit AND. It does not match "black cute cat".
  - "..." quotes behave the same as the equivalent bare phrase; content
    inside quotes is never treated as an operator.
  - AND / OR are recognized ONLY as exact uppercase, unquoted, whitespace-
    bounded words. AND binds tighter than OR.
  - Malformed input (dangling operators, unbalanced quotes) is handled
    leniently rather than raising.

Keep this in sync with web/js/workflows_plus/queryParser.js if the grammar
changes.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple


def _split_words(text: str) -> List[Tuple[str, bool]]:
    """Split into (word, quoted) pairs; quoted "..." sections are one unit."""
    words: List[Tuple[str, bool]] = []
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]

        if ch in " \t\n\r":
            i += 1
            continue

        if ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 1
            phrase = text[i + 1:j]
            if phrase:
                words.append((phrase, True))
            i = j + 1 if j < n else n
            continue

        j = i
        while j < n and text[j] not in " \t\n\r\"":
            j += 1
        word = text[i:j]
        i = j
        if word:
            words.append((word, False))

    return words


def tokenize(text: str) -> List[dict]:
    """Return a list of {"type": "phrase"|"op", "value": str} tokens."""
    if not text:
        return []

    tokens: List[dict] = []
    buffer: List[str] = []

    def flush():
        if buffer:
            tokens.append({"type": "phrase", "value": " ".join(buffer).lower()})
            buffer.clear()

    for word, quoted in _split_words(text):
        if not quoted and word in ("AND", "OR"):
            flush()
            tokens.append({"type": "op", "value": word})
        else:
            buffer.append(word)
    flush()

    return tokens


def parse(text: str) -> Optional[dict]:
    """Parse into an AST: {"op": "and"|"or", "children": [...]} or {"term": str}."""
    tokens = tokenize(text)
    if not tokens:
        return None

    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else None

    def parse_and():
        nonlocal pos
        children = []
        while True:
            t = peek()
            if t is None:
                break
            if t["type"] == "op":
                if t["value"] == "OR":
                    break
                if t["value"] == "AND":
                    pos += 1
                    continue
            if t["type"] == "phrase":
                pos += 1
                children.append({"term": t["value"]})
                continue
            pos += 1
        if not children:
            return None
        if len(children) == 1:
            return children[0]
        return {"op": "and", "children": children}

    def parse_or():
        nonlocal pos
        children = []
        first = parse_and()
        if first:
            children.append(first)
        while True:
            t = peek()
            if t and t["type"] == "op" and t["value"] == "OR":
                pos += 1
                nxt = parse_and()
                if nxt:
                    children.append(nxt)
                continue
            break
        if not children:
            return None
        if len(children) == 1:
            return children[0]
        return {"op": "or", "children": children}

    return parse_or()


def _compile(node: Optional[dict]) -> Optional[Callable[[str], bool]]:
    if node is None:
        return None

    if "term" in node:
        term = node["term"]
        return lambda haystack: term in haystack

    compiled = [c for c in (_compile(child) for child in node["children"]) if c]

    if node["op"] == "and":
        return lambda haystack: all(fn(haystack) for fn in compiled)
    if node["op"] == "or":
        return lambda haystack: any(fn(haystack) for fn in compiled)
    return None


def build_predicate(text: str) -> Optional[Callable[[str], bool]]:
    """Build a predicate `(text_lower) -> bool` from a raw query string.
    Returns None for an empty/degenerate query."""
    ast = parse(text)
    if ast is None:
        return None
    return _compile(ast)
