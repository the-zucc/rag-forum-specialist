"""Parsing of the model's structured output.

The planner/cross-check prompts ask for fixed `FIELD: value` lines; the
distillation prompt asks for one knowledge piece per line with its supporting
post ids. All parsing is tolerant of the markdown the model sneaks in.
"""

from __future__ import annotations

import re

MAX_KEYWORDS = 5
# Ignore obviously-empty "facts" the model might emit.
MIN_STATEMENT_CHARS = 15


def parse_field(raw, field):
    match = re.search(
        rf"^[\s>*#-]*{field}[\s*]*:\s*(.*?)\s*$", raw, re.IGNORECASE | re.MULTILINE
    )
    return match.group(1).strip() if match else ""


def parse_keywords(value):
    keywords = []
    for part in re.split(r"[,;\n]", value):
        part = part.strip().strip("\"'`*-–").strip()
        if part and part.lower() not in ("none", "n/a") and part.lower() not in (
            k.lower() for k in keywords
        ):
            keywords.append(part)
    return keywords[:MAX_KEYWORDS]


# Each line: "- <statement> [posts: 629, 631]". The bracket is optional.
_PIECE_RE = re.compile(r"^(.*?)\s*\[posts?:\s*([^\]]*)\]\s*$", re.I)


def parse_pieces(raw, valid_post_ids):
    """Parse the model's lines into (statement, [post_id, …]) tuples."""
    pieces = []
    for line in raw.splitlines():
        line = line.strip().lstrip("-*•").strip()
        if not line or line.upper() == "NONE":
            continue
        post_ids = []
        match = _PIECE_RE.match(line)
        if match:
            statement = match.group(1).strip()
            post_ids = [
                pid for pid in re.split(r"[,\s]+", match.group(2)) if pid in valid_post_ids
            ]
        else:
            statement = line
        statement = statement.strip().strip("*_`").strip()
        if len(statement) < MIN_STATEMENT_CHARS:
            continue
        pieces.append((statement, post_ids))
    return pieces
