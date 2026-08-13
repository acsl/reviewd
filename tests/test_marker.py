"""Comment metadata encoding.

ProviderState depends on _format_inline_comment being exactly reversible: whatever it renders,
parse_inline_comment must recover. The round-trip is asserted against the real formatter rather than
a hand-written sample, so the two cannot drift apart unnoticed.
"""

from __future__ import annotations

import pytest

from reviewd.commenter import _format_inline_comment
from reviewd.marker import (
    BOT_MARKER,
    format_summary_marker,
    is_bot_comment,
    parse_inline_comment,
    parse_summary_marker,
)
from reviewd.models import Severity

from .helpers import make_finding

# A comment body as BitBucket stored and returned it, abridged. Kept verbatim so the parser is
# exercised against real markup — escaped characters, hard breaks, a suggestion block and the marker.
REAL_COMMENT = (
    '\U0001f534 **HTTP 404 (v4 equivalent of ZERO_RESULTS) now throws instead of returning null**\n'
    '\n'
    'The v3→v4 status mapping turns `ZERO_RESULTS` into HTTP `404 NOT_FOUND`, not a 200 '
    'response with an empty `results` array.  \n'
    '  \n'
    'The v3 code explicitly returned `null` for `ZERO_RESULTS`, so this is a behaviour regression.\n'
    '\n'
    '```suggestion\n'
    '      } else if (response.statusCode() == 400 || response.statusCode() == 404) {\n'
    '```\n'
    '\n'
    '[](reviewd)'
)


@pytest.mark.parametrize('severity', list(Severity))
@pytest.mark.parametrize('fix', [None, 'int x = 1;'])
def test_inline_comment_round_trips(severity, fix):
    finding = make_finding(
        severity=severity.value,
        title='HTTP 404 (v4 equivalent of ZERO_RESULTS) throws',
        issue='First line.\nSecond line with `code`.',
        fix=fix,
    )
    parsed = parse_inline_comment(f'{_format_inline_comment(finding)}\n\n{BOT_MARKER}')

    assert parsed == {
        'severity': finding.severity.value,
        'title': finding.title,
        'issue': finding.issue,
    }


def test_parses_a_comment_bitbucket_actually_stored():
    parsed = parse_inline_comment(REAL_COMMENT)

    assert parsed is not None
    assert parsed['severity'] == 'critical'
    assert parsed['title'] == 'HTTP 404 (v4 equivalent of ZERO_RESULTS) now throws instead of returning null'
    assert parsed['issue'].startswith('The v3→v4 status mapping')
    # The ```suggestion``` block is a rendering of Finding.fix, not part of the issue text.
    assert 'suggestion' not in parsed['issue']
    # Hard-break padding is undone, so the text matches what the model originally produced.
    assert '  \n' not in parsed['issue']


@pytest.mark.parametrize(
    'raw',
    [
        'null check for locationNode?',
        'use `StandardCharsets.UTF_8` in stead of literal "UTF-8"',
        f"## review'd by Claude\n\nNo findings.\n\n{BOT_MARKER}",
        f'Still unresolved. See above.\n\n{BOT_MARKER}',
        '',
    ],
)
def test_rejects_anything_that_is_not_a_rendered_finding(raw):
    assert parse_inline_comment(raw) is None


def test_summary_marker_round_trips():
    commit = '2e711e5982dd1a2b3c4d5e6f708192a3b4c5d6e7'
    raw = f'## summary\n\n{format_summary_marker(commit)}\n\n{BOT_MARKER}'

    assert parse_summary_marker(raw) == commit
    assert is_bot_comment(raw)


def test_summary_without_a_commit_marker():
    assert parse_summary_marker(f'## summary\n\n{BOT_MARKER}') is None
    assert is_bot_comment(f'## summary\n\n{BOT_MARKER}')


def test_human_comments_are_not_ours():
    assert not is_bot_comment('null check for locationNode?')
    assert not is_bot_comment('')
