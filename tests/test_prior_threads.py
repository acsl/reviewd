"""How a review acts on reviewd's earlier inline threads: resolve, agree, or answer once."""

from __future__ import annotations

import pytest
from helpers import make_finding, make_result

from reviewd.commenter import post_review
from reviewd.models import PriorResolution
from reviewd.reviewer import parse_review_result

THREAD = 11


def _prior(replies=()):
    return {
        'comment_id': THREAD,
        'file': 'src/main.py',
        'line': 10,
        'title': 'Test finding',
        'issue': 'Something is wrong',
        'severity': 'critical',
        'replies': list(replies),
    }


REVIEWD_REPLY = {'author': 'Teamcity reviewd', 'text': '⚠️ This has not been addressed yet.', 'by_reviewd': True}
FIXD_REPLY = {'author': 'Teamcity fixd', 'text': 'Not changed: product decision.', 'by_reviewd': False}


@pytest.fixture()
def with_prior(state_db, monkeypatch):
    def install(prior):
        monkeypatch.setattr(state_db, 'get_open_inline_comments', lambda repo_slug, pr_id: [prior])
        return state_db

    return install


def _still_open():
    finding = make_finding(severity='critical')
    finding.prior_id = THREAD
    return make_result([finding])


@pytest.mark.parametrize(
    ('replies', 'answers'),
    [
        ([], True),  # first time: say it once
        ([REVIEWD_REPLY], False),  # reviewd already had the last word: stay quiet
        ([REVIEWD_REPLY, FIXD_REPLY], True),  # someone answered since: answer them
    ],
    ids=['no-replies', 'last-word-ours', 'answered-since'],
)
def test_not_addressed_is_said_once_per_turn(provider, with_prior, pr, project_config, global_config, replies, answers):
    post_review(provider, with_prior(_prior(replies)), pr, _still_open(), project_config, global_config)

    assert [r['parent'] for r in provider.replies] == ([THREAD] if answers else [])
    assert provider.resolved_comments == []
    # A matched finding lives in its thread, never in the summary, whether or not it was answered.
    assert all('Test finding' not in c['body'] for c in provider.posted_comments)


@pytest.mark.parametrize(
    ('kind', 'prefix'),
    [('fixed', '✅ This looks resolved by the latest changes.'), ('accepted', '✅ Agreed, closing:')],
)
def test_resolution_reply_depends_on_why(provider, with_prior, pr, project_config, global_config, kind, prefix):
    result = make_result([])
    result.resolved_priors = [PriorResolution(comment_id=THREAD, note='Fair point.', kind=kind)]

    post_review(provider, with_prior(_prior([FIXD_REPLY])), pr, result, project_config, global_config)

    assert provider.resolved_comments == [THREAD]
    assert provider.replies == [{'id': provider.replies[0]['id'], 'parent': THREAD, 'body': f'{prefix} Fair point.'}]


def test_parse_reads_the_resolution_kind():
    data = {
        'overview': '',
        'findings': [],
        'summary': '',
        'resolved_prior_ids': [
            {'id': 1, 'note': 'a', 'kind': 'accepted'},
            {'id': 2, 'note': 'b', 'kind': 'fixed'},
            {'id': 3, 'note': 'c'},  # older output without kind
            {'id': 4, 'note': 'd', 'kind': 'whatever'},
        ],
    }
    kinds = [(r.comment_id, r.kind) for r in parse_review_result(data).resolved_priors]
    assert kinds == [(1, 'accepted'), (2, 'fixed'), (3, 'fixed'), (4, 'fixed')]
