"""Interactive CLI: match Amazon orders to YNAB transactions and categorize."""

import argparse
import contextlib
import copy
import io
import json
import logging
import os
import sys
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import requests
from prompt_toolkit import prompt
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent

from . import __version__
from .amazon_invoice import allocate_amount, parse_order_summary
from .amazon_parser import AmazonParser, Order
from .amazon_transactions import (
    AmazonCharge,
    orders_from_charges,
    parse_transactions_page,
)
from .batch import process_batch
from .config import Config
from .exceptions import ConfigurationError, YNABAPIError
from .memo_generator import (
    MemoGenerator,
    amazon_orders_page_url,
    amazon_transactions_page_url,
    generate_split_summary_memo,
    sanitize_memo,
)
from .models import (
    SaveSubtransaction,
    TransactionUpdate,
    YNABTransaction,
    format_currency_amount,
    order_match_key,
)
from .payloads import (
    build_single_payload,
    build_split_payload,
)
from .setup_wizard import ENV_FILENAME, run_setup
from .tax import tax_rate_for_category as _tax_rate_for_category
from .transaction_matcher import TransactionMatcher
from .transactions import fetch_amazon_transactions
from .ynab_client import YNABClient

logger = logging.getLogger(__name__)


def _env_flag(var_name: str, default: bool = False) -> bool:
    """Read a boolean flag from the environment (e.g. a .env file).

    Truthy values: ``1``, ``true``, ``yes``, ``y`` (case-insensitive).
    Read at call-time so values loaded from ``.env`` during startup are used.
    """
    raw = os.getenv(var_name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y")


def prompt_for_amazon_orders_data() -> list[Order] | None:
    """Prompt user to paste Amazon orders page data."""
    print("\n--- Amazon Orders Data Entry ---")
    print("You can copy and paste the content from your Amazon orders page.")
    print("This will help automatically extract order details and item information.")

    print("\nPaste Amazon orders page content:")

    orders_text = get_multiline_input_with_custom_submit("Paste here: ")

    if orders_text is None or orders_text.strip().lower() == "skip":
        print("Skipping Amazon orders data entry.")
        return None

    if not orders_text.strip():
        return None

    # Use extracted Amazon parser
    amazon_parser = AmazonParser()
    parsed_orders = amazon_parser.parse_orders_page(orders_text)

    # Show what was parsed
    if parsed_orders:
        print(f"\n✓ Successfully parsed {len(parsed_orders)} orders from Amazon data")
        for order in parsed_orders[:3]:
            print(
                f"  - Order {order.order_id}: "
                f"{format_currency_amount(order.total, order.currency)} on {order.date_str}",
            )
        if len(parsed_orders) > 3:
            print(f"  ... and {len(parsed_orders) - 3} more orders")
    else:
        print("\nNo orders could be parsed from the provided text.")
        print("This might be due to formatting differences in the copied text.")

    return parsed_orders


def submit_key_hint(platform: str | None = None) -> str:
    """Describe the Meta+Enter submit keys in the platform's own terms.

    Terminals send Meta+Enter as Escape followed by Enter. Mac terminals don't
    map Option to Meta by default, so Esc then Return is the reliable keystroke.
    """
    if (platform or sys.platform) == "darwin":
        return "Esc then Return (or Option+Return if your terminal uses Option as Meta)"
    return "Alt+Enter (or Esc then Enter)"


def get_multiline_input_with_custom_submit(
    prompt_message: str = "Enter multiline text: ",
) -> str | None:
    """Get multiline input, submitted with Meta+Enter (see submit_key_hint)."""
    kb = KeyBindings()

    @kb.add("escape", "enter")
    def _(event: KeyPressEvent) -> None:
        """Accept the current buffer's text on Meta+Enter."""
        event.app.exit(result=event.app.current_buffer.text)

    print("Press Enter for a new line.")
    print(f"Submit by pressing {submit_key_hint()}.")
    print("Press Ctrl+C to cancel.")

    try:
        user_input = prompt(prompt_message, multiline=True, key_bindings=kb)
    except EOFError:
        print("\nInput cancelled (EOF).")
        return None
    except KeyboardInterrupt:
        print("\nInput cancelled (KeyboardInterrupt).")
        return None
    else:
        return user_input


def _prompt_line(message: str) -> str:
    """Read one line of input via prompt_toolkit for consistent UX.

    Used in place of the builtin input function so every prompt in the tool goes
    through prompt_toolkit (uniform rendering and key handling). Mirrors builtin
    input semantics: returns the entered text and lets ``EOFError`` /
    ``KeyboardInterrupt`` propagate to the caller, so existing ``.strip()`` /
    ``.lower()`` chains on the result keep working.
    """
    return prompt(message)


def _prompt_secret(message: str) -> str:
    """Read one line of input without echoing it."""
    return prompt(message, is_password=True)


def _prompt_quantity() -> int | None:
    while True:
        qty_input = _prompt_line(
            "Enter quantity (optional, press Enter to skip): ",
        ).strip()
        if not qty_input:
            return None
        try:
            quantity = int(qty_input)
            if quantity > 0:
                return quantity
            print("Quantity must be positive.")
        except ValueError:
            print("Please enter a valid number.")


def _prompt_price() -> float | None:
    while True:
        price_input = _prompt_line(
            "Enter item price (optional, press Enter to skip): ",
        ).strip()
        if not price_input:
            return None
        try:
            price = float(price_input.replace("$", "").replace(",", ""))
            if price >= 0:
                return price
            print("Price must be non-negative.")
        except ValueError:
            print("Please enter a valid price (e.g., 29.99).")


def prompt_for_item_details() -> dict[str, str | int | float | list[str] | None] | None:
    """Prompt user to enter item details manually."""
    print("\n--- Manual Item Details Entry ---")

    item_details: dict[str, str | int | float | list[str] | None] = {}

    # Get item title/description
    title = _prompt_line("Enter item title/description (optional): ").strip()
    if title:
        item_details["title"] = title

    # Get quantity
    quantity = _prompt_quantity()
    if quantity is not None:
        item_details["quantity"] = quantity

    # Get price per item
    price = _prompt_price()
    if price is not None:
        item_details["price"] = price

    return item_details or None


# --- Extracted Helper Functions ---


def print_config_summary(config: Config) -> None:
    """Print configuration summary without exposing secrets."""
    print(f"ynab-amazon-categorizer v{__version__}")
    print("✓ Configuration loaded successfully")
    print("✓ API Key: configured")
    if config.budget_id and len(config.budget_id) >= 4:
        print(f"✓ Budget ID: ...{config.budget_id[-4:]}")
    else:
        print("✓ Budget ID: configured")
    if config.account_id:
        print("✓ Account ID: configured")
    else:
        print("✓ All accounts")


def build_preview(
    payload: Mapping[str, object],
    category_id_map: dict[str, str],
) -> dict[str, Any]:
    """Build a preview dict from payload with category names injected.

    Uses deep copy to avoid mutating the original payload.
    """
    preview_dict: dict[str, Any] = copy.deepcopy(dict(payload))
    category_id = preview_dict.get("category_id")
    if isinstance(category_id, str):
        category_name = category_id_map.get(category_id, "Unknown Category")
        preview_dict["category_name"] = category_name
    subtransactions_value = preview_dict.get("subtransactions")
    if isinstance(subtransactions_value, list):
        for subtrans in subtransactions_value:
            if not isinstance(subtrans, dict):
                continue
            subtrans_category_id = subtrans.get("category_id")
            if isinstance(subtrans_category_id, str):
                cat_name = category_id_map.get(subtrans_category_id, "Unknown Category")
                subtrans["category_name"] = cat_name
    return preview_dict


def compute_split_amount(amount_float: float, remaining_milliunits: int) -> int:
    """Convert a positive user-entered amount to signed milliunits matching the parent.

    The sign of the result matches ``remaining_milliunits`` (negative for outflows,
    positive for inflows/refunds).

    Raises ``ValueError`` if the amount exceeds the remaining balance.
    """
    split_amount_milliunits = round(amount_float * 1000)

    if split_amount_milliunits > abs(remaining_milliunits) + 1:
        raise ValueError(
            f"Amount exceeds remaining. Max {abs(remaining_milliunits / 1000.0):.2f}",
        )

    # Apply sign to match parent transaction direction
    if remaining_milliunits < 0:
        split_amount_milliunits = -abs(split_amount_milliunits)
    else:
        split_amount_milliunits = abs(split_amount_milliunits)

    # Snap to exact remainder when within 1 milliunit
    if abs(abs(split_amount_milliunits) - abs(remaining_milliunits)) <= 1:
        split_amount_milliunits = remaining_milliunits

    return split_amount_milliunits


class CategoryCompleter(Completer):
    """Tab-completion over YNAB category names for prompt_toolkit."""

    def __init__(self, category_list: list[tuple[str, str]]) -> None:
        self.categories = [name for name, _id in category_list]
        self.category_list = category_list

    def get_completions(
        self,
        document: Document,
        complete_event: CompleteEvent,  # noqa: ARG002  (Completer protocol)
    ) -> Iterable[Completion]:
        """Yield category completions matching the text before the cursor."""
        text_before_cursor = document.text_before_cursor.lower()
        if text_before_cursor:
            for category_name in self.categories:
                if text_before_cursor in category_name.lower():
                    yield Completion(
                        category_name,
                        start_position=-len(text_before_cursor),
                    )


def _lookup_category(
    user_input: str,
    name_to_id_map: dict[str, str],
    category_completer: CategoryCompleter,
) -> tuple[str, str] | None:
    """Find (category_id, display_name) for user input if recognized."""
    input_lower = user_input.lower()
    if input_lower not in name_to_id_map:
        return None
    selected_id = name_to_id_map[input_lower]
    selected_display_name = next(
        (
            name
            for name, cat_id in category_completer.category_list
            if cat_id == selected_id
        ),
        "",
    )
    return selected_id, selected_display_name


def prompt_for_category_selection(
    category_completer: CategoryCompleter,
    name_to_id_map: dict[str, str],
) -> tuple[str | None, str | None]:
    """Prompt for a category until one resolves, or the user backs out.

    Returns ``(category_id, display_name)``, or ``(None, None)`` when the user
    backs out with 'b', two blank Enters, or a cancellation key.
    """
    history_file = Path.home() / ".ynab_amazon_cat_history"
    history = FileHistory(str(history_file))
    empty_streak = 0
    try:
        while True:
            user_input = prompt(
                "Enter category name (Tab to complete, Enter to confirm, "
                "empty+Enter twice or 'b' to go back): ",
                completer=category_completer,
                history=history,
                reserve_space_for_menu=5,
            ).strip()
            if not user_input:
                empty_streak += 1
                if empty_streak >= 2:
                    return None, None
                print(
                    "Press Enter again with nothing typed to go back, "
                    "or start typing a category name.",
                )
                continue
            empty_streak = 0
            if user_input.lower() == "b":
                return None, None

            match = _lookup_category(user_input, name_to_id_map, category_completer)
            if match:
                selected_id, selected_display_name = match
                print(f"Selected: {selected_display_name}")
                return selected_id, selected_display_name

            print(
                f"Error: '{user_input}' is not a recognized category. Please use Tab completion or try again.",
            )
    except EOFError:
        print("\nOperation cancelled by user (EOF).")
        return None, None
    except KeyboardInterrupt:
        print("\nOperation cancelled by user (KeyboardInterrupt).")
        return None, None


# --- Extracted per-transaction functions ---


def display_matched_order(matching_order: Order, memo_generator: MemoGenerator) -> None:
    """Display matched order details to the user."""
    print("\n  🎯 MATCHED ORDER FOUND:")
    print(f"     Order ID: {matching_order.order_id}")
    amount_label = {None: "Total", False: "Charged", True: "Refunded"}[
        matching_order.refund
    ]
    print(
        f"     {amount_label}: "
        f"{format_currency_amount(matching_order.total, matching_order.currency)}",
    )
    print(
        f"     Date: {matching_order.date_str if matching_order.date_str is not None else 'N/A'}",
    )
    order_link = memo_generator.generate_amazon_order_link(matching_order.order_id)
    print(f"     Order Link: {order_link}")
    if matching_order.items:
        print("     Items:")
        for item in matching_order.items:
            print(f"       - {item}")
    print()


def _get_item_details(
    matching_order: Order | None,
) -> dict[str, str | int | float | list[str] | None] | None:
    if matching_order:
        print("Using matched order data for memo generation...")
        return {
            "order_id": matching_order.order_id or "",
            "items": matching_order.items,
            "total": matching_order.total,
            "date": matching_order.date_str,
        }

    # Ask if user wants to enter item details manually
    manual_entry = _prompt_line(
        "No order match found. Enter item details manually? (y/n, default n): ",
    ).lower()
    if manual_entry == "y":
        return prompt_for_item_details()
    return None


def _build_suggested_memo(
    item_details: dict[str, str | int | float | list[str] | None] | None,
    matching_order: Order | None,
    original_memo: str,
    memo_generator: MemoGenerator,
) -> str:
    if not item_details:
        return original_memo

    if isinstance(item_details, dict) and "items" in item_details:
        # Auto-matched order data - format as: Item Name\n Order Link
        items_text = (
            generate_split_summary_memo(matching_order) if matching_order else ""
        ) or "Amazon Purchase"
        order_id_value = item_details["order_id"]
        order_link = memo_generator.generate_amazon_order_link(
            order_id_value if isinstance(order_id_value, str) else None,
        )
        return f"{items_text}\n {order_link}" if order_link else items_text

    # Manual item details
    return memo_generator.generate_enhanced_memo(original_memo, None, item_details)


def _prompt_memo_confirmation(suggested_memo: str, original_memo: str) -> str:
    if suggested_memo and suggested_memo != original_memo:
        print("\nSuggested memo:")
        print(f"'{suggested_memo}'")
        use_suggested = _prompt_line("Use suggested memo? (y/n, default y): ").lower()
        if use_suggested != "n":
            return sanitize_memo(suggested_memo)
        print("Enter custom memo (multiline):")
        memo_input = get_multiline_input_with_custom_submit("> ")
        return sanitize_memo(memo_input.strip()) if memo_input else ""

    print("Enter optional memo (multiline):")
    memo_input = get_multiline_input_with_custom_submit("> ")
    return sanitize_memo(memo_input.strip()) if memo_input else ""


def resolve_memo(
    matching_order: Order | None,
    original_memo: str,
    memo_generator: MemoGenerator,
) -> str:
    """Determine the memo for a single-category transaction.

    Uses matched order data when available, otherwise prompts for manual entry.
    Returns the final memo string (already sanitized).
    """
    item_details = _get_item_details(matching_order)
    enhanced_memo = _build_suggested_memo(
        item_details,
        matching_order,
        original_memo,
        memo_generator,
    )
    return _prompt_memo_confirmation(enhanced_memo, original_memo)


def _parse_currency_input(raw: str) -> float:
    """Parse a user-typed currency amount, tolerating '$' and thousands separators."""
    return float(raw.replace("$", "").replace(",", ""))


def _resolve_split_amount_float(
    base_str: str,
    max_amount: float,
    tax_rate: float,
    tax_pct: float,
) -> float | None:
    """Turn the raw split-amount input into a charged total.

    Blank uses the remaining balance as-is; a leading '=' is an exact charged
    total with no tax math; anything else is a pre-tax base price that gets tax
    added. Returns None when the value is non-positive and should be re-entered.
    Raises ValueError for unparseable input, handled by the caller's prompt loop.
    """
    if not base_str:
        return max_amount

    if base_str.startswith("="):
        split_amount_float = _parse_currency_input(base_str[1:])
        if split_amount_float <= 0:
            print("Amount must be positive.")
            return None
        return split_amount_float

    base_amount = _parse_currency_input(base_str)
    if base_amount <= 0:
        print("Amount must be positive.")
        return None
    tax_amount = round(base_amount * tax_rate, 2)
    split_amount_float = round(base_amount + tax_amount, 2)
    print(
        f"  Base: ${base_amount:.2f}  +  Tax ({tax_pct:g}%): "
        f"${tax_amount:.2f}  =  Total: ${split_amount_float:.2f}",
    )
    return split_amount_float


def _prompt_split_amount_milliunits(
    category_name: str | None,
    remaining_milliunits: int,
    suggested_amount: float | None = None,
) -> int:
    """Prompt the user for a split amount and return the signed milliunits value.

    With a ``suggested_amount`` (from the order summary page), blank accepts it.
    """
    tax_rate = _tax_rate_for_category(category_name)
    tax_pct = tax_rate * 100
    while True:
        try:
            max_amount = abs(remaining_milliunits / 1000.0)
            max_base = max_amount / (1 + tax_rate) if tax_rate else max_amount
            if suggested_amount is not None:
                default = min(suggested_amount, max_amount)
                base_str = _prompt_line(
                    f"Enter amount for '{category_name}' (blank = {default:.2f} from"
                    f" the order summary; or a base price for {tax_pct:g}% tax,"
                    " or =exact total): ",
                ).strip()
            else:
                default = None
                base_str = _prompt_line(
                    f"Enter base price for '{category_name}' ({tax_pct:g}% tax, "
                    f"max base ~{max_base:.2f}, blank = remaining {max_amount:.2f} as-is): ",
                ).strip()

            split_amount_float = (
                default
                if default is not None and not base_str
                else _resolve_split_amount_float(
                    base_str,
                    max_amount,
                    tax_rate,
                    tax_pct,
                )
            )
            if split_amount_float is None:
                continue

            split_amount_milliunits = compute_split_amount(
                split_amount_float,
                remaining_milliunits,
            )
            if split_amount_milliunits == remaining_milliunits:
                print("Amount covers remaining balance.")
        except ValueError as e:
            print(str(e) if str(e) != str(e).lower() else "Invalid amount.")
        else:
            return split_amount_milliunits


def _print_split_item(matching_order: Order | None, split_count: int) -> None:
    """Show which matched order item this split covers, when known."""
    items: list[str] = matching_order.items if matching_order else []
    if not items:
        return
    if split_count <= len(items):
        print(f"Item {split_count}: {items[split_count - 1]}")
    else:
        print("Additional split for remaining items")


def _absorb_tiny_remainder(
    subtransactions: list[SaveSubtransaction],
    remaining_milliunits: int,
) -> int:
    """Fold a sub-cent remainder into the last split and return what is left."""
    if abs(remaining_milliunits) > 1:
        return remaining_milliunits
    print("Remaining amount negligible.")
    if subtransactions:
        print(f"Adjusting last split amount by {remaining_milliunits} milliunits.")
        subtransactions[-1]["amount"] += remaining_milliunits
    return 0  # Force complete


def handle_split(
    transaction: Mapping[str, Any],
    matching_order: Order | None,
    memo_generator: MemoGenerator,
    category_completer: CategoryCompleter,
    category_name_map: dict[str, str],
    suggested_amounts: list[float] | None = None,
) -> list[SaveSubtransaction] | None:
    """Handle split transaction flow.

    ``suggested_amounts`` gives a default amount per item, in item order.
    Returns list of subtransaction dicts, or None if cancelled.
    """
    print("\n--- Splitting Transaction ---")
    subtransactions: list[SaveSubtransaction] = []
    amount_milliunits = transaction["amount"]
    remaining_milliunits = amount_milliunits
    split_count = 1

    while remaining_milliunits != 0:
        print(
            f"\nSplit {split_count}: Amount remaining: {abs(remaining_milliunits / 1000.0):.2f}",
        )

        # Show which item this split is for if we have matched order data
        _print_split_item(matching_order, split_count)

        print(f"Enter category name for split {split_count}:")
        category_id, category_name = prompt_for_category_selection(
            category_completer,
            category_name_map,
        )
        if category_id is None:  # User backed out
            print("Cancelling split process.")
            return None

        # Get amount for this split: enter the pre-tax base item price and
        # let the tool add sales tax automatically (rate chosen by category
        # via _tax_rate_for_category). Blank uses the full remaining balance
        # as-is (e.g. for a final catch-all split); '=' prefix enters an
        # exact charged total with no tax math applied.
        split_amount_milliunits = _prompt_split_amount_milliunits(
            category_name,
            remaining_milliunits,
            suggested_amounts[split_count - 1]
            if suggested_amounts and split_count <= len(suggested_amounts)
            else None,
        )

        # --- ENHANCED SPLIT MEMO INPUT ---
        split_memo = _resolve_split_memo(
            matching_order,
            memo_generator,
            category_name,
            split_count,
        )
        # --- END ENHANCED SPLIT MEMO INPUT ---

        subtransactions.append(
            {
                "amount": split_amount_milliunits,
                "category_id": category_id,
                "memo": sanitize_memo(split_memo) if split_memo else None,
            },
        )

        remaining_milliunits -= split_amount_milliunits
        split_count += 1
        remaining_milliunits = _absorb_tiny_remainder(
            subtransactions,
            remaining_milliunits,
        )

    if remaining_milliunits == 0 and subtransactions:
        return subtransactions
    return None


def prompt_for_invoice_split(
    matching_order: Order | None,
    memo_generator: MemoGenerator,
    amount_milliunits: int,
) -> tuple[Order, list[float]] | None:
    """Offer to paste the order summary page and suggest an amount per item.

    Returns the order with the summary page's items (in page order) and the
    matching amounts, or None to split without suggestions.
    """
    if matching_order is None or not matching_order.order_id:
        return None
    order_id = matching_order.order_id
    print("\nItem prices are on the order summary page:")
    print(f"  {memo_generator.generate_amazon_invoice_link(order_id)}")
    print(
        "Open it, select all and copy, then paste it below to fill in split"
        " amounts. Submit it empty to enter amounts yourself.",
    )
    text = get_multiline_input_with_custom_submit("Paste here: ")
    if text is None or not text.strip() or text.strip().lower() == "skip":
        return None
    summary = parse_order_summary(text)
    if summary is None:
        print("No item prices found in that text; enter amounts manually.")
        return None
    if summary.order_id and summary.order_id != order_id:
        print(f"That page is order {summary.order_id}, not {order_id}; ignoring it.")
        return None

    prices = [item.price for item in summary.items]
    if summary.subtotal is not None and abs(sum(prices) - summary.subtotal) >= 0.01:
        print(
            f"⚠ Item prices add up to ${sum(prices):.2f} but the subtotal is"
            f" ${summary.subtotal:.2f} (quantities?); check the amounts.",
        )
    charge = abs(amount_milliunits) / 1000
    order_total = summary.grand_total if summary.grand_total is not None else charge
    if abs(charge - order_total) >= 0.01:
        print(
            f"⚠ This charge (${charge:.2f}) is only part of the order"
            f" (${order_total:.2f}); amounts are each item's share of the whole order.",
        )
    amounts = allocate_amount(order_total, prices)

    print("Amount per item (price plus its share of tax, shipping and discounts):")
    for item, amount in zip(summary.items, amounts, strict=True):
        print(f"  ${amount:.2f}  {item.name}")
    return replace(matching_order, items=[item.name for item in summary.items]), amounts


def _get_suggested_split_memo(
    matching_order: Order | None,
    memo_generator: MemoGenerator,
    split_count: int,
) -> str:
    if matching_order:
        print("Using matched order data for split memo...")
        items = matching_order.items
        order_id = matching_order.order_id

        if split_count <= len(items):
            items_text = items[split_count - 1]
            order_link = memo_generator.generate_amazon_order_link(order_id)
            return f"{items_text}\n {order_link}" if order_link else items_text
        return "Additional item"

    manual_entry = _prompt_line(
        "Enter item details for this split? (y/n, default n): ",
    ).lower()
    if manual_entry == "y":
        item_details = prompt_for_item_details()
        if item_details:
            return memo_generator.generate_enhanced_memo("", None, item_details)
    return ""


def _prompt_split_memo_confirmation(
    suggested_split_memo: str,
    category_name: str | None,
) -> str:
    if suggested_split_memo:
        print(f"Suggested memo for '{category_name}' split:")
        print(f"'{suggested_split_memo}'")
        use_suggested = _prompt_line("Use suggested memo? (y/n, default y): ").lower()
        if use_suggested != "n":
            return suggested_split_memo
        print(f"Enter custom memo for '{category_name}' split (multiline):")
        split_memo = get_multiline_input_with_custom_submit("> ")
        return split_memo.strip() if split_memo else ""

    print(f"Enter optional memo for '{category_name}' split (multiline):")
    split_memo = get_multiline_input_with_custom_submit("> ")
    return split_memo.strip() if split_memo else ""


def _resolve_split_memo(
    matching_order: Order | None,
    memo_generator: MemoGenerator,
    category_name: str | None,
    split_count: int,
) -> str:
    """Resolve memo for a single split within a split transaction."""
    suggested_split_memo = _get_suggested_split_memo(
        matching_order,
        memo_generator,
        split_count,
    )
    return _prompt_split_memo_confirmation(suggested_split_memo, category_name)


def _should_skip_inflow(
    payee: str,
    amount_float: float,
    matching_order: Order | None,
) -> bool:
    """Prompt whether to process an inflow transaction; returns True to skip."""
    currency = matching_order.currency if matching_order else None
    print(
        f"Found inflow transaction: {payee} "
        f"{format_currency_amount(amount_float, currency)}",
    )
    process_inflow = _prompt_line(
        "Process this inflow (refund/credit)? (y/n, default n): ",
    ).lower()
    if process_inflow != "y":
        print("Skipping inflow transaction.")
        return True
    return False


def _print_transaction_summary(
    transaction: Mapping[str, Any],
    index: int,
    total: int,
    amount_float: float,
    matching_order: Order | None,
) -> None:
    """Print standard transaction details header."""
    print(f"\n--- Processing Transaction {index + 1}/{total} ---")
    print(f"  ID:   {transaction['id']}")
    print(f"  Date: {transaction['date']}")
    print(f"  Payee: {transaction.get('payee_name', 'N/A')}")
    amount_display = (
        format_currency_amount(amount_float, matching_order.currency)
        if matching_order
        else f"{amount_float:.2f}"
    )
    print(f"  Amount: {amount_display}")
    if transaction.get("cleared") == "reconciled":
        print(
            "  Status: 🔒 reconciled (category edits do not affect the reconciled balance)",
        )
    original_memo = transaction.get("memo", "")
    if original_memo:
        print(f"  Original Memo: {original_memo}")


def _resolve_matching_order(
    parsed_orders: list[Order] | None,
    amount_float: float,
    date: str,
    used_order_ids: set[str] | None,
) -> Order | None:
    """Find the parsed order matching this transaction, if order data exists."""
    if not parsed_orders:
        return None
    return TransactionMatcher().find_matching_order(
        amount_float,
        date,
        parsed_orders,
        used_order_ids,
    )


def _report_unmatched(stats: dict[str, int] | None) -> None:
    """Announce that order data was provided but nothing matched this transaction.

    There is no data to help categorize it, so the caller skips the prompt
    instead of asking blind. (When ``parsed_orders`` itself is empty -- i.e. no
    order data was provided at all this run -- the caller falls through to the
    normal action loop, which still offers manual item entry.)
    """
    print(
        "  ⚠ No matching order found in parsed Amazon data — "
        "skipping (nothing to categorize from).",
    )
    if stats is not None:
        stats["auto_skipped_no_match"] = stats.get("auto_skipped_no_match", 0) + 1


def _mark_order_used(
    matching_order: Order | None,
    used_order_ids: set[str] | None,
    dry_run: bool,
) -> None:
    """Consume the matched order so a later same-amount transaction cannot reuse it.

    Skipped in dry-run because nothing was actually applied.
    """
    if (
        not dry_run
        and used_order_ids is not None
        and matching_order is not None
        and (key := order_match_key(matching_order)) is not None
    ):
        used_order_ids.add(key)


def _run_action_loop(
    transaction: Mapping[str, Any],
    matching_order: Order | None,
    original_memo: str,
    memo_generator: MemoGenerator,
    ynab_client: YNABClient,
    category_completer: CategoryCompleter,
    category_name_map: dict[str, str],
    category_id_map: dict[str, str],
    used_order_ids: set[str] | None,
    dry_run: bool,
) -> bool:
    """Prompt for categorize/skip/quit until resolved.

    Returns True if processed/skipped, False if the user quit.
    """
    while True:
        action = _prompt_line(
            "Action? (c = categorize/split, s = skip, q = quit, default c): ",
        ).lower()
        if not action:
            action = "c"
        if action == "q":
            print("Quitting.")
            return False
        if action == "s":
            print("Skipping.")
            return True
        if action != "c":
            print("Invalid action. Choose 'c', 's', or 'q'.")
            continue
        result = _handle_categorize(
            transaction,
            matching_order,
            original_memo,
            memo_generator,
            ynab_client,
            category_completer,
            category_name_map,
            category_id_map,
            dry_run,
        )
        if result == "done":
            _mark_order_used(matching_order, used_order_ids, dry_run)
            return True
        # result == "continue" means back to the action prompt


def process_transaction(
    transaction: Mapping[str, Any],
    index: int,
    total: int,
    parsed_orders: list[Order] | None,
    memo_generator: MemoGenerator,
    ynab_client: YNABClient,
    category_completer: CategoryCompleter,
    category_name_map: dict[str, str],
    category_id_map: dict[str, str],
    used_order_ids: set[str] | None = None,
    dry_run: bool = False,
    stats: dict[str, int] | None = None,
) -> bool:
    """Process a single transaction through the interactive flow.

    Returns True if processed/skipped, False if user quit.

    ``used_order_ids`` accumulates the order IDs already applied to a
    transaction so the matcher does not reuse one order for several
    same-amount transactions. When ``dry_run`` is True no changes are sent
    to YNAB and matched orders are not marked as used. ``stats``, if given,
    is used to accumulate run-level counters (currently just
    ``auto_skipped_no_match``) for a summary printed at the end of the run.
    """
    date = transaction["date"]
    payee = transaction.get("payee_name", "N/A")
    amount_milliunits = transaction["amount"]
    amount_float = amount_milliunits / 1000.0
    original_memo = transaction.get("memo", "")

    matching_order = _resolve_matching_order(
        parsed_orders,
        amount_float,
        date,
        used_order_ids,
    )

    if amount_milliunits > 0 and _should_skip_inflow(
        payee,
        amount_float,
        matching_order,
    ):
        return True

    _print_transaction_summary(transaction, index, total, amount_float, matching_order)

    # Try to find matching order from parsed data and show it
    if parsed_orders:
        if not matching_order:
            _report_unmatched(stats)
            return True
        display_matched_order(matching_order, memo_generator)

    return _run_action_loop(
        transaction,
        matching_order,
        original_memo,
        memo_generator,
        ynab_client,
        category_completer,
        category_name_map,
        category_id_map,
        used_order_ids,
        dry_run,
    )


def _should_ask_split(matching_order: Order | None) -> str:
    """Determine whether to split and prompt the user if appropriate."""
    if matching_order and matching_order.items and len(matching_order.items) > 1:
        print("There is more than one item in this transaction.")
        return _prompt_line("Split this transaction? (y/n, default n): ").lower()
    if _env_flag("YNAB_SKIP_SPLIT_PROMPT_SINGLE_ITEM"):
        return "n"
    return _prompt_line("Split this transaction? (y/n, default n): ").lower()


def _confirm_and_apply_update(
    transaction_id: str,
    payload: TransactionUpdate,
    category_id_map: dict[str, str],
    ynab_client: YNABClient,
    dry_run: bool,
) -> str:
    """Preview update and prompt for confirmation before sending to YNAB."""
    print("\n--- Preview Update ---")
    preview_dict = build_preview(payload, category_id_map)
    print(json.dumps(preview_dict, indent=2, ensure_ascii=False))
    if dry_run:
        print("[dry-run] No changes were sent to YNAB.")
        return "done"
    confirm = _prompt_line("Confirm update? (y/n, default y): ").lower()
    if not confirm:
        confirm = "y"
    if confirm == "y":
        try:
            ynab_client.update_transaction(transaction_id, payload)
            print("Update successful.")
        except (
            YNABAPIError,
            requests.exceptions.RequestException,
            OSError,
        ) as exc:
            logger.error("Failed to update transaction %s: %s", transaction_id, exc)
            print(f"Update failed: {exc}")
            return "continue"
        else:
            return "done"
    else:
        print("Update cancelled.")
        return "continue"


def _handle_categorize(
    transaction: Mapping[str, Any],
    matching_order: Order | None,
    original_memo: str,
    memo_generator: MemoGenerator,
    ynab_client: YNABClient,
    category_completer: CategoryCompleter,
    category_name_map: dict[str, str],
    category_id_map: dict[str, str],
    dry_run: bool = False,
) -> str:
    """Handle the categorize action for a transaction.

    Returns "done" if the transaction was successfully updated (or split completed),
    or "continue" to go back to the action prompt.

    When ``dry_run`` is True the preview is shown but no update is sent to YNAB.
    """
    transaction_id = transaction["id"]
    split_decision = _should_ask_split(matching_order)

    if split_decision != "y":
        # --- SINGLE CATEGORY ---
        print("Enter category name for the transaction:")
        category_id, _category_name = prompt_for_category_selection(
            category_completer,
            category_name_map,
        )
        if category_id is None:
            return "continue"

        memo_input = resolve_memo(matching_order, original_memo, memo_generator)
        updated_payload_dict = build_single_payload(
            category_id,
            memo_input or original_memo,
        )
    else:
        # --- SPLITTING ---
        invoice = prompt_for_invoice_split(
            matching_order,
            memo_generator,
            transaction["amount"],
        )
        split_order, suggested_amounts = invoice or (matching_order, None)
        subtransactions = handle_split(
            transaction,
            split_order,
            memo_generator,
            category_completer,
            category_name_map,
            suggested_amounts,
        )
        if not subtransactions:
            print("Splitting cancelled. No changes will be made.")
            return "continue"
        updated_payload_dict = build_split_payload(
            subtransactions,
            split_order,
            original_memo,
        )

    return _confirm_and_apply_update(
        transaction_id,
        updated_payload_dict,
        category_id_map,
        ynab_client,
        dry_run,
    )


# --- Main Script Logic ---


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="ynab-amazon-categorizer",
        description=(
            "Match Amazon orders to YNAB transactions with item-level memos "
            "and guided categorization."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview updates without sending any changes to YNAB.",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help=(
            "Non-interactive: auto-set memos (items + order link) for "
            "confidently matched transactions and leave categories unchanged. "
            "Combine with --dry-run to preview."
        ),
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help=(
            "Interactively create or replace the .env file in the current "
            "directory (API key, budget, account, Amazon domain), then exit."
        ),
    )
    parser.add_argument(
        "--include-reconciled",
        action="store_true",
        help=(
            "Also surface uncategorized Amazon transactions that are already "
            "reconciled (excluded by default). Category edits don't affect "
            "the reconciled balance. Combine with --dry-run to just print the "
            "suggested categories for manual entry without writing to YNAB."
        ),
    )
    return parser.parse_args(argv)


