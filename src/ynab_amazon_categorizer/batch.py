"""Non-interactive memo enrichment policy."""

import logging
from collections.abc import Mapping, Sequence
from typing import Any

import requests

from .exceptions import YNABAPIError
from .memo_generator import MemoGenerator, build_batch_memo
from .models import Order, TransactionUpdate, format_currency_amount, order_match_key
from .payloads import build_memo_only_payload
from .transaction_matcher import TransactionMatcher
from .ynab_client import YNABClient

logger = logging.getLogger(__name__)


def _resolve_batch_memo_candidate(
    transaction: Mapping[str, Any],
    order: Order,
    memo_generator: MemoGenerator,
) -> str | None:
    """Return enriched memo if valid and changed, or None if skipped."""
    original_memo = transaction.get("memo")
    existing_memo = original_memo if isinstance(original_memo, str) else ""
    memo = build_batch_memo(order, memo_generator, existing_memo)
    if memo is None:
        logger.info(
            "Skipping transaction %s because enrichment would truncate its memo.",
            transaction["id"],
        )
        return None
    if memo == existing_memo:
        logger.info(
            "Skipping transaction %s because its memo is already enriched.",
            transaction["id"],
        )
        return None
    return memo


def _apply_batch_enrichment(
    transaction: Mapping[str, Any],
    payload: TransactionUpdate,
    payee: str,
    amount_display: str,
    summary: str,
    ynab_client: YNABClient,
    dry_run: bool,
) -> bool:
    """Send memo update to YNAB (or simulate for dry-run). Returns True on success."""
    if dry_run:
        print(f"  [dry-run] would enrich {payee} {amount_display}: {summary}")
        return True
    try:
        ynab_client.update_transaction(transaction["id"], payload)
        print(f"  ✓ Enriched {payee} {amount_display}: {summary}")
    except (YNABAPIError, requests.exceptions.RequestException, OSError) as exc:
        logger.error("Failed to enrich transaction %s: %s", transaction["id"], exc)
        print(f"  ✗ Failed to enrich {payee} {amount_display}: {exc}")
        return False
    else:
        return True


def _enrich_one(
    transaction: Mapping[str, Any],
    matcher: TransactionMatcher,
    parsed_orders: list[Order] | None,
    used_order_ids: set[str],
    memo_generator: MemoGenerator,
    ynab_client: YNABClient,
    dry_run: bool,
) -> str:
    """Enrich one transaction, returning "enriched", "skipped", or "failed"."""
    amount_float = transaction["amount"] / 1000.0
    order = matcher.find_confident_match(
        amount_float,
        transaction["date"],
        parsed_orders or [],
        used_order_ids,
    )
    if order is None:
        return "skipped"

    # A confident match belongs to this transaction even when enrichment is
    # unnecessary or impossible. Do not let a later same-amount transaction
    # reuse the order merely because this transaction does not get updated.
    key = order_match_key(order)
    if key:
        used_order_ids.add(key)

    memo = _resolve_batch_memo_candidate(transaction, order, memo_generator)
    if memo is None:
        return "skipped"

    payload = build_memo_only_payload(memo, bool(transaction.get("approved", False)))
    payee = transaction.get("payee_name", "N/A")
    summary = memo.splitlines()[0] if memo else ""
    amount_display = format_currency_amount(amount_float, order.currency)

    if _apply_batch_enrichment(
        transaction,
        payload,
        payee,
        amount_display,
        summary,
        ynab_client,
        dry_run,
    ):
        return "enriched"
    return "failed"


def process_batch(
    transactions: Sequence[Mapping[str, Any]],
    parsed_orders: list[Order] | None,
    memo_generator: MemoGenerator,
    ynab_client: YNABClient,
    dry_run: bool = False,
) -> tuple[int, int, int]:
    """Auto-enrich confidently matched memos without changing categories."""
    matcher = TransactionMatcher()
    used_order_ids: set[str] = set()
    counts = {"enriched": 0, "skipped": 0, "failed": 0}

    for transaction in transactions:
        outcome = _enrich_one(
            transaction,
            matcher,
            parsed_orders,
            used_order_ids,
            memo_generator,
            ynab_client,
            dry_run,
        )
        counts[outcome] += 1

    return counts["enriched"], counts["skipped"], counts["failed"]
