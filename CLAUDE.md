# CLAUDE.md — reviewd

Local AI code review daemon for GitHub/BitBucket PRs. See README.md for full docs.

## Project Conventions

- Python 3.12+, no backward compatibility
- Dependencies managed with `uv`
- Google style, single quotes for strings, double quotes for messages
- No broad except clauses
- No unnecessary docstrings or comments
- Tests only when explicitly asked
- Never add Co-Authored-By to commits
- Only commit or release when explicitly asked

## Key Files

| File | Purpose |
|------|---------|
| `src/reviewd/cli.py` | Click CLI: `ls`, `watch`, `pr`, `status` commands |
| `src/reviewd/daemon.py` | Poll loop, boot summary, status line, orchestration, signal handling |
| `src/reviewd/reviewer.py` | Worktree lifecycle + AI CLI invocation (Popen) + JSON extraction |
| `src/reviewd/prompt.py` | Built-in review prompt template + builder |
| `src/reviewd/commenter.py` | Format findings as markdown, post via provider, resolve/reply on prior comments (BitBucket) |
| `src/reviewd/config.py` | YAML + `${ENV_VAR}` loading, global + per-project merge, provider factory |
| `src/reviewd/state.py` | SQLite: reviews + posted_comments (stores finding metadata + resolved flag; additive column migration) |
| `src/reviewd/models.py` | Dataclasses: PRInfo, Finding, ReviewResult, configs, CLI enum |
| `src/reviewd/providers/base.py` | Abstract GitProvider ABC |
| `src/reviewd/providers/bitbucket.py` | BitBucket 2.0 API (httpx, pagination with ID dedup, inline comments) |
| `src/reviewd/providers/github.py` | GitHub REST API v3 (httpx, Link header pagination, review comments) |

## Releasing

Private fork — **never published to PyPI**, and no GitHub Releases (`gh` CLI is not available).
A release is a version bump, a commit, and a tag.

Versions carry a local suffix — `0.7.2+acsl.N`. Bump `N`; the tag is `v<version>`.

```bash
# 1. Bump version in pyproject.toml, e.g. 0.7.2+acsl.6 -> 0.7.2+acsl.7
#    Commit the bump together with the change it releases, ending the
#    message with "Bump version to X.Y.Z+acsl.N."
git add pyproject.toml <changed files> && git commit -F - && git push origin main
# 2. Tag and push the tag
git tag -a 'v0.7.2+acsl.7' -m 'v0.7.2+acsl.7' && git push origin 'v0.7.2+acsl.7'
```

None of this reaches CI on its own. The TeamCity PR-review builds run the
`482650546777.dkr.ecr.us-east-1.amazonaws.com/acsl/reviewd:latest` image, built outside this repo;
a change only takes effect in CI once that image is rebuilt and pushed to ECR.

## Known Limitations

- BitBucket markdown doesn't support HTML comments — bot marker uses empty link `[](reviewd)`
- Can't `git fetch` by commit hash from BB — if source branch is deleted, commit must exist locally
- Claude CLI rejects nested sessions — must unset `CLAUDECODE` env var in subprocess
- Gemini CLI loads global extensions by default — use `-e none` to disable
- Codex CLI outputs raw JSON (no markdown fences) — extract_json falls back to raw JSON object parsing
- Inline suggestions are single-line only (TODO: multi-line support)
- AI may hallucinate line numbers — prompt instructs to double-check but not guaranteed
- `cli: claude_interactive` drives an *interactive* Claude session over a pexpect PTY (no `--print`, no tmux), to bill against the subscription instead of API credits. The prompt tells Claude to write the review JSON to a temp file via `Write` + atomic `mv`; reviewd waits for that file (watching the PTY stream for usage/login errors + an overall timeout). Requires `claude` logged in via `/login`; if `ANTHROPIC_API_KEY` is exported it may still be used. Runs with `--dangerously-skip-permissions` so Write/Bash run unattended in the worktree.
