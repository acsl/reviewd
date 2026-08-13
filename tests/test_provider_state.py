"""Review state read back from PR comments.

Comment payloads mirror what BitBucket returns, including the detail that is easiest to get backwards:
a resolved thread carries `resolution` as an empty dict, an unresolved one carries `null`. The
`RESOLVED` / `UNRESOLVED` constants below name those values so the fixtures cannot be misread.

Comments here also share one author, as they do in practice — reviewd authenticates with an ordinary
user's API token — so the tests exercise identification by marker rather than by author.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from reviewd.commenter import _format_inline_comment
from reviewd.marker import BOT_MARKER, format_summary_marker
from reviewd.models import GlobalConfig
from reviewd.provider_state import ProviderState

from .helpers import make_finding

COMMIT = '2e711e5982dd1a2b3c4d5e6f708192a3b4c5d6e7'


class FakeProvider:
    def __init__(self, comments):
        self._comments = comments
        self.calls = 0

    def list_comments(self, repo_slug, pr_id):
        self.calls += 1
        return list(self._comments)


_ABSENT = object()

# The two values BitBucket sends for the resolution field.
RESOLVED = {}
UNRESOLVED = None


def _comment(cid, raw, *, inline=None, resolution=_ABSENT, parent=None, created=None):
    data = {
        'id': cid,
        'content': {'raw': raw},
        'created_on': (created or datetime.now(UTC)).isoformat(),
    }
    if inline is not None:
        data['inline'] = inline
    if resolution is not _ABSENT:
        data['resolution'] = resolution
    if parent is not None:
        data['parent'] = {'id': parent}
    return data


def _finding_comment(cid, finding, **kwargs):
    return _comment(cid, f'{_format_inline_comment(finding)}\n\n{BOT_MARKER}', **kwargs)


def _summary(cid, commit=COMMIT, **kwargs):
    body = "## review'd by Claude\n\nAll good.\n\n"
    if commit:
        body += f'{format_summary_marker(commit)}\n\n'
    return _comment(cid, body + BOT_MARKER, **kwargs)


def state_for(comments):
    return ProviderState(FakeProvider(comments))


# ---------------------------------------------------------------------------
# Open inline findings
# ---------------------------------------------------------------------------


def test_open_inline_comments_recovers_the_finding():
    finding = make_finding(severity='critical', title='NPE escapes the caller', issue='Line one.\nLine two.')
    state = state_for([_finding_comment(11, finding, inline={'path': 'a/B.java', 'to': 71}, resolution=UNRESOLVED)])

    assert state.get_open_inline_comments('repo', 1) == [
        {
            'comment_id': 11,
            'file': 'a/B.java',
            'line': 71,
            'title': 'NPE escapes the caller',
            'issue': 'Line one.\nLine two.',
            'severity': 'critical',
        }
    ]


@pytest.mark.parametrize(
    'resolution',
    [
        # What BitBucket sends for a resolved thread; falsy, so a truthiness check would miss it.
        RESOLVED,
        # A populated object must read the same way.
        {'type': 'pullrequest_comment_resolution', 'user': {'nickname': 'someone'}},
    ],
    ids=['empty-dict', 'populated'],
)
def test_resolved_threads_are_not_open(resolution):
    state = state_for(
        [_finding_comment(11, make_finding(), inline={'path': 'a/B.java', 'to': 71}, resolution=resolution)]
    )
    assert state.get_open_inline_comments('repo', 1) == []


def test_absent_resolution_field_means_open():
    """Not every payload carries the key at all; absent must read the same as null."""
    state = state_for([_finding_comment(11, make_finding(), inline={'path': 'a/B.java', 'to': 71})])
    assert len(state.get_open_inline_comments('repo', 1)) == 1


def test_ignores_comments_that_are_not_ours():
    """A human's inline comment has no marker — and the same author as reviewd's own."""
    state = state_for(
        [
            # Unresolved, so it is the missing marker — not the resolution state — that excludes them.
            _comment(11, 'null check for locationNode?', inline={'path': 'a/B.java', 'to': 65}, resolution=UNRESOLVED),
            _comment(12, 'use `StandardCharsets.UTF_8`', inline={'path': 'a/B.java', 'to': 42}, resolution=UNRESOLVED),
        ]
    )
    assert state.get_open_inline_comments('repo', 1) == []


def test_ignores_a_human_comment_that_mimics_the_finding_format():
    """Without the marker it is not ours, however much it looks like it.

    Claiming it would make reviewd resolve and reply to someone else's thread.
    """
    finding = make_finding(severity='critical', title='Careful here')
    mimic = _format_inline_comment(finding)  # identical body, no marker appended
    state = state_for([_comment(11, mimic, inline={'path': 'a/B.java', 'to': 71}, resolution=UNRESOLVED)])

    assert state.get_open_inline_comments('repo', 1) == []


def test_ignores_replies_and_summaries():
    state = state_for(
        [
            _summary(10),
            _comment(
                11,
                f'Still unresolved.\n\n{BOT_MARKER}',
                inline={'path': 'a/B.java', 'to': 71},
                # Unresolved, so it is the parent link that excludes it.
                resolution=UNRESOLVED,
                parent=9,
            ),
        ]
    )
    assert state.get_open_inline_comments('repo', 1) == []


def test_falls_back_to_the_old_side_of_the_diff():
    finding = make_finding()
    state = state_for([_finding_comment(11, finding, inline={'path': 'a/B.java', 'to': None, 'from': 40})])
    assert state.get_open_inline_comments('repo', 1)[0]['line'] == 40


# ---------------------------------------------------------------------------
# Whether the PR has been reviewed
# ---------------------------------------------------------------------------


def test_has_review_matches_only_the_recorded_commit():
    state = state_for([_summary(10, commit=COMMIT)])
    assert state.has_review('repo', 1, COMMIT)
    assert not state.has_review('repo', 1, 'a' * 40)


def test_summary_without_a_commit_marker_counts_as_reviewed_but_not_at_this_commit():
    """Summaries posted before the marker existed: re-review once, without duplicating threads."""
    state = state_for([_summary(10, commit=None)])
    assert state.has_any_review('repo', 1)
    assert not state.has_review('repo', 1, COMMIT)


def test_unreviewed_pr():
    state = state_for([_comment(11, 'looks good to me')])
    assert not state.has_any_review('repo', 1)
    assert not state.has_review('repo', 1, COMMIT)
    assert state.minutes_since_last_review('repo', 1) is None


def test_minutes_since_last_review_uses_the_newest_comment_of_ours():
    now = datetime.now(UTC)
    state = state_for(
        [
            _summary(10, created=now - timedelta(hours=3)),
            _summary(11, created=now - timedelta(minutes=30)),
        ]
    )
    assert 29 < state.minutes_since_last_review('repo', 1) < 31


def test_cooldown_survives_a_summary_edited_past_recognition():
    """A summary edited in the web UI can lose its marker; the inline findings still date the review."""
    now = datetime.now(UTC)
    state = state_for(
        [
            _comment(10, "## review'd by Claude\n\nEdited by hand.\n\n‌", created=now - timedelta(minutes=30)),
            _finding_comment(
                11,
                make_finding(),
                inline={'path': 'a/B.java', 'to': 71},
                created=now - timedelta(minutes=30),
            ),
        ]
    )
    assert state.minutes_since_last_review('repo', 1) is not None
    assert 29 < state.minutes_since_last_review('repo', 1) < 31


def test_no_comments_of_ours_means_no_cooldown():
    state = state_for([_comment(10, 'looks good to me')])
    assert state.minutes_since_last_review('repo', 1) is None


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def test_comments_are_fetched_once_then_reused():
    provider = FakeProvider([_summary(10)])
    state = ProviderState(provider)
    state.has_review('repo', 1, COMMIT)
    state.has_any_review('repo', 1)
    state.minutes_since_last_review('repo', 1)
    assert provider.calls == 1


def test_starting_a_review_refetches():
    """The daemon reuses one store across polls, so cached comments must not outlive a review."""
    provider = FakeProvider([_summary(10)])
    state = ProviderState(provider)
    state.has_any_review('repo', 1)
    state.start_review('repo', 1, COMMIT)
    state.has_any_review('repo', 1)
    assert provider.calls == 2


def test_writes_are_accepted_and_do_nothing():
    state = state_for([])
    state.start_review('repo', 1, COMMIT)
    state.record_comment('repo', 1, 11, kind='inline', file='a', line=1)
    state.mark_comment_resolved(11)
    state.finish_review('repo', 1, COMMIT)
    state.finish_review('repo', 1, COMMIT, error='boom')
    state.close()


def test_provider_without_comment_listing_is_rejected():
    class GithubLike:
        pass

    with pytest.raises(ValueError, match='state: provider'):
        ProviderState(GithubLike())


def test_make_state_selects_the_backend(tmp_path):
    from reviewd.config import make_state
    from reviewd.state import StateDB

    provider = FakeProvider([])
    assert isinstance(make_state(GlobalConfig(repos=[], state='provider'), provider), ProviderState)

    db = make_state(GlobalConfig(repos=[], state='sqlite', state_db=str(tmp_path / 'state.db')), provider)
    assert isinstance(db, StateDB)
    db.close()
