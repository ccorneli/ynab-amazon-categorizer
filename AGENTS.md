# AGENTS.md

Repository instructions for coding agents working on this project.
`CLAUDE.md` may point here for compatibility.

## Project Overview

This is a Python CLI tool that matches Amazon orders to YNAB transactions with item-level memo enrichment and guided categorization.

## Setup and Validation Commands

```bash
# Install project + development dependencies
uv sync --extra dev

# Run tests
python -X utf8 -m pytest tests/ -v

# Run tests with coverage
python -X utf8 -m pytest tests/ --cov=src --cov-report=html

# Type checking (portable, repo-local)
uv run --extra dev ty check src tests

# Architecture / drift boundary checks
uv run --extra dev tach check

# Formatting / lint
uv run --extra dev ruff format src tests
uv run --extra dev ruff check src tests --fix

# Pre-commit hooks (all stages: lint, types, tach, vibedrift, typos, gitleaks, pytest)
uv run --extra dev pre-commit run --all-files

# Run locally
python -X utf8 -m ynab_amazon_categorizer
python -X utf8 src/ynab_amazon_categorizer/cli.py
```

## Architecture

Current modules:
- `src/ynab_amazon_categorizer/cli.py` - main CLI entry point and interactive flow.
- `src/ynab_amazon_categorizer/amazon_parser.py` - Amazon order parsing logic.
- `src/ynab_amazon_categorizer/transaction_matcher.py` - amount/date matching logic.
- `src/ynab_amazon_categorizer/memo_generator.py` - memo and order-link generation.
- `src/ynab_amazon_categorizer/ynab_client.py` - YNAB API communication.
- `src/ynab_amazon_categorizer/config.py` - environment config loading/validation.
- `src/ynab_amazon_categorizer/setup_wizard.py` - interactive `.env` creation (`--setup` / first run).
- `src/ynab_amazon_categorizer/models.py` - typed domain and YNAB payload models.
- `src/ynab_amazon_categorizer/payloads.py` - minimal YNAB update construction.
- `src/ynab_amazon_categorizer/transactions.py` - transaction validation and Amazon-payee filtering.
- `src/ynab_amazon_categorizer/batch.py` - non-interactive matching and memo enrichment policy.

Design principles:
- Keep modules single-purpose and composable.
- Keep `cli.py` as orchestration; move business logic into focused modules.
- Prefer typed interfaces and predictable return values for API/parsing layers.
- Add tests for behavior changes before refactoring or extending logic.

Data flow:
1. User copies Amazon orders page text.
2. Parser extracts orders and item details.
3. Tool fetches uncategorized YNAB transactions.
4. Matcher pairs orders with transactions using amount/date heuristics.
5. CLI guides category updates and split transactions.
6. Tool updates YNAB memos/categories via API.

Matching and memo behavior:
- Transaction matching prioritizes amount match with date proximity heuristics.
- Memo generation should include item context and an order link when available.
- Missing/partial order data should degrade gracefully rather than crash updates.

## Configuration Requirements

Create a `.env` with:

```env
YNAB_API_KEY=your_api_key_here
YNAB_BUDGET_ID=your_budget_id_here
YNAB_ACCOUNT_ID=none
AMAZON_DOMAIN=amazon.ca
```

`YNAB_ACCOUNT_ID` is optional (`none` means all accounts). `AMAZON_DOMAIN` is
optional (default `amazon.ca`) and only affects memo order links.
`ynab-amazon-categorizer --setup` (or the first-run prompt) creates this file.

## Dependencies

- `requests` - YNAB API calls
- `prompt_toolkit` - interactive CLI UX
- `python-dotenv` - `.env` loading

## Security Notes

- Never commit real API keys or `.env` contents.
- Do not print full secrets in logs, tests, or screenshots.
- When sharing examples, use placeholder credentials.

## Agent Notes

- On Windows, prefer `python -X utf8` to avoid emoji/category encoding issues.
- Focus processing on likely Amazon payees (`amazon`, `amzn`, `amz`).
- Add or update tests when behavior changes (parser, matcher, memo generation, API payloads).
