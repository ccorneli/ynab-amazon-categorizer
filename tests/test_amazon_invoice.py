"""Tests for parsing the Amazon order summary page and allocating amounts."""

from ynab_amazon_categorizer.amazon_invoice import (
    InvoiceItem,
    OrderSummary,
    allocate_amount,
    effective_tax_rate,
    parse_order_summary,
)

# Layout copied from the real page; name, address and order number are made up.
ORDER_SUMMARY = """
Order Summary
Order placed
October 4, 2026
Order #
111-2000001-0000001
Print
Ship to
- Jane Doe
- 1 Example St
Anytown, ST 00000
- United States
Payment method
Prime Visa
••••
0000
Order Summary
- Item(s) Subtotal:
$100.82
- Shipping & Handling:
$0.00
- Total before tax:
$100.82
- Estimated tax to be collected:
$6.68
- Grand Total:
$107.50
Delivered October 6
Your package was left near the front door or porch.
[Artificial Grass Pee Pad for Dogs, 4x6ft](https://www.amazon.com/dp/B000000001?ref_=x)
Sold by: [Turf Seller](https://www.amazon.com/gp/aag/main?seller=A1)
Return or replace items: Eligible through November 5, 2026
$59.99
Delivered October 6
Your package was left near the front door or porch.
[Screen Repair Tape, Extra Wide](https://www.amazon.com/dp/B000000002?ref_=x)
Sold by: [Tape Seller](https://www.amazon.com/gp/aag/main?seller=A2)
Return or replace items: Eligible through November 5, 2026
$9.99
[Freeze Dried Dog Training Treats (Beef Liver, 4 oz)](https://www.amazon.com/dp/B000000003)
Sold by: [Treat Seller](https://www.amazon.com/gp/aag/main?seller=A3)
Return items: Eligible through November 5, 2026
$16.85
[Replacement Canister Filter Pads](https://www.amazon.com/dp/B000000004)
Sold by: [Filter Seller](https://www.amazon.com/gp/aag/main?seller=A4)
Return or replace items: Eligible through November 5, 2026
$13.99
Back to top
- [Help](https://www.amazon.com/gp/help/customer/display.html)
"""

# The order-details layout: gift card, FSA line, and recommendations below.
ORDER_DETAILS_WITH_GIFT_CARD = """
Order placed October 1, 2026  Order # 111-2000002-0000002
Order Summary
Item(s) Subtotal:
$35.66
Shipping & Handling:
$0.00
Total before tax:
$35.66
Estimated tax to be collected:
$1.66
Gift Card Amount:
-$5.00
Grand Total:
$32.32
FSA or HSA eligible:
(inc. tax and shipping)
$10.68
Your delivery is taking longer than expected
Reflective Dog Bandana, Small
Reflective Dog Bandana, Small
Sold by: Bandana Seller
$5.99
Track package
Antacid Chewable Tablets, 100 Count
Sold by: Amazon.com
Return items: Eligible through November 2, 2026
$10.68
FSA or HSA eligible
Reflective Dog Collar, Small
Sold by: Collar Seller
Return or replace items: Eligible through November 2, 2026
$18.99
Buy again
Customers also bought
Dog Reflective Vest, Yellow, Small
4.4 out of 5 stars 1,586
$16.97 ($16.97/count)
"""


def test_parses_items_and_totals() -> None:
    summary = parse_order_summary(ORDER_SUMMARY)

    assert summary is not None
    assert summary.order_id == "111-2000001-0000001"
    assert summary.items == [
        InvoiceItem("Artificial Grass Pee Pad for Dogs, 4x6ft", 59.99),
        InvoiceItem("Screen Repair Tape, Extra Wide", 9.99),
        InvoiceItem("Freeze Dried Dog Training Treats (Beef Liver, 4 oz)", 16.85),
        InvoiceItem("Replacement Canister Filter Pads", 13.99),
    ]
    assert (summary.subtotal, summary.tax, summary.grand_total) == (
        100.82,
        6.68,
        107.50,
    )


def test_ignores_recommendations_and_status_lines() -> None:
    summary = parse_order_summary(ORDER_DETAILS_WITH_GIFT_CARD)

    assert summary is not None
    assert summary.order_id == "111-2000002-0000002"
    assert [(i.name, i.price) for i in summary.items] == [
        ("Reflective Dog Bandana, Small", 5.99),
        ("Antacid Chewable Tablets, 100 Count", 10.68),
        ("Reflective Dog Collar, Small", 18.99),
    ]
    assert summary.grand_total == 32.32


def test_no_items_returns_none() -> None:
    assert parse_order_summary("Grand Total: $10.00") is None


def test_allocate_spreads_tax_and_sums_exactly() -> None:
    amounts = allocate_amount(107.50, [59.99, 9.99, 16.85, 13.99])

    assert amounts == [63.96, 10.65, 17.97, 14.92]
    assert round(sum(amounts), 2) == 107.50


def test_allocate_spreads_gift_card_discount() -> None:
    amounts = allocate_amount(32.32, [5.99, 10.68, 18.99])

    assert round(sum(amounts), 2) == 32.32
    assert amounts[0] < 5.99 * 1.05


def test_allocate_edge_cases() -> None:
    assert allocate_amount(10.0, []) == []
    assert allocate_amount(10.0, [0.0, 0.0]) == [0.0, 10.0]


def test_effective_tax_rate_uses_total_before_tax() -> None:
    summary = parse_order_summary(ORDER_SUMMARY)
    assert summary is not None
    assert summary.pre_tax_total == 100.82
    assert effective_tax_rate(summary) == 0.06626


def test_effective_tax_rate_unknown_without_tax() -> None:
    summary = OrderSummary(
        "1", [], subtotal=10.0, pre_tax_total=None, tax=None, grand_total=10.0
    )
    assert effective_tax_rate(summary) is None
