from __future__ import annotations

import logging

from reviewd.models import (
    CLI,
    SEVERITY_ORDER,
    AutoApproveConfig,
    Finding,
    GlobalConfig,
    PRInfo,
    ProjectConfig,
    ReviewResult,
    Severity,
)
from reviewd.providers.base import GitProvider
from reviewd.state import StateDB

logger = logging.getLogger(__name__)

TASK_MARKER = '[reviewd]'

SEVERITY_EMOJI = {
    Severity.CRITICAL: '\U0001f534',
    Severity.SUGGESTION: '\U0001f7e1',
    Severity.NITPICK: '\U0001f535',
    Severity.GOOD: '\U0001f7e2',
}


def supports_comment_threads(provider) -> bool:
    """Whether the provider can resolve/reply to existing comments (BitBucket only)."""
    return hasattr(provider, 'resolve_comment') and hasattr(provider, 'reply_comment')


def _hard_breaks(text: str) -> str:
    # BitBucket/CommonMark render a lone newline as a space. Two trailing spaces
    # before the newline turn it into a visible line break.
    return text.replace('\n', '  \n')


def _format_finding_summary(finding: Finding) -> str:
    loc = ''
    if finding.file:
        loc = f' — `{finding.file}`'
        if finding.line:
            loc += f' (line {finding.line})'
    # Indent continuation lines so a multi-line issue stays inside the list item.
    issue = finding.issue.replace('\n', '  \n  ')
    return f'- **{finding.title}**{loc}  \n  {issue}'


# TODO: support multi-line suggestions (end_line) — needs correct line range in provider API calls
def _format_inline_comment(finding: Finding) -> str:
    emoji = SEVERITY_EMOJI.get(finding.severity, '')
    parts = [f'{emoji} **{finding.title}**', _hard_breaks(finding.issue)]
    if finding.fix:
        parts.append(f'```suggestion\n{finding.fix}\n```')
    return '\n\n'.join(parts)


_MAX_TALLY_DOTS = 3


def _format_inline_tally(inline_findings: list[Finding]) -> str:
    """Compact emoji tally of inline findings, e.g. '🔴🔴 🟡🟡🟡+2 — posted as inline comments'."""
    grouped: dict[Severity, int] = {}
    for f in inline_findings:
        grouped[f.severity] = grouped.get(f.severity, 0) + 1

    parts = []
    for severity in [Severity.CRITICAL, Severity.SUGGESTION, Severity.NITPICK]:
        count = grouped.get(severity, 0)
        if count == 0:
            continue
        emoji = SEVERITY_EMOJI[severity]
        shown = min(count, _MAX_TALLY_DOTS)
        part = emoji * shown
        if count > _MAX_TALLY_DOTS:
            part += f'+{count - _MAX_TALLY_DOTS}'
        parts.append(part)

    if not parts:
        return ''
    return ' '.join(parts) + ' — posted as inline comments'


def _format_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    if m > 0:
        return f'{m}m {s}s'
    return f'{s}s'


