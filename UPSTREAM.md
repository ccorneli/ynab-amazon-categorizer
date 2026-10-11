# Contributing this fork back upstream

This repository is a fork of
[dizzlkheinz/ynab-amazon-categorizer](https://github.com/dizzlkheinz/ynab-amazon-categorizer).
The plan is to use the fork for a few weeks and, if it holds up, offer the
changes back upstream in small pieces.

## Where the fork stands

- Forked from upstream `main` at `7b88a9b` (2026-09-04). Upstream had no newer
  commits as of 2026-10-11, so the fork was a clean fast-forward.
- Changes as of PR #7: 22 files, +2,699 / −98 lines.
  - Source: +1,177 / −83. New modules `setup_wizard.py`, `amazon_transactions.py`,
    `amazon_invoice.py` and `run_summary.py`; `cli.py` changed by about 400 lines.
  - Tests: +1,384 / −3, in 4 new test files plus additions to existing ones.
  - Docs and config: +138 / −12 (README, CHANGELOG, AGENTS.md, `.env.example`, `tach.toml`).

Recompute with:

```bash
git remote add upstream https://github.com/dizzlkheinz/ynab-amazon-categorizer.git
git fetch upstream
git diff --stat upstream/main origin/main
```

## Features added

| Feature | Fork PRs |
|---|---|
| Interactive `.env` setup (`--setup` and a first-run offer): validates the API key, picks the budget and account by list number, YNAB URL or ID, confirms the Amazon domain, and writes `.env` privately and atomically while keeping existing content | #1, #2 |
| Paste prompt shows the submit keys for the current OS (Esc then Return on macOS) | #3 |
| Optional Amazon Transactions page paste: matches each YNAB charge to the exact card charge (split shipments, gift-card orders, refunds) | #4 |
| Parser no longer reads "Your delivery…" or "Your return…" lines as items | #4 |
| Split amounts from the pasted order summary (invoice) page: item price plus its share of tax, shipping and discounts, adding up exactly to the charge | #5 / #6 |
| Paste boxes open directly with the page URL underneath (empty paste skips); split defaults to yes for multi-item orders | #7 |
| End-of-run summary of each transaction, including in dry runs | #7 |
| Tax-rate settings documented; offers to save the rate to `.env` when two pasted orders agree | #7 |

## Caveats to raise upstream

- The new parsers (Transactions page, order summary page) were tested only on
  amazon.com pages, and upstream's default is amazon.ca. The URLs and layouts on
  other Amazon domains are unverified.
- Some behaviour changes for existing users: the y/n questions before each
  paste are gone (Ctrl+C at the orders paste now skips instead of quitting),
  split defaults to yes for multi-item orders, and running without a `.env`
  offers setup.
- Ruff here extends `../githooks/ruff.toml` (upstream's shared config), which
  is not public. Run upstream's checks by hand before submitting (see below).
- Test fixtures must stay anonymised: no real names, addresses, card digits or
  order numbers.

## Suggested approach

1. Open an issue upstream first. Describe the features above and ask whether
   they're wanted and how the maintainer would like them split. One
   2,700-line PR would be hard to review.
2. Send small, independent PRs. Branch each from `upstream/main` and give it
   its own tests, README and CHANGELOG entries. Suggested order, smallest and
   least controversial first:
   1. Parser fix for "Your delivery…" lines, plus the OS-specific submit hint
      (#3 and the parser part of #4).
   2. Setup wizard (#1, #2): `setup_wizard.py`, its tests, and `--setup` in `cli.py`.
   3. Transactions-page matching (rest of #4): `amazon_transactions.py`, its
      tests, and the matcher changes.
   4. Order summary split amounts and the direct paste prompts (#6 and part of
      #7): `amazon_invoice.py` and the split flow in `cli.py`.
   5. End-of-run summary and the tax-rate offer (rest of #7): `run_summary.py`
      and `save_env_value`.
3. Ask the maintainer to try pieces 3 and 4 on amazon.ca, or to share
   anonymised sample pages so the parsers can be checked against that layout.
4. Before each PR, check whether upstream has moved (`git fetch upstream`) and
   rebase onto it.
5. Run the checks in [Running upstream's checks by hand](#running-upstreams-checks-by-hand)
   on each PR branch.

## Running upstream's checks by hand

Upstream's pre-commit hook comes from its `githooks` repo, which isn't public
(both `dizzlkheinz/githooks` and `ccorneli/githooks` return 404). Each check
can still be run on its own from the repository root. `ty`, `tach` and
`pytest` need nothing extra (see `AGENTS.md`).

### ruff

`pyproject.toml` extends `../githooks/ruff.toml`, so ruff won't start without
it. As a stand-in, build that file from upstream's last full ruff settings
(commit `7b88a9b^`, just before upstream moved them out). It's close to the
shared config, but not guaranteed identical.

```bash
mkdir ../githooks   # stop if it already exists
git show '7b88a9b^:pyproject.toml' \
  | sed -n '/^\[tool\.ruff\]/,/^\[tool\.typos/p' | sed '$d' \
  | sed '/^\[tool\.ruff\]$/d; s/^\[tool\.ruff\.lint/[lint/' \
  | grep -v 'target-version\|extend-exclude\|"DTZ007"' > ../githooks/ruff.toml
uv run --extra dev ruff format --check src tests
uv run --extra dev ruff check src tests
rm -r ../githooks
```

### typos

```bash
uvx typos
```

### gitleaks

Install the binary from <https://github.com/gitleaks/gitleaks/releases>
(`brew install gitleaks` on macOS), then:

```bash
gitleaks git --redact .   # full history
gitleaks dir --redact .   # working tree
```

`.env` is in `.gitignore`. Check that it has never been committed with
`git log --all -- .env`, which should print nothing.

### vibedrift

Needs Node.js. The hook fails below a score of 90.

```bash
npx --yes @vibedrift/cli scan . --format terminal --local-only --fail-on-score 90
```

### Telling new findings from old ones

Run the same commands on upstream's base in a separate worktree. For ruff,
create the stand-in file next to the worktree, here `/tmp/githooks/ruff.toml`.

```bash
git worktree add /tmp/upstream-base upstream/main
cd /tmp/upstream-base   # run the checks here
git worktree remove --force /tmp/upstream-base
```

### Last run (2026-10-11, fork `main` at `3af7430`)

| Check | Fork | Upstream `7b88a9b` |
|---|---|---|
| ruff format | clean | clean |
| ruff check | 67 findings | clean |
| typos | 4 (2 already upstream, 2 false positives) | 2 |
| gitleaks | 2 fake test keys (1 already upstream) | 1 |
| vibedrift | 95.7/100, passes | 95.5/100 |

The ruff findings, all in code added by the fork:

- 50 missing docstrings (D100–D103), mostly test functions.
- 14 tests that call private `cli._…` helpers (SLF001).
- One each: a Unicode minus sign "−" in `amazon_transactions.py` (RUF001),
  intentional because Amazon pages use that character; an unused `noqa` in
  `setup_wizard.py` (RUF100); a docstring that isn't in the imperative (D401);
  and an import that belongs under `TYPE_CHECKING` (TC001).
- Two combined `assert a and b` checks in `test_amazon_transactions.py` (PT018).

Likely false positives. Leave these flagged and explain them in the PR rather
than suppressing them (no `noqa`, allowlist entries or changed fixtures), so the
maintainer can decide:

- typos: "unparseable" (already flagged in upstream's own code) and "HSA" in
  the FSA/HSA invoice fixtures. Upstream's old config allowed "unparseable"
  via `[tool.typos.default] extend-words`. This file quotes both words, so
  typos flags it too.
- ruff RUF001: the "−" minus sign above.
- gitleaks: `API_KEY = "secret…"` in `tests/test_setup_wizard.py` and the
  key in `tests/test_cli_functions.py` (already upstream) are fake. vibedrift's
  "hardcoded API key" warning is the same test key.
