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
  was not available when these changes were written. Run the full upstream
  hooks (ruff, typos, gitleaks, vibedrift) before submitting.
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