def _format_summary_comment(
    result: ReviewResult,
    inline_ids: set[int],
    global_config: GlobalConfig,
    project_config: ProjectConfig,
    cli: CLI = CLI.CLAUDE,
    approved: bool = False,
    approve_blocked_reason: str | None = None,
) -> str:
    cli_name = ('claude' if cli == CLI.CLAUDE_INTERACTIVE else cli.value).capitalize()
    title = global_config.review_title.replace('{cli}', cli_name)
    lines = [f'## {title}', '']

    # Tally of findings posted as inline comments (not shown in summary)
    inline_findings = [f for f in result.findings if id(f) in inline_ids]
    if inline_findings:
        lines.append(_format_inline_tally(inline_findings))
        lines.append('')

    if project_config.show_overview and result.overview:
        lines.extend([_hard_breaks(result.overview), ''])

    if result.tests_passed is not None:
        status = 'passed' if result.tests_passed else 'FAILED'
        lines.append(f'**Tests:** {status}')
        lines.append('')

    # Findings with inline comments appear only inline, not in the summary
    summary_findings = [f for f in result.findings if id(f) not in inline_ids]

    grouped: dict[Severity, list[Finding]] = {}
    for f in summary_findings:
        grouped.setdefault(f.severity, []).append(f)

    for severity in [Severity.CRITICAL, Severity.SUGGESTION, Severity.NITPICK, Severity.GOOD]:
        findings = grouped.get(severity, [])
        if not findings:
            continue
        emoji = SEVERITY_EMOJI[severity]
        lines.append(f'### {emoji} {severity.value.capitalize()} ({len(findings)})')
        lines.append('')
        for finding in findings:
            lines.append(_format_finding_summary(finding))
        lines.append('')

    if result.summary:
        # Label on its own line + blank line so a markdown-list summary renders as a list
        # rather than gluing the first bullet onto the bold label.
        lines.append('**Bottom line:**')
        lines.append('')
        lines.append(_hard_breaks(result.summary))
        lines.append('')

    if approved and result.approve_reason:
        lines.append(f'**Auto-approve rationale:** {_hard_breaks(result.approve_reason)}')
        lines.append('')

    if approve_blocked_reason:
        lines.append(f'**Auto-approve blocked:** AI recommended approval, but {approve_blocked_reason}.')
        lines.append('')

    duration_str = f' in {_format_duration(result.duration_seconds)}' if result.duration_seconds else ''
    footer = global_config.footer.replace('{duration}', duration_str)
    lines.append(f'*{footer}*')
    lines.append('*Replies to this comment are not monitored.*')

    return '\n'.join(lines)


def _sync_critical_task(provider, pr: PRInfo, result: ReviewResult, project_config: ProjectConfig):
    try:
        tasks = provider.list_tasks(pr.repo_slug, pr.pr_id)
        for task in tasks:
            if TASK_MARKER in task.get('content', {}).get('raw', ''):
                provider.delete_task(pr.repo_slug, pr.pr_id, task['id'])
        has_critical = any(f.severity == Severity.CRITICAL for f in result.findings)
        if has_critical:
            message = f'{TASK_MARKER} {project_config.critical_task_message}'
            provider.create_task(pr.repo_slug, pr.pr_id, message)
    except Exception:
        logger.exception('Failed to sync critical task on PR #%d', pr.pr_id)


def _check_auto_approve_gates(
    aa: AutoApproveConfig,
    result: ReviewResult,
    diff_lines: int | None,
) -> str | None:
    """Returns a blocking reason string, or None if auto-approve should proceed."""
    if aa.max_diff_lines is not None and diff_lines is not None and diff_lines > aa.max_diff_lines:
        return f'diff too large ({diff_lines} > {aa.max_diff_lines})'

    if aa.max_findings is not None:
        issue_count = sum(1 for f in result.findings if f.severity != Severity.GOOD)
        if issue_count > aa.max_findings:
            return f'too many findings ({issue_count} > {aa.max_findings})'

    if aa.max_severity is not None:
        max_allowed = SEVERITY_ORDER.get(aa.max_severity, 3)
        for f in result.findings:
            f_order = SEVERITY_ORDER.get(f.severity.value, 3)
            if f_order > max_allowed:
                return f'finding severity {f.severity.value} exceeds max {aa.max_severity}'

    if not result.approve:
        return 'AI did not approve'

    return None


def _resolve_auto_approve(
    aa: AutoApproveConfig,
    result: ReviewResult,
    diff_lines: int | None,
) -> tuple[bool, str | None]:
    """Returns (approved, blocked_reason_to_show).

    blocked_reason_to_show is set only when the AI recommended approval
    but a config gate prevented it and show_blocked_reason is enabled.
    """
    blocked = _check_auto_approve_gates(aa, result, diff_lines)
    if not blocked:
        return True, None

    # AI wanted to approve but a gate stopped it
    show_reason = aa.show_blocked_reason and result.approve and blocked != 'AI did not approve'
    return False, blocked if show_reason else None


_RESOLVED_REPLY = '✅ This looks resolved by the latest changes.'
_UNRESOLVED_REPLY = '⚠️ This has not been addressed yet.'