_FETCH_ERRORS = (YNABAPIError, requests.exceptions.RequestException, OSError)


def _print_run_banners(dry_run: bool, include_reconciled: bool) -> None:
    """Announce the run-level modes that change what the CLI will do."""
    if dry_run:
        print("*** DRY RUN: no changes will be sent to YNAB. ***")
    if include_reconciled:
        print("*** Including already-reconciled transactions. ***")
    if _env_flag("YNAB_SKIP_SPLIT_PROMPT_SINGLE_ITEM"):
        print("*** Skipping split prompt for single-item transactions. ***")


def _run_setup_wizard() -> bool:
    """Run interactive setup for ./.env; return True if a config was saved."""
    try:
        values = run_setup(
            _prompt_line,
            _prompt_secret,
            Path.cwd() / ENV_FILENAME,
            current_amazon_domain=os.getenv("AMAZON_DOMAIN"),
        )
    except _FETCH_ERRORS as exc:
        logger.error("Setup failed: %s", exc)
        print(f"Setup failed: {exc}")
        return False
    return values is not None


def _offer_setup(error: ConfigurationError) -> bool:
    """Offer first-run setup when there is no .env and a user is at the terminal."""
    if (Path.cwd() / ENV_FILENAME).exists() or not sys.stdin.isatty():
        return False
    print(f"\n{error}")
    answer = _prompt_line(
        "No .env file found in this folder. Set one up now? (Y/n): ",
    )
    if answer.strip().lower() not in ("", "y", "yes"):
        return False
    return _run_setup_wizard()


