"""Typed domain models used across parsing, matching, and YNAB updates."""

from dataclasses import dataclass, field
from datetime import date
from typing import NotRequired, TypedDict, cast


@dataclass(slots=True)
class Order:
    """A parsed Amazon order.

    Optional scalar fields allow partially parsed orders to remain useful for
    matching while making their shape explicit and easy to construct in tests.
    """

    order_id: str | None = None
    total: float | None = None
    date_str: str | None = None
    items: list[str] = field(default_factory=list)
    currency: str | None = None
    charge_id: str | None = None
    """Set when this entry is one card charge from the transactions page."""
    refund: bool | None = None
    """For a charge, whether it was a refund; None when unknown (order totals)."""


def order_match_key(order: Order) -> str | None:
    """Key that marks an order (or one of its charges) as used by a match."""
    return order.charge_id or order.order_id


def format_currency_amount(amount: float | None, currency: str | None = None) -> str:
    """Format an amount with its parsed currency, defaulting legacy orders to dollars."""
    if amount is None:
        return "N/A"
    sign = "-" if amount < 0 else ""
    return f"{sign}{currency or '$'}{abs(amount):.2f}"


class YNABTransaction(TypedDict):
    """YNAB transaction fields consumed by this application."""

    id: str
    account_id: str
    date: str
    amount: int
    payee_id: NotRequired[str | None]
    payee_name: NotRequired[str | None]
    category_id: NotRequired[str | None]
    memo: NotRequired[str | None]
    cleared: NotRequired[str | None]
    approved: NotRequired[bool]
    flag_color: NotRequired[str | None]
    import_id: NotRequired[str | None]
    transfer_account_id: NotRequired[str | None]
    subtransactions: NotRequired[list[object]]


class SaveSubtransaction(TypedDict):
    """Fields accepted when creating a YNAB split subtransaction."""

    amount: int
    category_id: str
    memo: str | None


class TransactionUpdate(TypedDict, total=False):
    """Minimal set of fields intentionally changed in a YNAB update."""

    category_id: str | None
    memo: str
    approved: bool
    subtransactions: list[SaveSubtransaction]


_REQUIRED_STRINGS = ("id", "account_id", "date")

_NULLABLE_STRINGS = (
    "payee_id",
    "payee_name",
    "category_id",
    "memo",
    "cleared",
    "flag_color",
    "import_id",
    "transfer_account_id",
)


def _require_non_empty_strings(raw: dict[str, object]) -> None:
    """Require the identity fields YNAB always sends to be present and non-empty."""
    for field_name in _REQUIRED_STRINGS:
        field_value = raw.get(field_name)
        if not isinstance(field_value, str) or not field_value:
            raise ValueError(f"{field_name} must be a non-empty string")


def _require_nullable_strings(raw: dict[str, object]) -> None:
    """Require the optional string fields to be strings when YNAB sends them."""
    for field_name in _NULLABLE_STRINGS:
        field_value = raw.get(field_name)
        if field_value is not None and not isinstance(field_value, str):
            raise ValueError(f"{field_name} must be a string or null")


def _require_iso_date(raw: dict[str, object]) -> None:
    """Require ``date`` to be a parseable ISO calendar date."""
    try:
        date.fromisoformat(cast("str", raw["date"]))
    except ValueError as exc:
        raise ValueError("date must use ISO YYYY-MM-DD format") from exc


def _require_amount(raw: dict[str, object]) -> None:
    """Require ``amount`` to be milliunits, rejecting the bool subclass of int."""
    amount = raw.get("amount")
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise ValueError("amount must be an integer number of milliunits")


def _require_optional_fields(raw: dict[str, object]) -> None:
    """Require ``approved`` and ``subtransactions`` to match their YNAB types."""
    approved = raw.get("approved")
    if approved is not None and not isinstance(approved, bool):
        raise ValueError("approved must be a boolean")

    subtransactions = raw.get("subtransactions")
    if subtransactions is not None and not isinstance(subtransactions, list):
        raise ValueError("subtransactions must be a list")


def validate_ynab_transaction(value: object) -> YNABTransaction:
    """Validate the YNAB fields required by filtering and processing."""
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    raw = cast("dict[str, object]", value)

    _require_non_empty_strings(raw)
    _require_iso_date(raw)
    _require_amount(raw)
    _require_nullable_strings(raw)
    _require_optional_fields(raw)

    return cast("YNABTransaction", raw)
