"""Encoding and decoding of the metadata reviewd leaves in its own PR comments.

Under ``state: provider`` a comment is the only record of a review, so two things must be recoverable
from a comment body:

* **whose it is** — ``BOT_MARKER``, an empty markdown link that renders to nothing. The comment
  author cannot be used: reviewd authenticates with an ordinary user's API token, so its comments
  carry the same author as that user's own.
* **what it recorded** — an inline comment is parsed back into the ``Finding`` fields it was
  rendered from. Only the summary carries metadata that is not already in its text: the reviewed
  commit.

Formatting and parsing are kept in the same module because they must stay symmetrical. If one side
changes alone, reviewd no longer recognises its own comments and posts every finding again.
"""

from __future__ import annotations

import re

from reviewd.models import Severity

# An empty markdown link: invisible once rendered, preserved verbatim in the raw body.
BOT_MARKER = '[](reviewd)'

SEVERITY_EMOJI = {
    Severity.CRITICAL: '\U0001f534',
    Severity.SUGGESTION: '\U0001f7e1',
    Severity.NITPICK: '\U0001f535',
    Severity.GOOD: '\U0001f7e2',
}

_EMOJI_SEVERITY = {emoji: severity for severity, emoji in SEVERITY_EMOJI.items()}

# Same shape as BOT_MARKER so it stays invisible, with the reviewed commit as the link target.
_SUMMARY_MARKER_RE = re.compile(r'\[\]\(reviewd:([0-9a-fA-F]{7,40})\)')

_INLINE_HEAD_RE = re.compile(
    r'^(?P<emoji>[{emoji}])[ ]\*\*(?P<title>.*?)\*\*\n\n(?P<issue>.*)$'.format(emoji=''.join(_EMOJI_SEVERITY)),
    re.DOTALL,
)

# Trailing ```suggestion``` block appended by _format_inline_comment when a Finding carries a fix.
_SUGGESTION_RE = re.compile(r'\n\n```suggestion\n.*?\n```\s*$', re.DOTALL)

_TRAILING_MARKER_RE = re.compile(rf'(?:\n+(?:{re.escape(BOT_MARKER)}|\[\]\(reviewd:[0-9a-fA-F]{{7,40}}\)))+\s*$')


def format_summary_marker(source_commit: str) -> str:
    """The trailer that records which commit a summary comment reviewed."""
    return f'[](reviewd:{source_commit})'


def parse_summary_marker(raw: str) -> str | None:
    """The commit a summary comment recorded, or None if it carries no such marker.

    A summary without the marker leaves the PR reviewed-at-some-point but not at any known commit,
    which makes it eligible for one more review and deduplicated after that.
    """
    match = _SUMMARY_MARKER_RE.search(raw)
    return match.group(1) if match else None


def is_bot_comment(raw: str) -> bool:
    return BOT_MARKER in raw or _SUMMARY_MARKER_RE.search(raw) is not None


def strip_markers(raw: str) -> str:
    return _TRAILING_MARKER_RE.sub('', raw)


def parse_inline_comment(raw: str) -> dict | None:
    """Recover the Finding fields that _format_inline_comment rendered into a comment body.

    Returns None for anything not matching that shape — a reply, a summary, or a comment edited past
    recognition. Callers treat None as "not one of ours" and post the finding fresh instead of
    threading onto it.
    """
    body = strip_markers(raw).strip()
    match = _INLINE_HEAD_RE.match(body)
    if not match:
        return None
    issue = _SUGGESTION_RE.sub('', match.group('issue')).strip()
    return {
        'severity': _EMOJI_SEVERITY[match.group('emoji')].value,
        'title': match.group('title').strip(),
        # Undo the two-space hard breaks added on the way out.
        'issue': issue.replace('  \n', '\n'),
    }