def _load_config(*, interactive: bool = True) -> Config | None:
    """Load and summarize configuration, or report why it could not be loaded."""
    try:
        config = Config.from_env()
    except ConfigurationError as e:
        if not (interactive and _offer_setup(e)):
            logger.error("Configuration error: %s", e)
            print("Please set environment variables or create a .env file.")
            print("Run 'ynab-amazon-categorizer --setup' to create one interactively.")
            print("See README.md for setup instructions.")
            return None
        config = Config.from_env()
    print_config_summary(config)
    return config


def _load_categories(
    ynab_client: YNABClient,
) -> tuple[list[tuple[str, str]], dict[str, str], dict[str, str]] | None:
    """Fetch the usable category list and its name/id maps, or None on failure."""
    print("Fetching categories...")
    try:
        categories_list, category_name_map, category_id_map = (
            ynab_client.get_categories()
        )
    except _FETCH_ERRORS as exc:
        logger.error("Failed to fetch categories: %s", exc)
        print(f"Could not fetch categories: {exc}")
        return None
    if not categories_list:
        print("Exiting due to category fetch error or no usable categories found.")
        return None
    return categories_list, category_name_map, category_id_map


def _collect_parsed_orders(amazon_domain: str = "amazon.ca") -> list[Order] | None:
    """Optionally prompt for pasted Amazon orders data and summarize what parsed."""
    print("\n--- Optional: Amazon Orders Data ---")
    print(
        "You can paste Amazon orders page content to automatically match transactions with order details.",
    )
    print(f"Open {amazon_orders_page_url(amazon_domain)}, select all and copy.")
    provide_orders = _prompt_line(
        "Would you like to provide Amazon orders data? (y/n, default y): ",
    ).lower()
    if provide_orders and provide_orders != "y":
        return None

    parsed_orders = prompt_for_amazon_orders_data()
    if not parsed_orders:
        print("No valid orders found in provided data.")
        return parsed_orders

    charges = prompt_for_amazon_transactions_data(amazon_domain)
    if charges:
        parsed_orders = orders_from_charges(parsed_orders, charges)

    print(f"✓ Parsed {len(parsed_orders)} orders from Amazon data")
    for order in parsed_orders[:3]:
        print(
            f"  - Order {order.order_id}: "
            f"{format_currency_amount(order.total, order.currency)} "
            f"({len(order.items)} items)",
        )
    if len(parsed_orders) > 3:
        print(f"  ... and {len(parsed_orders) - 3} more orders")
    return parsed_orders


