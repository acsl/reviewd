"""Review state read back from the pull request itself, instead of a local database.

``StateDB`` stores two things: which commits have been reviewed, and which inline findings are still
open. The PR already records both — the first in the summary comment's marker, the second in the
inline comments — so this class answers the same questions by reading them, and needs no storage of
its own.

Interface-compatible with ``StateDB``, so ``_process_pr`` and ``post_review`` accept either. Every
writing method is a no-op: posting the comment is what records the state.

Two consequences of reading rather than caching. Anything done to the PR outside reviewd is visible —
a thread resolved by hand counts as resolved, a deleted comment is simply gone. And a review that
posts nothing records nothing, so a dry run leaves no trace.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from reviewd.marker import is_bot_comment, parse_inline_comment, parse_summary_marker

logger = logging.getLogger(__name__)


class ProviderState:
    def __init__(self, provider):
        if not hasattr(provider, 'list_comments'):
            raise ValueError(
                f'{type(provider).__name__} cannot read back its own comments, so it does not '
                "support state: provider. Use state: sqlite for this repo's provider."
            )
        self.provider = provider
        self._cache: dict[tuple[str, int], list[dict]] = {}

    def _comments(self, repo_slug: str, pr_id: int) -> list[dict]:
        key = (repo_slug, pr_id)
        if key not in self._cache:
            self._cache[key] = self.provider.list_comments(repo_slug, pr_id)
            logger.debug('Fetched %d comments for %s PR #%d', len(self._cache[key]), repo_slug, pr_id)
        return self._cache[key]

    def _summaries(self, repo_slug: str, pr_id: int) -> list[dict]:
        """reviewd's own top-level comments — the ones that stand for "a review happened"."""
        return [
            c
            for c in self._comments(repo_slug, pr_id)
            if not c.get('inline') and not c.get('parent') and is_bot_comment(_raw(c))
        ]

    # --- reads -------------------------------------------------------------

    def has_review(self, repo_slug: str, pr_id: int, source_commit: str) -> bool:
        return any(parse_summary_marker(_raw(c)) == source_commit for c in self._summaries(repo_slug, pr_id))

    def has_any_review(self, repo_slug: str, pr_id: int) -> bool:
        return any(is_bot_comment(_raw(c)) for c in self._comments(repo_slug, pr_id))

    def minutes_since_last_review(self, repo_slug: str, pr_id: int) -> float | None:
        """How long ago reviewd last commented, standing in for when it last reviewed.

        Counts any comment of ours, not just summaries: a review posts all of its comments within
        seconds, so any one of them dates it, and an edited summary that has lost its marker would
        otherwise disable the cooldown entirely.
        """
        stamps = [_created(c) for c in self._comments(repo_slug, pr_id) if is_bot_comment(_raw(c))]
        stamps = [s for s in stamps if s is not None]
        if not stamps:
            return None
        return (datetime.now(UTC) - max(stamps)).total_seconds() / 60

    def get_open_inline_comments(self, repo_slug: str, pr_id: int) -> list[dict]:
        """reviewd's unresolved inline findings, in the shape post_review and the prompt expect.

        A thread is resolved when `resolution` is *present*, whatever it contains: BitBucket sends an
        empty dict `{}` for resolved and `null` for unresolved, so testing truthiness reads every
        resolved thread as open and re-posts findings already on the PR.

        A comment must both carry the marker and parse as a rendered finding. The marker alone is
        not enough — replies carry it too — and the shape alone is not enough, since a hand-written
        "🔴 **Careful here**" matches it, and claiming that thread would mean resolving and replying
        to someone else's comment. Missing one of reviewd's own comments only costs a duplicate, so
        the check errs towards rejecting.
        """
        open_comments = []
        for c in self._comments(repo_slug, pr_id):
            inline = c.get('inline')
            if not inline or c.get('parent') or c.get('resolution') is not None:
                continue
            raw = _raw(c)
            if not is_bot_comment(raw):
                continue
            parsed = parse_inline_comment(raw)
            if parsed is None:
                continue
            open_comments.append(
                {
                    'comment_id': c['id'],
                    'file': inline.get('path'),
                    # `to` is the new side of the diff, `from` the old one; findings are posted
                    # against whichever the finding referred to.
                    'line': inline.get('to') if inline.get('to') is not None else inline.get('from'),
                    'title': parsed['title'],
                    'issue': parsed['issue'],
                    'severity': parsed['severity'],
                }
            )
        return open_comments

    # --- writes ------------------------------------------------------------
    # No-ops: the comment posted to the PR is itself the record. Present so this class can stand in
    # for StateDB without _process_pr or post_review knowing which one they hold.

    def start_review(self, repo_slug: str, pr_id: int, source_commit: str) -> None:
        # Drops the cached comments so the review reads the PR as it is now. StateDB also marks the
        # review in_progress here to keep a second worker off the same PR; there is no equivalent on
        # the PR, so two concurrent reviews of one PR can both post.
        self._cache.pop((repo_slug, pr_id), None)

    def finish_review(self, repo_slug: str, pr_id: int, source_commit: str, *, error: str | None = None) -> None:
        pass

    def record_comment(self, repo_slug: str, pr_id: int, comment_id: int, **kwargs) -> None:
        pass

    def mark_comment_resolved(self, comment_id: int) -> None:
        # provider.resolve_comment() has already been called and is the durable record.
        pass

    def get_review_history(self, repo_slug: str, limit: int = 20) -> list[dict]:
        raise NotImplementedError('review history is only kept by the local database (state: sqlite)')

    def close(self) -> None:
        pass


def _raw(comment: dict) -> str:
    return comment.get('content', {}).get('raw') or ''


def _created(comment: dict) -> datetime | None:
    stamp = comment.get('created_on')
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        logger.debug('Unparseable comment timestamp %r', stamp)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
