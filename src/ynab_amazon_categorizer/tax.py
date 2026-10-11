"""Sales-tax policy for split transaction entry."""

import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_TAX_RATE = 0.09
GROCERY_TAX_RATE = 0.045
GROCERY_CATEGORY_KEYWORDS = ("grocery", "groceries")


def _env_tax_rate(var_name: str, fallback: float) -> float:
    """Read a tax-rate override, falling back when it is not numeric."""
    raw = os.getenv(var_name)
    if not raw:
        return fallback
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "Ignoring invalid %s=%r (not a number); using default %.4f",
            var_name,
            raw,
            fallback,
        )
        return fallback


def default_tax_rate() -> float:
    """Return the configured tax rate for non-grocery categories."""
    return _env_tax_rate("YNAB_DEFAULT_TAX_RATE", DEFAULT_TAX_RATE)


def tax_rate_for_category(category_name: str | None) -> float:
    """Return the configured split-entry tax rate for a category."""
    default_rate = default_tax_rate()
    grocery_rate = _env_tax_rate("YNAB_GROCERY_TAX_RATE", GROCERY_TAX_RATE)
    if category_name and any(
        keyword in category_name.lower() for keyword in GROCERY_CATEGORY_KEYWORDS
    ):
        return grocery_rate
    return default_rate
