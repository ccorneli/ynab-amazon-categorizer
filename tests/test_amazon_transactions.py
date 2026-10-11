"""Tests for parsing the Amazon transactions page and matching exact charges."""

import pytest

from ynab_amazon_categorizer.amazon_transactions import (
    AmazonCharge,
    orders_from_charges,
    parse_transactions_page,
)
from ynab_amazon_categorizer.models import Order
from ynab_amazon_categorizer.transaction_matcher import TransactionMatcher

# Layout copied from the real page; order numbers and amounts are made up.
TRANSACTIONS_PAGE = """
Transactions
Completed
Oct 06, 2026 · AMZN Mktp US
Prime Visa••••0000
Order #111-1000001-0000001
-$19.14
Charged
Oct 02, 2026 · AMZN Mktp US
Prime Visa••••0000
Order #111-1000002-0000002
-$32.32
Charged
Oct 02, 2026 · AMAZON MKTPLACE PMTS
Amazon Gift Card
Order #111-1000002-0000002
-$5.00
Charged
Sep 29, 2026 · Amazon.com
Prime Visa••••0000
Order #111-1000003-0000003
-$4.19
Charged
Sep 29, 2026 · Amazon.com
Prime Visa••••0000
Order #111-1000003-0000003
-$6.42
Charged
Sep 25, 2026 · AMZN Mktp US
Prime Visa••••0000
Order #111-1000004-0000004
+$1,042.64
Refunded
"""


def test_parses_each_charge() -> None:
    charges = parse_transactions_page(TRANSACTIONS_PAGE)

    assert [(c.order_id, c.amount) for c in charges] == [
        ("111-1000001-0000001", -19.14),
        ("111-1000002-0000002", -32.32),
        ("111-1000002-0000002", -5.00),
        ("111-1000003-0000003", -4.19),
        ("111-1000003-0000003", -6.42),
        ("111-1000004-0000004", 1042.64),
    ]
    assert charges[0].date_str == "October 6, 2026"
    assert charges[0].currency == "$"


def test_flags_gift_cards_and_refunds() -> None:
    charges = parse_transactions_page(TRANSACTIONS_PAGE)

    assert [c.is_gift_card for c in charges] == [
        False,
        False,
        True,
        False,
        False,
        False,
    ]
    assert [c.is_refund for c in charges] == [False] * 5 + [True]


def test_parses_markdown_link_copy() -> None:
    """Copying the page as markdown runs each entry together on one line."""
    text = (
        "[Oct 02, 2026·AMZN Mktp USPrime Visa••••0000Order #111-1000002-0000002"
        "-$32.32](https://www.amazon.com/cpe/yourpayments/transactions#)\n"
        "[Charged](https://www.amazon.com/cpe/yourpayments/transactions#)\n"
        "[Sept 2, 2026·AMAZON MKTPLACE PMTSAmazon Gift CardOrder "
        "#111-1000002-0000002](https://www.amazon.com/cpe/yourpayments/transactions#)\n"
        "-5.00\n"
    )

    charges = parse_transactions_page(text)

    assert charges == [
        AmazonCharge("111-1000002-0000002", "October 2, 2026", -32.32, "$", False),
        AmazonCharge(
            "111-1000002-0000002", "September 2, 2026", -5.00, None, is_gift_card=True
        ),
    ]


def test_entry_without_order_number_is_ignored() -> None:
    text = """
Oct 03, 2026 · Prime Membership
Prime Visa••••0000
-$14.99
Charged
Oct 02, 2026 · AMZN Mktp US
Prime Visa••••0000
Order #111-1000002-0000002
-$32.32
Charged
"""
    charges = parse_transactions_page(text)

    assert [(c.order_id, c.amount) for c in charges] == [
        ("111-1000002-0000002", -32.32),
    ]
    assert charges[0].date_str == "October 2, 2026"


def test_no_charges_in_unrelated_text() -> None:
    assert parse_transactions_page("Your Orders\nBuy it again") == []


