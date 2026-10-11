"""Parse an Amazon order summary (invoice) page for per-item prices.

The orders list only shows one total per order. The order summary page lists
each item's price plus the subtotal, tax and grand total, which is what split
transactions need.
"""

import re
from dataclasses import dataclass

_ORDER_ID_PATTERN = r"(?:\d{3}|D\d{2})-\d{7}-\d{7}"
_PRICE_LINE = re.compile(r"^\$([\d,]+\.\d{2})$")
_END_OF_ITEMS = ("Back to top", "Sold by:")


@dataclass(frozen=True, slots=True)
class InvoiceItem:
    name: str
    price: float


@dataclass(frozen=True, slots=True)
class OrderSummary:
    order_id: str | None
    items: list[InvoiceItem]
    subtotal: float | None
    pre_tax_total: float | None
    tax: float | None
    grand_total: float | None


def _clean(line: str) -> str:
    line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)  # [text](url) -> text
    return re.sub(r"^[-•*]\s+", "", line.strip()).strip()


def _labeled_amount(text: str, label: str) -> float | None:
    match = re.search(
        rf"{re.escape(label)}:?\s*(-?)\s*\$([\d,]+\.\d{{2}})",
        text,
    )
    if not match:
        return None
    amount = float(match.group(2).replace(",", ""))
    return -amount if match.group(1) else amount


def _item_price(lines: list[str], start: int) -> float | None:
    for line in lines[start:]:
        if line.startswith(_END_OF_ITEMS):
            return None
        price = _PRICE_LINE.match(line)
        if price:
            return float(price.group(1).replace(",", ""))
    return None


def _item_name(lines: list[str], sold_by_index: int) -> str | None:
    for line in reversed(lines[:sold_by_index]):
        if line:
            return line
    return None


def parse_order_summary(text: str) -> OrderSummary | None:
    """Extract items with prices and the order totals; None if no items found.

    Each item is the line before a "Sold by:" line, priced by the first
    "$x.xx" line after it. Recommendations lower on the page have no
    "Sold by:" line, so they are ignored.
    """
    lines = [_clean(line) for line in text.splitlines()]
    items = []
    for index, line in enumerate(lines):
        if not line.startswith("Sold by:"):
            continue
        name = _item_name(lines, index)
        price = _item_price(lines, index + 1)
        if name and price is not None:
            items.append(InvoiceItem(name, price))
    if not items:
        return None

    order_id = re.search(rf"Order\s*#\s*({_ORDER_ID_PATTERN})", text)
    return OrderSummary(
        order_id=order_id.group(1) if order_id else None,
        items=items,
        subtotal=_labeled_amount(text, "Item(s) Subtotal"),
        pre_tax_total=_labeled_amount(text, "Total before tax"),
        tax=_labeled_amount(text, "Estimated tax to be collected"),
        grand_total=_labeled_amount(text, "Grand Total"),
    )


def allocate_amount(total: float, prices: list[float]) -> list[float]:
    """Split ``total`` across items in proportion to their prices.

    Spreads tax, shipping and discounts the way the order did. Amounts are
    rounded to cents and the last item takes the rounding remainder, so they
    always add up to ``total`` exactly.
    """
    total_cents = round(total * 100)
    price_sum = sum(prices)
    if not prices:
        return []
    if price_sum <= 0:
        shares = [0] * (len(prices) - 1)
    else:
        shares = [round(total_cents * price / price_sum) for price in prices[:-1]]
    shares.append(total_cents - sum(shares))
    return [share / 100 for share in shares]


def effective_tax_rate(summary: OrderSummary) -> float | None:
    """The order's tax as a fraction of its pre-tax total, or None if unknown."""
    base = summary.pre_tax_total or summary.subtotal
    if summary.tax is None or not base:
        return None
    return round(summary.tax / base, 5)
