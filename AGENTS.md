# Agent Instructions

## Agent skills

### Issue tracker

Issues and PRDs are tracked as local markdown files under `.scratch/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Triage uses the default five-label vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, and `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context project with root `CONTEXT.md` and root `docs/adr/` for architectural decisions. See `docs/agents/domain.md`.

## Repository safety

Do not run any `git` command unless the user explicitly allows it in the current conversation. This includes read-only commands such as `git status`, staging, committing, and pushing.

## Recoverable cleanup

During local builds and migration work, do not delete obsolete or replaceable files. Move them into the repository-root `.trash/` directory instead so they remain recoverable. Keep `.trash/` ignored by Git, preserve relative paths where practical, and report what was moved there. Never move source-of-truth data or secrets there without explicit user approval.