def prompt_for_amazon_transactions_data(
    amazon_domain: str = "amazon.ca",
) -> list[AmazonCharge] | None:
    """Optionally prompt for the Amazon transactions page, to match exact charges."""
    print("\n--- Optional: Amazon Transactions Page ---")
    print(
        "Order totals don't always equal what your card was charged (split"
        " shipments, gift cards). Pasting Your Payments > Transactions lets each"
        " YNAB charge match its exact Amazon charge.",
    )
    print(f"Open {amazon_transactions_page_url(amazon_domain)}, select all and copy.")
    provide = _prompt_line(
        "Would you like to paste the Amazon transactions page? (y/n, default y): ",
    ).lower()
    if provide and provide != "y":
        return None

    print("\nPaste Amazon transactions page content:")
    text = get_multiline_input_with_custom_submit("Paste here: ")
    if text is None or not text.strip() or text.strip().lower() == "skip":
        print("Skipping Amazon transactions data; matching by order totals.")
        return None

    charges = parse_transactions_page(text)
    card_charges = [charge for charge in charges if not charge.is_gift_card]
    if not card_charges:
        print("No card charges found in that text; matching by order totals.")
        return None
    refunds = sum(charge.is_refund for charge in card_charges)
    print(
        f"✓ Parsed {len(card_charges)} card transactions"
        f" ({refunds} refunds; {len(charges) - len(card_charges)} gift-card"
        " payments ignored)",
    )
    return charges