def _handle_prior_comments(
    provider,
    state_db: StateDB,
    pr: PRInfo,
    result: ReviewResult,
    matched_findings: list[Finding],
    open_ids: set[int],
):
    resolved_notes = {r.comment_id: r.note for r in result.resolved_priors if r.comment_id in open_ids}

    for cid, note in resolved_notes.items():
        try:
            if provider.resolve_comment(pr.repo_slug, pr.pr_id, cid):
                body = f'{_RESOLVED_REPLY} {_hard_breaks(note)}'.strip() if note else _RESOLVED_REPLY
                provider.reply_comment(pr.repo_slug, pr.pr_id, cid, body)
                state_db.mark_comment_resolved(cid)
        except Exception:
            logger.exception('Failed to resolve prior comment %d on PR #%d', cid, pr.pr_id)

    for f in matched_findings:
        if f.prior_id in open_ids and f.prior_id not in resolved_notes:
            try:
                provider.reply_comment(
                    pr.repo_slug, pr.pr_id, f.prior_id, f'{_UNRESOLVED_REPLY} {_hard_breaks(f.issue)}'.strip()
                )
            except Exception:
                logger.exception('Failed to reply to prior comment %d on PR #%d', f.prior_id, pr.pr_id)


def post_review(
    provider: GitProvider,
    state_db: StateDB,
    pr: PRInfo,
    result: ReviewResult,
    project_config: ProjectConfig,
    global_config: GlobalConfig,
    cli: CLI = CLI.CLAUDE,
    dry_run: bool = False,
    diff_lines: int | None = None,
):
    # Deduplicate findings by file + line + title
    seen: set[tuple] = set()
    unique_findings = []
    for f in result.findings:
        key = (f.file, f.line, f.title)
        if key not in seen:
            seen.add(key)
            unique_findings.append(f)
        else:
            logger.debug('Skipping duplicate finding: %s:%s %s', f.file, f.line, f.title)
    # Filter out skipped severities
    skip = {s for s in project_config.skip_severities}
    if skip:
        unique_findings = [f for f in unique_findings if f.severity.value not in skip]
        logger.info('Filtered out %s severities, %d findings remain', skip, len(unique_findings))

    result = ReviewResult(
        overview=result.overview,
        findings=unique_findings,
        summary=result.summary,
        tests_passed=result.tests_passed,
        approve=result.approve,
        approve_reason=result.approve_reason,
        duration_seconds=result.duration_seconds,
        resolved_priors=result.resolved_priors,
    )

    # Findings whose prior_id points at a still-open prior comment map onto that thread
    # (resolve/reply); everything else — new issues and stale/bogus prior_ids — is posted
    # fresh. Providers that can't manage threads fall back to posting everything as new.
    supports_threads = supports_comment_threads(provider)
    if supports_threads:
        open_priors = state_db.get_open_inline_comments(pr.repo_slug, pr.pr_id)
        open_ids = {p['comment_id'] for p in open_priors}
        matched_findings = [f for f in result.findings if f.prior_id in open_ids]
        new_findings = [f for f in result.findings if f.prior_id not in open_ids]
    else:
        open_ids = set()
        matched_findings = []
        new_findings = list(result.findings)

    inline_severities = {s for s in project_config.inline_comments_for}
    inline_findings = [f for f in new_findings if f.severity.value in inline_severities and f.file and f.line]

    max_inline = project_config.max_inline_comments
    if max_inline is not None and len(inline_findings) > max_inline:
        logger.info(
            'Inline comments (%d) exceed max (%d), skipping all inline',
            len(inline_findings),
            max_inline,
        )
        inline_findings = []

    inline_ids = {id(f) for f in inline_findings}

    # The summary lists only new findings; matched ones live in their existing threads.
    summary_result = ReviewResult(
        overview=result.overview,
        findings=new_findings,
        summary=result.summary,
        tests_passed=result.tests_passed,
        approve=result.approve,
        approve_reason=result.approve_reason,
        duration_seconds=result.duration_seconds,
    )

    if dry_run:
        _print_dry_run(
            summary_result,
            inline_findings,
            inline_ids,
            global_config,
            project_config,
            cli,
            diff_lines=diff_lines,
            matched_findings=matched_findings,
            resolved_priors=result.resolved_priors,
        )
        return

    if supports_threads:
        _handle_prior_comments(provider, state_db, pr, result, matched_findings, open_ids)

    logger.info('Posting review: %d inline + summary comment', len(inline_findings))

    for i, finding in enumerate(inline_findings, 1):
        logger.info('Posting inline comment %d/%d: %s:%s', i, len(inline_findings), finding.file, finding.line)
        body = _format_inline_comment(finding)
        try:
            comment_id = provider.post_comment(
                pr.repo_slug,
                pr.pr_id,
                body,
                file_path=finding.file,
                line=finding.line,
                source_commit=pr.source_commit,
            )
            state_db.record_comment(
                pr.repo_slug,
                pr.pr_id,
                comment_id,
                kind='inline',
                file=finding.file,
                line=finding.line,
                title=finding.title,
                issue=finding.issue,
                severity=finding.severity.value,
                source_commit=pr.source_commit,
            )
        except Exception:
            logger.exception('Failed to post inline comment on %s:%s, skipping', finding.file, finding.line)

    aa = project_config.auto_approve
    approved = False
    approve_blocked_reason = None
    if aa.enabled:
        approved, approve_blocked_reason = _resolve_auto_approve(aa, result, diff_lines)
        if not approved:
            logger.info('Auto-approve blocked for PR #%d: %s', pr.pr_id, approve_blocked_reason or 'AI did not approve')

    logger.info('Posting summary comment')
    summary_body = _format_summary_comment(
        summary_result,
        inline_ids,
        global_config,
        project_config,
        cli,
        approved=approved,
        approve_blocked_reason=approve_blocked_reason,
    )
    comment_id = provider.post_comment(pr.repo_slug, pr.pr_id, summary_body)
    state_db.record_comment(pr.repo_slug, pr.pr_id, comment_id, kind='summary', source_commit=pr.source_commit)

    if project_config.critical_task and hasattr(provider, 'list_tasks'):
        _sync_critical_task(provider, pr, result, project_config)

    if approved and provider.approve_pr(pr.repo_slug, pr.pr_id):
        logger.info('Auto-approved PR #%d', pr.pr_id)


