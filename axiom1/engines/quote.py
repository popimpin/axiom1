"""Quote engine: source citation verification, line search, and exact excerpting."""
import re

from ._base import EngineError

SPEC = {
    "name": "quote",
    "version": 1,
    "summary": "verify cited passages in source text, search lines by terms, and excerpt line ranges",
    "functions": {
        "find_lines": {
            "args": ["text", "terms"],
            "returns": "list of {line, text} matching all terms case-insensitively",
            "io": False,
            "examples": [
                {
                    "args": {
                        "text": "First line.\nSecond term line.\nThird line.",
                        "terms": ["second", "line"],
                    },
                    "returns": [{"line": 2, "text": "Second term line."}],
                },
                {
                    "args": {"text": "A line.", "terms": []},
                    "refuses": "non-empty",
                },
            ],
        },
        "verify_quote": {
            "args": ["text", "quote"],
            "returns": "{found, line} reporting the 1-based start line of the exact quote",
            "io": False,
            "examples": [
                {
                    "args": {
                        "text": "This is a formal policy.\nCoverage applies to damage.",
                        "quote": "Coverage applies to damage.",
                    },
                    "returns": {"found": True, "line": 2},
                },
                {
                    "args": {
                        "text": "Coverage applies\n  to all vehicles.",
                        "quote": "Coverage applies to all vehicles.",
                    },
                    "returns": {"found": True, "line": 1},
                },
                {
                    "args": {
                        "text": "Coverage applies to damage.",
                        "quote": "Coverage applies",
                    },
                    "refuses": "3 words",
                },
                {
                    "args": {
                        "text": "Coverage applies to damage.",
                        "quote": "Coverage does not apply here",
                    },
                    "refuses": "not found in the source",
                },
            ],
        },
        "excerpt": {
            "args": ["text", "start_line", "end_line"],
            "returns": "exact lines joined with newline",
            "io": False,
            "examples": [
                {
                    "args": {
                        "text": "Line 1\nLine 2\nLine 3",
                        "start_line": 2,
                        "end_line": 3,
                    },
                    "returns": "Line 2\nLine 3",
                },
                {
                    "args": {
                        "text": "Line 1\nLine 2",
                        "start_line": 2,
                        "end_line": 1,
                    },
                    "refuses": "cannot be greater than",
                },
            ],
        },
    },
}


def find_lines(text, terms):
    """Find 1-based line numbers and text of lines containing ALL terms (case-insensitive).

    Refuses empty terms list or empty terms.
    """
    if not isinstance(text, str):
        raise EngineError("text must be a string")
    if not isinstance(terms, (list, tuple)) or not terms:
        raise EngineError("terms must be a non-empty list of terms")
    if not all(isinstance(t, str) and t.strip() for t in terms):
        raise EngineError("all terms must be non-empty strings")

    terms_lower = [t.strip().lower() for t in terms]
    lines = text.splitlines()

    matches = []
    for n, line in enumerate(lines, 1):
        line_lower = line.lower()
        if all(t in line_lower for t in terms_lower):
            matches.append({"line": n, "text": line})

    return matches


def verify_quote(text, quote):
    """Verify that a cited quote appears verbatim in the source text.

    Matches after collapsing whitespace runs to single spaces, case-sensitively.
    Quotes spanning across lines are supported and report the 1-based start line.
    Refuses quotes under 3 words (too short to prove citation) or quotes not found
    in the source.
    """
    if not isinstance(text, str):
        raise EngineError("text must be a string")
    if not isinstance(quote, str) or not quote.strip():
        raise EngineError("quote must be a non-empty string")

    words = quote.strip().split()
    if len(words) < 3:
        raise EngineError(f"quote {quote!r} is under 3 words; a quote must have at least 3 words to prove citation")

    pattern = r"\s+".join(re.escape(w) for w in words)
    m = re.search(pattern, text)
    if not m:
        raise EngineError(f"quote {quote!r} was not found in the source text")

    start_line = text[:m.start()].count("\n") + 1
    return {"found": True, "line": start_line}


def excerpt(text, start_line, end_line):
    """Return exact lines from start_line to end_line (1-based, inclusive) joined with newline.

    Refuses out-of-range line numbers or start_line > end_line.
    """
    if not isinstance(text, str):
        raise EngineError("text must be a string")
    if not isinstance(start_line, int) or isinstance(start_line, bool) or start_line < 1:
        raise EngineError(f"start_line must be an integer >= 1, got {start_line!r}")
    if not isinstance(end_line, int) or isinstance(end_line, bool) or end_line < 1:
        raise EngineError(f"end_line must be an integer >= 1, got {end_line!r}")

    lines = text.splitlines()
    total = len(lines)

    if start_line > end_line:
        raise EngineError(f"start_line ({start_line}) cannot be greater than end_line ({end_line})")
    if start_line > total or end_line > total:
        raise EngineError(f"line range {start_line}..{end_line} is out of range for text with {total} lines")

    return "\n".join(lines[start_line - 1 : end_line])


GUIDE = '''Use quote whenever an answer must cite its source.
- `quote.find_lines(text, ["refund", "days"])` finds candidate lines (all terms, any case), with line numbers.
- `quote.verify_quote(text, "Refunds are issued within 30 days")` proves the exact passage is in the source
  and returns its line. Check every quotation you deliver with it; a refusal means you misquoted - copy the words exactly.
- `quote.excerpt(text, start_line, end_line)` returns exact lines to quote.'''