def _fetch_transactions(
    ynab_client: YNABClient,
    config: Config,
    include_reconciled: bool,
) -> list[YNABTransaction] | None:
    """Fetch the Amazon transactions needing attention, or None on failure."""
    print("\nFetching transactions...")
    try:
        transactions = fetch_amazon_transactions(
            ynab_client,
            config,
            include_reconciled=include_reconciled,
        )
    except _FETCH_ERRORS as exc:
        logger.error("Failed to fetch transactions: %s", exc)
        print(f"Could not fetch transactions: {exc}")
        return None

    reconciled_count = sum(1 for t in transactions if t.get("cleared") == "reconciled")
    print(
        f"\nFound {len(transactions)} uncategorized Amazon transaction(s) needing attention.",
    )
    if reconciled_count:
        print(f"  ({reconciled_count} of these are already reconciled 🔒)")
    return transactions


def _process_interactively(
    transactions_to_process: list[YNABTransaction],
    parsed_orders: list[Order] | None,
    memo_generator: MemoGenerator,
    ynab_client: YNABClient,
    category_completer: CategoryCompleter,
    category_name_map: dict[str, str],
    category_id_map: dict[str, str],
    dry_run: bool,
) -> None:
    """Walk the transactions through the interactive flow and print a summary."""
    used_order_ids: set[str] = set()
    stats: dict[str, int] = {}
    for i, t in enumerate(transactions_to_process):
        should_continue = process_transaction(
            t,
            i,
            len(transactions_to_process),
            parsed_orders,
            memo_generator,
            ynab_client,
            category_completer,
            category_name_map,
            category_id_map,
            used_order_ids,
            dry_run,
            stats,
        )
        if not should_continue:
            return

    print("\nFinished processing transactions.")
    auto_skipped = stats.get("auto_skipped_no_match", 0)
    if auto_skipped:
        print(
            f"  ({auto_skipped} auto-skipped: no matching order data for that "
            "transaction)",
        )