def _print_dry_run(
    result: ReviewResult,
    inline_findings: list[Finding],
    inline_ids: set[int],
    global_config: GlobalConfig,
    project_config: ProjectConfig,
    cli: CLI = CLI.CLAUDE,
    diff_lines: int | None = None,
    matched_findings: list[Finding] | None = None,
    resolved_priors: list | None = None,
):
    print('\n' + '=' * 60)
    print('DRY RUN — would post the following comments:')
    print('=' * 60)

    if resolved_priors:
        print(f'\n--- Resolve Prior Comments ({len(resolved_priors)}) ---')
        for r in resolved_priors:
            print(f'  comment {r.comment_id}: resolve + reply ({r.note or "resolved"})')

    if matched_findings:
        print(f'\n--- Reply "not addressed" ({len(matched_findings)}) ---')
        for f in matched_findings:
            print(f'  comment {f.prior_id}: {f.issue}')

    if inline_findings:
        print(f'\n--- Inline Comments ({len(inline_findings)}) ---')
        for f in inline_findings:
            print(f'\n  File: {f.file}:{f.line}')
            print(f'  {_format_inline_comment(f)}')

    aa = project_config.auto_approve
    approved = False
    approve_blocked_reason = None
    if aa.enabled:
        approved, approve_blocked_reason = _resolve_auto_approve(aa, result, diff_lines)
        if not approved:
            print(f'\n--- Auto-Approve: BLOCKED ({approve_blocked_reason or "AI did not approve"}) ---')

    print('\n--- Summary Comment ---')
    print(
        _format_summary_comment(
            result,
            inline_ids,
            global_config,
            project_config,
            cli,
            approved=approved,
            approve_blocked_reason=approve_blocked_reason,
        )
    )

    if aa.enabled and approved:
        print('\n--- Auto-Approve: WOULD APPROVE ---')

    print('=' * 60 + '\n')
