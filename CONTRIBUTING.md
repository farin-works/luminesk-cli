# Contributing to Luminesk-CLI

Thanks for helping improve Luminesk-CLI.

## Before you start

1. Search existing issues and pull requests to avoid duplicate work.
2. If your change affects user-visible behavior, update documentation in `/docs/docs/` in the same pull request.
3. Keep changes focused and easy to review.

## Local setup

```bash
git clone https://github.com/task-v1/luminesk-cli
cd luminesk-cli
uv sync --locked --extra dev
```

## Required checks

Run these before opening or updating a pull request:

```bash
uv run ruff check .
uv run ty check .
uv run pytest
```

If your change touches docs site code or content, also run:

```bash
pnpm --dir docs install --frozen-lockfile
pnpm --dir docs typecheck
pnpm --dir docs build
```

## Pull request expectations

- Use a clear title and describe the intent of the change.
- Include a short summary of what changed and why.
- List validation commands and their results.
- Update docs, examples, and troubleshooting entries when behavior changes.
- Do not include unrelated refactors in the same pull request.

## Commit guidance

- Prefer small, logical commits.
- Write commit messages in imperative mood (for example: `Add docs validation job`).

## Reporting bugs and requesting features

Use GitHub issue templates:

- Bug Report
- Feature Request

Blank issues are disabled to keep reports structured and actionable.
