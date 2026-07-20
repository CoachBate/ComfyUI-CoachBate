"""
test_contains_search.py — plain-assertion smoke test for contains_search.py
(the Python port of the Workflows+ AND/OR/phrase query grammar used by the
"Contains" server-side content search).

No pytest/unittest framework dependency, no ComfyUI import needed — this
module is pure stdlib. Run directly:

    python_embeded\\python.exe custom_nodes\\ComfyUI-CoachBate\\tests\\test_contains_search.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contains_search import build_predicate, tokenize  # noqa: E402

_failures = []


def check(label, actual, expected):
    if actual != expected:
        _failures.append(f"{label}: expected {expected!r}, got {actual!r}")


def test_bare_adjacency_is_literal_phrase():
    pred = build_predicate("black cat")
    check("bare/contiguous", pred("black cat"), True)
    check("bare/embedded", pred("my black cat photo"), True)
    check("bare/non-contiguous", pred("black cute cat"), False)
    check("bare/wrong-order", pred("cat black"), False)


def test_quoted_phrase_same_as_bare():
    bare = build_predicate("black cat")
    quoted = build_predicate('"black cat"')
    for s in ("black cat", "my black cat photo", "black cute cat", "cat black"):
        check(f"quoted==bare for {s!r}", quoted(s), bare(s))


def test_uppercase_and():
    pred = build_predicate("black AND cat")
    check("AND/both-any-order-1", pred("black cute cat"), True)
    check("AND/both-any-order-2", pred("cat is black"), True)
    check("AND/missing-one", pred("black only"), False)


def test_uppercase_or():
    pred = build_predicate("qwen OR flux")
    check("OR/left", pred("qwen_workflow"), True)
    check("OR/right", pred("flux_workflow"), True)
    check("OR/neither", pred("sdxl_workflow"), False)


def test_and_binds_tighter_than_or():
    pred = build_predicate("alpha OR bravo AND charlie")
    check("prec/alpha-only", pred("contains alpha only"), True)
    check("prec/bravo-and-charlie", pred("contains bravo and charlie"), True)
    check("prec/bravo-only", pred("contains bravo only"), False)


def test_lowercase_and_or_are_literal():
    pred = build_predicate("rock and roll")
    check("lower-and/literal-match", pred("rock and roll band"), True)
    check("lower-and/no-match", pred("rock guitar"), False)


def test_empty_query_returns_none():
    check("empty", build_predicate(""), None)
    check("whitespace", build_predicate("   "), None)


def test_dangling_operator():
    pred = build_predicate("qwen AND")
    check("dangling-and/match", pred("qwen workflow"), True)
    check("dangling-and/no-match", pred("other workflow"), False)


def test_tokenize_matches_js_shape():
    tokens = tokenize('black cat AND "hi res" OR flux')
    expected = [
        {"type": "phrase", "value": "black cat"},
        {"type": "op", "value": "AND"},
        {"type": "phrase", "value": "hi res"},
        {"type": "op", "value": "OR"},
        {"type": "phrase", "value": "flux"},
    ]
    check("tokenize", tokens, expected)


def main():
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        t()

    if _failures:
        print(f"FAILED ({len(_failures)}/{len(tests)} checks failed):")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print(f"OK — all checks passed across {len(tests)} test functions")


if __name__ == "__main__":
    main()