def _orders() -> list[Order]:
    return [
        Order("111-1000001-0000001", 19.14, "October 4, 2026", ["Widget"], "$"),
        Order("111-1000002-0000002", 32.32, "October 1, 2026", ["Collar"], "$"),
        Order("111-1000003-0000003", 4.19, "September 27, 2026", ["Defoamer"], "$"),
        Order("111-1000009-0000009", 12.00, "August 1, 2026", ["Old order"], "$"),
    ]


def test_orders_from_charges_uses_charge_amounts() -> None:
    charges = parse_transactions_page(TRANSACTIONS_PAGE)

    orders = orders_from_charges(_orders(), charges)

    by_amount = {o.total: o for o in orders if o.total is not None}
    assert sorted(by_amount) == [4.19, 6.42, 12.00, 19.14, 32.32, 1042.64]
    split = [o for o in orders if o.order_id == "111-1000003-0000003"]
    assert [o.total for o in split] == [4.19, 6.42]
    assert all(o.items == ["Defoamer"] for o in split)
    assert len({o.charge_id for o in split}) == 2
    assert by_amount[32.32].date_str == "October 2, 2026"
    assert by_amount[1042.64].refund is True
    assert by_amount[1042.64].items == []
    assert by_amount[12.00].charge_id is None


def test_orders_from_charges_drops_gift_card_payments() -> None:
    orders = orders_from_charges(_orders(), parse_transactions_page(TRANSACTIONS_PAGE))

    assert 5.00 not in {o.total for o in orders}


def test_orders_from_charges_does_not_share_item_lists() -> None:
    base = _orders()
    orders = orders_from_charges(base, parse_transactions_page(TRANSACTIONS_PAGE))

    for order in orders:
        if order.charge_id:
            order.items.append("changed")

    assert all("changed" not in o.items for o in base)


@pytest.fixture
def charge_orders() -> list[Order]:
    return orders_from_charges(_orders(), parse_transactions_page(TRANSACTIONS_PAGE))


def test_matches_one_shipment_of_a_split_order(charge_orders: list[Order]) -> None:
    match = TransactionMatcher().find_matching_order(-6.42, "2026-10-01", charge_orders)

    assert match is not None
    assert match.order_id == "111-1000003-0000003"
    assert match.items == ["Defoamer"]


def test_used_charge_does_not_block_other_charge_of_same_order(
    charge_orders: list[Order],
) -> None:
    matcher = TransactionMatcher()
    first = matcher.find_matching_order(-4.19, "2026-10-01", charge_orders)
    assert first is not None and first.charge_id is not None

    second = matcher.find_matching_order(
        -6.42, "2026-10-01", charge_orders, used_order_ids={first.charge_id}
    )

    assert second is not None
    assert second.charge_id != first.charge_id


def test_used_charge_is_not_matched_again(charge_orders: list[Order]) -> None:
    matcher = TransactionMatcher()
    first = matcher.find_matching_order(-4.19, "2026-10-01", charge_orders)
    assert first is not None and first.charge_id is not None

    assert (
        matcher.find_matching_order(
            -4.19, "2026-10-01", charge_orders, used_order_ids={first.charge_id}
        )
        is None
    )


def test_refund_matches_inflow_only(charge_orders: list[Order]) -> None:
    matcher = TransactionMatcher()

    assert matcher.find_matching_order(-1042.64, "2026-09-27", charge_orders) is None
    refund = matcher.find_matching_order(1042.64, "2026-09-27", charge_orders)
    assert refund is not None
    assert refund.order_id == "111-1000004-0000004"


def test_charge_does_not_match_inflow(charge_orders: list[Order]) -> None:
    assert (
        TransactionMatcher().find_matching_order(32.32, "2026-10-04", charge_orders)
        is None
    )


def test_order_total_still_matches_either_direction() -> None:
    """Without a transactions paste, matching works as before."""
    orders = _orders()

    assert TransactionMatcher().find_matching_order(12.00, "2026-08-02", orders)
    assert TransactionMatcher().find_matching_order(-12.00, "2026-08-02", orders)
