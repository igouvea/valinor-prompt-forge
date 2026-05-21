# Benchmark: git-log-summary

A small Node.js CLI that summarizes `git log` output. Used as a one-shot
benchmark for Valinor's prompt-optimization loop against local models.

## Goal

Build `bin/git-summary.js` — a Node CLI that reads git log output (either piped
on stdin or read from a file via `--input <path>`) and prints a deterministic
plain-text summary:

```
Total commits: <N>
Authors:
  <Author Name>: <count>
  <Author Name>: <count>
Most recent:
  <short_hash> by <author> — <subject>
```

Authors are listed in descending count order; ties are broken by author name
ascending. The "Most recent" line uses the first commit in the input (git log's
default reverse-chronological order).

## Acceptance

`npm test` must pass with at least 4 deterministic vitest tests:

1. Parses a 3-commit fixture, prints exact expected summary
2. Handles a single-commit input
3. Tied author counts are sorted alphabetically
4. Empty input prints `Total commits: 0` with no authors / most-recent

## Input format

Standard `git log` output with default formatting:

```
commit 8a2b1c3d4e5f6789...
Author: Jane Doe <jane@example.com>
Date:   Mon May 19 10:30:00 2026 +0000

    Add login endpoint

commit ...
```

## Out of scope

- Streaming / very large logs (assume input fits in memory)
- `git log --pretty` custom formats
- Network access
- Cross-platform path handling (POSIX is fine)

## Files the agent should produce

- `bin/git-summary.js` — the CLI (shebang + Node code)
- `src/summarize.js` — pure function `summarize(logText) → { totalCommits, authors, mostRecent }`
- `tests/summarize.test.js` — vitest tests against `src/summarize.js`
- `tests/fixtures/three-commits.log`, `tests/fixtures/single-commit.log`,
  `tests/fixtures/tied-authors.log` — fixture inputs (provided below)

The fixtures are pre-provided in `tests/fixtures/` so the agent has stable
inputs to write tests against.
