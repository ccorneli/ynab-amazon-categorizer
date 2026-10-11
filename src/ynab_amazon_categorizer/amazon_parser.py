"""Amazon order parsing functionality."""

import logging
import re
from datetime import datetime

from .models import Order

logger = logging.getLogger(__name__)

# Maximum items to extract per order (keeps memos manageable)
MAX_ITEMS_PER_ORDER = 10

ORDER_START_LABEL = r"(?:Order placed|Subscription charged on|Digital order placed)"
ORDER_DATE_PATTERN = r"(?:[A-Za-z]+ \d{1,2}, \d{4}|\d{1,2} [A-Za-z]+ \d{4})"
CURRENCY_PREFIX_PATTERN = r"(?:C(?:A|DN)\$|US\$|[$£€])"
ORDER_ID_PATTERN = r"(?:(?:\d{3}|D\d{2})-\d{7}-\d{7})"

ORDER_HEADER_PATTERN = re.compile(
    rf"{ORDER_START_LABEL}\s*"
    rf"(?P<date>{ORDER_DATE_PATTERN})\s*"
    rf"Total\s*(?P<currency>{CURRENCY_PREFIX_PATTERN})\s*"
    rf"(?P<total>[0-9][0-9,]*(?:\.[0-9]{{1,2}})?)\s*"
    rf".*?Order #\s*(?P<order_id>{ORDER_ID_PATTERN})",
    re.DOTALL | re.IGNORECASE,
)

ORDER_CONTENT_BOUNDARY_PATTERN = re.compile(
    rf"^\s*{ORDER_START_LABEL}\b",
    re.IGNORECASE | re.MULTILINE,
)