def _run(argv: list[str] | None = None) -> int:
    """Run the CLI workflow and return a process exit code."""
    args = _parse_args(argv)
    dry_run = args.dry_run

    logging.basicConfig(level=logging.INFO)
    if args.setup:
        return 0 if _run_setup_wizard() else 1
    _print_run_banners(dry_run, args.include_reconciled)

    config = _load_config(interactive=not args.batch)
    if config is None:
        return 1

    ynab_client = YNABClient(config.api_key, config.budget_id)
    memo_generator = MemoGenerator(config.amazon_domain)

    categories = _load_categories(ynab_client)
    if categories is None:
        return 1
    categories_list, category_name_map, category_id_map = categories

    category_completer_instance = CategoryCompleter(categories_list)
    print(f"\nFound {len(categories_list)} usable categories. Completion enabled.")

    parsed_orders = _collect_parsed_orders(config.amazon_domain)

    transactions_to_process = _fetch_transactions(
        ynab_client,
        config,
        args.include_reconciled,
    )
    if transactions_to_process is None:
        return 1

    if args.batch:
        print("\n--- Batch: auto-enriching memos for confident matches ---")
        enriched, skipped, failed = process_batch(
            transactions_to_process,
            parsed_orders,
            memo_generator,
            ynab_client,
            dry_run,
        )
        print(
            f"\nBatch complete: {enriched} enriched, {skipped} skipped "
            f"(no/ambiguous match), {failed} failed.",
        )
        return 0

    _process_interactively(
        transactions_to_process,
        parsed_orders,
        memo_generator,
        ynab_client,
        category_completer_instance,
        category_name_map,
        category_id_map,
        dry_run,
    )
    return 0


def _ensure_utf8_streams() -> None:
    """Ensure standard output and error streams handle Unicode safely."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            with contextlib.suppress(Exception):
                stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    """Run the CLI with clean handling for terminal cancellation."""
    _ensure_utf8_streams()
    try:
        return _run(argv)
    except (EOFError, KeyboardInterrupt):
        print("\nOperation cancelled. No further changes were made.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
