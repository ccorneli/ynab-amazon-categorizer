"""Parse the Amazon "Your Payments > Transactions" page into card charges.

An order's total on the orders page isn't always what reaches the bank:
split shipments are charged separately, and gift-card balance is deducted
from what the card is charged. The transactions page lists each charge with
its order number, so matching against those charges is exact.
"""

import re
from dataclasses import dataclass, replace
from datetime import datetime

from .models import Order

_ORDER_ID_PATTERN = r"(?:\d{3}|D\d{2})-\d{7}-\d{7}"

# One entry, e.g. (whitespace and line breaks between the parts vary):
#   Oct 02, 2026·AMZN Mktp US
#   Prime Visa••••1234
#   Order #111-1234567-1234567
#   -$32.32
#   Charged
_CHARGE_PATTERN = re.compile(
    r"(?P<date>[A-Z][a-z]{2,8}\.? \d{1,2}, \d{4})\s*·"
    r"(?P<details>[^·]*?)"
    rf"Order #\s*(?P<order_id>{_ORDER_ID_PATTERN})\s*"
    r"(?P<sign>[-+−])\s*(?P<currency>C(?:A|DN)\$|US\$|[$£€])?\s*"
    r"(?P<amount>\d[\d,]*\.\d{2})",
)

_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y")


@dataclass(frozen=True, slots=True)
class AmazonCharge:
    """One line of the Amazon transactions page."""

    order_id: str
    date_str: str
    amount: float
    """Signed like YNAB: negative for a charge, positive for a refund."""
    currency: str | None
    is_gift_card: bool

    @property
    def is_refund(self) -> bool:
        return self.amount > 0


def _normalize_date(raw: str) -> str:
    """Return the 'October 2, 2026' form the matcher expects."""
    cleaned = raw.replace(".", "").replace("Sept ", "Sep ")
    for date_format in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(cleaned, date_format)
        except ValueError:
            continue
        return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"
    return raw


def parse_transactions_page(text: str) -> list[AmazonCharge]:
    """Extract the charges and refunds from pasted transactions-page text."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1 ", text)  # [text](url) -> text
    charges = []
    for match in _CHARGE_PATTERN.finditer(text):
        amount = float(match.group("amount").replace(",", ""))
        if match.group("sign") != "+":
            amount = -amount
        charges.append(
            AmazonCharge(
                order_id=match.group("order_id"),
                date_str=_normalize_date(match.group("date")),
                amount=amount,
                currency=match.group("currency"),
                is_gift_card="gift card" in match.group("details").lower(),
            ),
        )
    return charges


def orders_from_charges(
    orders: list[Order],
    charges: list[AmazonCharge],
) -> list[Order]:
    """Turn each card charge into an order to match against YNAB.

    A charge takes the items and currency of the order with its number, and
    its own amount and date. Gift-card payments are dropped because they never
    reach the bank. Orders with no line on the transactions page (e.g. older
    than the pasted range) are kept as-is so they can still match by total.
    """
    by_id = {order.order_id: order for order in orders if order.order_id}
    charged_ids = {charge.order_id for charge in charges}
    result = [order for order in orders if order.order_id not in charged_ids]
    for index, charge in enumerate(charges):
        if charge.is_gift_card:
            continue
        base = by_id.get(charge.order_id) or Order(order_id=charge.order_id)
        result.append(
            replace(
                base,
                total=abs(charge.amount),
                date_str=charge.date_str,
                items=list(base.items),
                currency=base.currency or charge.currency,
                charge_id=f"{charge.order_id}#{index}",
                refund=charge.is_refund,
            ),
        )
    return result
