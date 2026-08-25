# Tighten English "not X but Y" hard check

## Problem

`_CHAPTER_FORBIDDEN_STYLE_PATTERNS` English `not X but Y` regex is:

```
\bnot\b(?!\s+only\b)[^.\n]{0,60}?\bbut\b
```

That matches any `not` … `but` in one sentence. Chapter 2 of esmee aborted on two ordinary coordinations:

- "The bra did not dig in, but it did not move either."
- "He did not stop, but he slowed his pace"

## Goal

Flag the rhetorical contrast formula (English analogue of `不是…而是`), not verb-negation plus coordinating `but`.

## Status

Done. English pattern now matches copula/`not a…`/sentence-initial contrast, and ignores verb-negation + coordinating `but`. Tests in `tests/test_chapter_style_violations.py`.

## Done when

- New unittest file `tests/test_chapter_style_violations.py` is green.
- Templates still flag; the two chapter-2 sentences do not.
- Chinese patterns, `not only X but also Y`, and `——` are unchanged in intent.
- Smallest change: pattern(s) plus tests. No rewrite of the repair loop.