ORDER_TAIL_SENTINEL_PATTERN = re.compile(
    r"^\s*(?:"
    r"[←<]?\s*Previous\b.*|"
    r"Next set of slides\b.*|"
    r"Next\s*[→>]?\s*$|"
    r"Sponsored\s*$|"
    rf"Learn more[ \t]*(?:\r?\n)[ \t]*{CURRENCY_PREFIX_PATTERN}\s*\d|"
    r"Top .+ For You\s*$|"
    r"Customers who (?:viewed|bought)\b.*|"
    r"Continue series you\b.*|"
    r"Your Browsing History\b.*|"
    r"Back to top\b.*|"
    r"Get to Know Us\s*$|"
    r"Make Money with Us\s*$|"
    r"Amazon Payment Products\s*$|"
    r"Let Us Help You\s*$"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

# Sanity cap for a detected quantity badge (see _deduplicate_and_badge_filter).
# Above this, a trailing number is more likely a coincidental part of the
# title (e.g. a model number) than a genuine "you bought N of these" badge.
MAX_REASONABLE_BADGE_QTY = 12

# Lines shorter than this are navigation chrome, not product names.
MIN_ITEM_LENGTH = 15

# A line this many words long is descriptive enough to be a product name.
MIN_DESCRIPTIVE_WORDS = 5


def _normalize_markdown_text(text: str) -> str:
    r"""Strip markdown-link and bullet-marker syntax from a full page of text.

    Some order-history copies (e.g. from a markdown-rendering copy tool)
    wrap every line as "* [Visible Text](https://...)". Left as-is, this
    breaks the order-header regex below (a "* " bullet in front of "TOTAL"
    or "ORDER #" stops the \\s*-only gaps in that pattern from matching
    through it) as well as per-item extraction (skip_patterns are anchored
    to the start of the line, so a leading "* [" hides "Buy it again",
    "View", etc., and the raw URL would otherwise get glued onto extracted
    item text). Applied once up front so every downstream check — the order
    header, cancelled-order detection, and item extraction — sees plain text.
    """
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1 ", text)  # [text](url) -> text
    return re.sub(r"(?m)^[ \t]*[-•*]\s*", "", text)  # leading bullet markers


# Amazon sometimes emits an image's alt-text line and the product-link text
# for the *same* item as two separate lines that are reworded/reordered
# rather than character-for-character identical (e.g. "Soft Pink" vs.
# "Soft Fashion Pink", clauses in a different order). A plain string-equality
# or quantity-badge check won't catch that, so near-duplicates are detected
# by token overlap (order-independent) instead. Threshold picked from real
# examples: true alt-text/title pairs for the same item score ~0.85-0.95;
# genuinely different items (even same brand) score well under 0.3.
NEAR_DUPLICATE_JACCARD_THRESHOLD = 0.7

# Real alt-text/title pairs can score as low as ~0.6 (well within range of a
# genuinely different same-brand item's title, e.g. two different sizes of
# the same listing) — text similarity alone can't cleanly separate the two
# cases. But there's a reliable structural marker: in real order-history
# copies, an item's image alt-text line has a leading space, and the
# following (unindented) title-link line for the *same* item never does.
# When that pattern is present, this much lower floor is enough — it's only
# there to rule out two unrelated lines that coincidentally landed adjacent.
ADJACENT_DUPLICATE_JACCARD_FLOOR = 0.3


def _item_token_set(text: str) -> set[str]:
    """Lowercased alphanumeric tokens of an item line, for similarity checks."""
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _differs_only_numerically(a: str, b: str) -> bool:
    """Return whether the only differing tokens between two lines are numbers.

    e.g. "...Bourbon, 38" vs. "...Bourbon, 36" — that's the "same listing,
    different size/quantity/model number" case, a real separate line item,
    not a reworded repeat of the same one, regardless of how similar the
    rest of the text is or whether the two lines are structurally adjacent.
    """
    tokens_a, tokens_b = _item_token_set(a), _item_token_set(b)
    differing_tokens = tokens_a.symmetric_difference(tokens_b)
    return bool(differing_tokens) and all(t.isdigit() for t in differing_tokens)


def _differs_by_single_word_substitution(a: str, b: str) -> bool:
    """Return whether the two lines differ by exactly one swapped token.

    e.g. "...24 oz, Black" vs. "...24 oz, White" — a color/size *word* variant
    of the same listing, i.e. a real separate line item, the word analogue of
    _differs_only_numerically. Restricted to exactly one unique token per side
    because genuine reworded alt-text/title pairs for a single item differ by
    several tokens on each side (real example: an alt/title pair with 4 vs. 7
    unique tokens), and a looser rule would wrongly split those. A prefix
    relationship between the two tokens ("Toy"/"Toys", "Color"/"Colorful")
    is treated as a spelling/plural rewording of the same item, not a variant.
    """
    tokens_a, tokens_b = _item_token_set(a), _item_token_set(b)
    only_a, only_b = tokens_a - tokens_b, tokens_b - tokens_a
    if len(only_a) != 1 or len(only_b) != 1:
        return False
    (token_a,), (token_b,) = only_a, only_b
    return not (token_a.startswith(token_b) or token_b.startswith(token_a))


def _token_overlap(a: str, b: str) -> float:
    """Jaccard similarity of two lines' token sets. 0 if either has no tokens."""
    tokens_a, tokens_b = _item_token_set(a), _item_token_set(b)
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def _is_duplicate_item_pair(
    prev_item: str,
    prev_had_leading_space: bool,
    item: str,
    had_leading_space: bool,
) -> bool:
    """Return whether `item` is a reworded repeat of the preceding kept item.

    Prefers the structural signal (leading-space alt-text line immediately
    followed by a non-indented title line) when present, since it reliably
    separates true duplicates from genuinely different same-brand items in a
    way text similarity alone cannot (real examples overlap: a true
    duplicate can score lower than a real distinct-size variant). Falls back
    to the plain similarity check otherwise. Numeric-only and single-word
    substitutions are size/color variants of the same listing — genuinely
    separate items — and are never treated as duplicates.
    """
    if _differs_only_numerically(prev_item, item):
        return False
    if _differs_by_single_word_substitution(prev_item, item):
        return False
    if prev_had_leading_space and not had_leading_space:
        return _token_overlap(prev_item, item) >= ADJACENT_DUPLICATE_JACCARD_FLOOR
    return _token_overlap(prev_item, item) >= NEAR_DUPLICATE_JACCARD_THRESHOLD


def _resolve_badge_item(item: str, candidate_texts: set[str]) -> tuple[str, int]:
    """Resolve quantity-badge suffix if present, returning (normalized_text, qty)."""
    badge_match = re.search(r"\s+(\d+)$", item)
    if badge_match:
        stripped = item[: badge_match.start()]
        if stripped != item and stripped in candidate_texts:
            qty = int(badge_match.group(1))
            if qty < 1 or qty > MAX_REASONABLE_BADGE_QTY:
                qty = 1
            return stripped, qty
    return item, 1


class AmazonParser:
    """Parses Amazon order data from order history pages."""

    def _remove_cancelled_orders(self, text: str) -> str:
        """Remove cancelled order blocks so their items don't bleed into adjacent orders."""
        parts = re.split(
            rf"(?=^\s*{ORDER_START_LABEL}\b)",
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        kept = []
        for part in parts:
            if (
                re.match(rf"\s*{ORDER_START_LABEL}", part, re.IGNORECASE)
                and "your order was cancelled" in part.lower()
            ):
                continue
            kept.append(part)
        return "".join(kept)

    def _normalize_order_date(self, date_str: str) -> str:
        """Normalize supported English date layouts for the matcher."""
        for date_format in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
            try:
                parsed = datetime.strptime(date_str, date_format)
                return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"
            except ValueError:
                continue
        return date_str

    def parse_orders_page(self, orders_text: str) -> list[Order]:
        """Parse Amazon orders page text to extract order information.

        Orders are kept even when item extraction fails (partial orders)
        so that amount/date matching can still work.
        """
        if not orders_text.strip():
            return []

        orders_text = _normalize_markdown_text(orders_text)

        orders_text = self._remove_cancelled_orders(orders_text)

        orders = []

        order_matches = list(ORDER_HEADER_PATTERN.finditer(orders_text))

        for idx, match in enumerate(order_matches):
            order_date = self._normalize_order_date(match.group("date").strip())
            order_total = float(match.group("total").replace(",", ""))
            order_currency = match.group("currency")
            order_id = match.group("order_id")

            # Find the content after this order until the next order-like block or end
            start_pos = match.end()
            if idx + 1 < len(order_matches):
                end_pos = order_matches[idx + 1].start()
            else:
                end_pos = len(orders_text)
            end_pos = self._find_order_content_end(orders_text, start_pos, end_pos)
            order_content = orders_text[start_pos:end_pos]

            # Extract items from the order content
            items = self.extract_items_from_content(order_content)

            # Always keep the order even without items (partial order)
            order = Order(
                order_id=order_id,
                total=order_total,
                date_str=order_date,
                items=items,
                currency=order_currency,
            )

            if not items:
                logger.info(
                    "Order %s parsed without items (amount=%.2f). "
                    "It can still match by amount/date.",
                    order_id,
                    order_total,
                )

            orders.append(order)

        return orders

    def _find_order_content_end(
        self,
        orders_text: str,
        start_pos: int,
        default_end: int,
    ) -> int:
        """Find the earliest unparsed order-like boundary before the default end."""
        boundary = ORDER_CONTENT_BOUNDARY_PATTERN.search(
            orders_text,
            start_pos,
            default_end,
        )
        if boundary:
            return boundary.start()
        return default_end

    def _trim_footer(self, order_content: str) -> str:
        """Trim at the earliest footer/recommendation-carousel sentinel.

        A full page copy includes real order content first, then Amazon's
        "recommended for you" carousels, "continue reading" carousels,
        browsing history, and the site-wide footer nav — all of which are
        long, mixed-case, multi-word lines that would otherwise pass the
        product-name heuristics below. Cutting at the *first* sentinel found
        (rather than only the copyright line at the very end) removes all of
        that in one go, since none of it is genuine order content.
        """
        footer_sentinel = ORDER_TAIL_SENTINEL_PATTERN.search(order_content)
        if not footer_sentinel:
            footer_sentinel = re.search(
                r"©\s*\d{4}|To move between items",
                order_content,
                re.IGNORECASE,
            )
        if footer_sentinel:
            return order_content[: footer_sentinel.start()]
        return order_content

    def _get_valid_cleaned_item(self, line: str) -> str | None:
        """Check if a line matches product name criteria and return the cleaned string, or None."""
        line = line.strip()
        if not line or len(line) < MIN_ITEM_LENGTH:
            return None

        # Normalize markdown link formatting before any other checks. Some
        # order-history copies (e.g. from a markdown-rendering copy tool)
        # wrap every line as "* [Visible Text](https://...)". Left as-is,
        # the leading "* [" defeats the line-start-anchored skip_patterns
        # below (e.g. "* [Buy it again](...)" no longer starts with "Buy it
        # again"), and the raw URL would otherwise get glued onto extracted
        # item text.
        line = re.sub(r"^[-•*]\s*", "", line)  # leading bullet marker
        line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1 ", line)  # [text](url) -> text
        line = re.sub(r"\s+", " ", line).strip()
        if not line or len(line) < MIN_ITEM_LENGTH:
            return None

        # Skip common UI elements and delivery status lines
        skip_patterns = [
            r"^(Buy it again|Track package|View|Return|Write|Get|Share|Leave|Ask)\b",
            r"^(Delivered|Arriving|Now arriving|Auto-delivered|Package was)",
            (
                r"^(Your package|Your order|Your item|Your shipment|Your refund"
                r"|Your replacement|Your delivery|Your return)\b"
            ),
            (
                r"^(Return items:|Return or replace|Refund issued|Refund:|Returned"
                r"|Return started)"
            ),
            (
                r"^(Subscribe & Save|Subscribe now|Skip this delivery|Deliver every"
                r"|Change delivery|Manage subscription|Edit delivery|Set up now)"
            ),
            r"^\d+\.?\d* out of \d+ stars",
            r"^FREE|^Today by|^Get it|^List:|^Was:|^Limited-time deal",
            r"^\$\d+\.\d+|\(\$\d+\.\d+",
            r"^\d+ sustainability features?$",
            r"^(Ship to|Order #|View order|Invoice)",
        ]

        if any(re.match(pattern, line, re.IGNORECASE) for pattern in skip_patterns):
            return None

        # All-caps lines (e.g. shouty UI labels) are skipped too, but this
        # check must stay case-sensitive — matched under the shared
        # re.IGNORECASE above, "[A-Z]" would also match lowercase letters and
        # wrongly reject any ordinary product title made up of only letters
        # and spaces (no digits/punctuation).
        if re.match(r"^[A-Z\s]+$", line):
            return None

        # Look for product names - they usually contain specific patterns
        has_product_pattern = (
            any(
                re.search(rf"\b{re.escape(word)}\b", line, re.IGNORECASE)
                for word in [
                    "pack",
                    "count",
                    "size",
                    "oz",
                    "ml",
                    "lbs",
                    "kg",
                    "inch",
                    "cm",
                ]
            )
            or re.search(
                r"[A-Z][a-z].*[A-Z]",
                line,
            )  # Mixed case indicating product names
            or len(line.split()) >= MIN_DESCRIPTIVE_WORDS
        )  # Long descriptive lines

        if not has_product_pattern:
            return None

        # Whitespace/bullet normalization already happened above.
        cleaned_line = line

        # Skip if it looks like navigation or common elements. Word-boundary
        # matched (not plain substring) so single words like "cart" or
        # "prime" don't false-positive inside real product words — "cart"
        # is a substring of "Carton"/"Cartridge", "prime" of "Primer",
        # "orders" of "Recorders"/"Borders", etc.
        skip_words = [
            "account",
            "orders",
            "cart",
            "search",
            "hello",
            "browse",
            "prime",
            "shipping",
            "mastercard",
            "your brand",
            "registry & gift",
            "attract and engage",
            "interest-based",
        ]
        if any(
            re.search(rf"\b{re.escape(word)}\b", cleaned_line, re.IGNORECASE)
            for word in skip_words
        ):
            return None

        return cleaned_line

    def _deduplicate_and_badge_filter(
        self,
        candidates: list[tuple[str, bool]],
    ) -> list[str]:
        """Resolve quantity-badge duplicates and drop (near-)duplicate lines.

        ``candidates`` pairs each cleaned line with whether its *raw* source
        line had leading whitespace — see _is_duplicate_item_pair for why
        that matters. Keeps up to MAX_ITEMS_PER_ORDER entries.
        """
        # Amazon shows "Product Name <qty>" and "Product Name" on adjacent lines when
        # qty > 1. Rather than collapsing that pair to a single entry (which would
        # hide the fact that 2+ units were bought and make it impossible to split
        # them into separate line items later), the bare name is repeated once per
        # unit — capped at MAX_REASONABLE_BADGE_QTY so a coincidental trailing
        # number that isn't really a quantity (e.g. part of a model number)
        # doesn't blow up the item list.
        candidate_texts = {text for text, _ in candidates}
        seen: set[str] = set()
        unique_items: list[str] = []
        last_kept_had_leading_space = False

        for item, had_leading_space in candidates:
            normalized, qty = _resolve_badge_item(item, candidate_texts)

            if normalized in seen or len(normalized) <= MIN_ITEM_LENGTH:
                continue
            # Skip a reworded/reordered repeat of the item we *just* kept
            # (e.g. an image alt-text line immediately followed by the
            # product-link text for the same item) rather than treating it
            # as a second, different item.
            if unique_items and _is_duplicate_item_pair(
                unique_items[-1],
                last_kept_had_leading_space,
                normalized,
                had_leading_space,
            ):
                continue

            seen.add(normalized)
            for _ in range(qty):
                if len(unique_items) >= MAX_ITEMS_PER_ORDER:
                    break
                unique_items.append(normalized)
            last_kept_had_leading_space = had_leading_space
            if len(unique_items) >= MAX_ITEMS_PER_ORDER:
                break
        return unique_items

    def extract_items_from_content(self, order_content: str) -> list[str]:
        """Extract item names from order content."""
        order_content = self._trim_footer(order_content)

        candidates: list[tuple[str, bool]] = []
        for line in order_content.split("\n"):
            had_leading_space = line[:1].isspace() if line else False
            cleaned = self._get_valid_cleaned_item(line)
            if cleaned:
                candidates.append((cleaned, had_leading_space))

        return self._deduplicate_and_badge_filter(candidates)
