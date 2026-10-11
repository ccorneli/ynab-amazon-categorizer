"""Transaction matching functionality."""

from collections.abc import Sequence
from datetime import datetime

from .amazon_parser import Order
from .models import order_match_key


def _parse_transaction_date(date_str: str) -> datetime | None:
    """Parse transaction date in YYYY-MM-DD format."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _parse_order_date(date_str: str | None) -> datetime | None:
    """Parse order date in 'Month DD, YYYY' format."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%B %d, %Y")
    except (ValueError, TypeError):
        return None


SAME_OR_NEXT_DAY = 1
WITHIN_A_FEW_DAYS = 3
WITHIN_A_WEEK = 7
AMOUNT_MATCH_TOLERANCE = 0.01


def _date_proximity_bonus(date_diff: int) -> int:
    """Score bonus for how close the order date is to the transaction date."""
    if date_diff <= SAME_OR_NEXT_DAY:
        return 30
    if date_diff <= WITHIN_A_FEW_DAYS:
        return 15
    if date_diff <= WITHIN_A_WEEK:
        return 5
    return 0


def _calculate_proximity_score(
    trans_date: datetime | None,
    order_date_str: str | None,
    max_date_diff_days: int,
) -> tuple[int, int | None] | None:
    """Calculate date proximity score and date diff, or None if outside window."""
    order_date = _parse_order_date(order_date_str) if trans_date else None
    if trans_date is None or order_date is None:
        return 100, None

    date_diff = abs((trans_date - order_date).days)
    if date_diff > max_date_diff_days:
        return None
    return 100 + _date_proximity_bonus(date_diff), date_diff


def _is_better_candidate(
    score: int,
    date_diff: int | None,
    order_id: str,
    best_score: int,
    best_date_diff: int | None,
    best_order_id: str,
) -> bool:
    """Deterministic tie-breaking: score > date_diff (lower wins) > order_id."""
    if score > best_score:
        return True
    if score == best_score:
        if date_diff is not None and (
            best_date_diff is None or date_diff < best_date_diff
        ):
            return True
        if date_diff == best_date_diff and order_id < best_order_id:
            return True
    return False


def _is_amount_candidate(
    order: Order,
    transaction_amount: float,
    used_order_ids: set[str] | None,
) -> bool:
    """Check if an order matches the transaction amount and has not been used.

    Charges from the transactions page also have to match direction: a refund
    only matches an inflow, and a charge only an outflow.
    """
    if order.total is None:
        return False
    key = order_match_key(order)
    if used_order_ids and key is not None and key in used_order_ids:
        return False
    if order.refund is not None and order.refund != (transaction_amount > 0):
        return False
    return abs(order.total - abs(transaction_amount)) < AMOUNT_MATCH_TOLERANCE


def _is_within_date_window(
    trans_date: datetime | None,
    order_date_str: str | None,
    max_date_diff_days: int,
) -> bool:
    """Return whether order date is within max_date_diff_days when dates are parseable."""
    if not trans_date:
        return True
    order_date = _parse_order_date(order_date_str)
    if not order_date:
        return True
    return abs((trans_date - order_date).days) <= max_date_diff_days


class TransactionMatcher:
    """Matches Amazon orders with YNAB transactions."""

    def __init__(self) -> None:
        pass

    def find_matching_order(
        self,
        transaction_amount: float,
        transaction_date: str,
        parsed_orders: Sequence[Order],
        used_order_ids: set[str] | None = None,
        max_date_diff_days: int = 14,
    ) -> Order | None:
        """Find the best matching order for a transaction.

        Matching requires an exact amount match (within 1 cent) and, when both
        dates are parseable, a date within ``max_date_diff_days``.
        Ties are broken by date proximity, then by order ID for determinism.

        Orders whose ``order_id`` appears in ``used_order_ids`` are skipped so a
        single order is not matched to multiple transactions of the same amount.
        """
        if not parsed_orders:
            return None

        trans_date = _parse_transaction_date(transaction_date)

        best_match: Order | None = None
        best_score = 0
        best_date_diff: int | None = None
        best_order_id: str = ""

        for order in parsed_orders:
            if not _is_amount_candidate(order, transaction_amount, used_order_ids):
                continue

            match_result = _calculate_proximity_score(
                trans_date,
                order.date_str,
                max_date_diff_days,
            )
            if match_result is None:
                continue
            score, date_diff = match_result
            order_id = order.order_id or ""

            if _is_better_candidate(
                score,
                date_diff,
                order_id,
                best_score,
                best_date_diff,
                best_order_id,
            ):
                best_score = score
                best_match = order
                best_date_diff = date_diff
                best_order_id = order_id

        return best_match

    def find_confident_match(
        self,
        transaction_amount: float,
        transaction_date: str,
        parsed_orders: Sequence[Order],
        used_order_ids: set[str] | None = None,
        max_date_diff_days: int = 7,
    ) -> Order | None:
        """Return an order only when the match is unambiguous (for batch use).

        Unlike ``find_matching_order``, this requires *exactly one* unused order
        matching the amount (within 1 cent). If that order has a parseable date
        it must be within ``max_date_diff_days`` of the transaction. Any
        ambiguity (zero or multiple amount matches, or a far-off date) returns
        ``None`` so batch mode never auto-applies a guess.
        """
        trans_date = _parse_transaction_date(transaction_date)

        candidates = [
            order
            for order in parsed_orders
            if _is_amount_candidate(order, transaction_amount, used_order_ids)
        ]

        if len(candidates) != 1:
            return None

        order = candidates[0]
        if not _is_within_date_window(trans_date, order.date_str, max_date_diff_days):
            return None
        return order
