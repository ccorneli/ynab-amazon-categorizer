"""Tests for the interactive .env setup wizard."""

import stat
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pytest

from ynab_amazon_categorizer.exceptions import YNABAuthError, YNABNotFoundError
from ynab_amazon_categorizer.setup_wizard import (
    SetupValues,
    extract_account_id,
    extract_budget_id,
    mask_secret,
    normalize_amazon_domain,
    render_env,
    run_setup,
)

BUDGET = "12345678-90ab-cdef-1234-567890abcdef"
OTHER_BUDGET = "99999999-90ab-cdef-1234-567890abcdef"
ACCOUNT = "abcdef12-3456-7890-abcd-ef1234567890"
API_KEY = "secret-api-key-1234"

BUDGETS = [
    {"id": OTHER_BUDGET, "name": "Old", "last_modified_on": "2024-01-01T00:00:00Z"},
    {"id": BUDGET, "name": "Main", "last_modified_on": "2026-10-01T00:00:00Z"},
]
ACCOUNTS = [
    {"id": "closed-acct", "name": "Closed", "closed": True, "deleted": False},
    {"id": ACCOUNT, "name": "Visa", "closed": False, "deleted": False},
]


class FakeClient:
    """Stand-in for YNABClient.get_data."""

    def __init__(self, api_key: str, valid_budgets: set[str]) -> None:
        """Accept only API_KEY and the given budget IDs."""
        self.api_key = api_key
        self.valid_budgets = valid_budgets

    def get_data(self, endpoint: str) -> dict[str, Any]:
        """Return canned budgets/accounts or raise like the real API."""
        if self.api_key != API_KEY:
            msg = "bad key"
            raise YNABAuthError(msg, status_code=401)
        if endpoint == "/budgets":
            return {"budgets": BUDGETS}
        budget_id = endpoint.split("/")[2]
        if budget_id not in self.valid_budgets:
            msg = "no budget"
            raise YNABNotFoundError(msg, status_code=404)
        return {"accounts": ACCOUNTS}


def _scripted(answers: Iterable[str]) -> Callable[[str], str]:
    remaining = iter(answers)

    def ask(_message: str) -> str:
        return next(remaining)

    return ask


def _run(
    tmp_path: Path,
    answers: list[str],
    secrets: list[str] | None = None,
    current_amazon_domain: str = "amazon.ca",
) -> tuple[SetupValues | None, dict[str, str], Path]:
    env_path = tmp_path / ".env"
    environ: dict[str, str] = {}
    values = run_setup(
        _scripted(answers),
        _scripted(secrets or [API_KEY]),
        env_path,
        client_factory=lambda key: FakeClient(key, {BUDGET, OTHER_BUDGET}),
        environ=environ,
        current_amazon_domain=current_amazon_domain,
    )
    return values, environ, env_path


@pytest.mark.parametrize(
    "value",
    [
        BUDGET,
        f"  {BUDGET}  ",
        f"https://app.ynab.com/{BUDGET}/budget",
        f"https://app.ynab.com/{BUDGET}/budget/202610",
        f"https://app.ynab.com/{BUDGET}/accounts/{ACCOUNT}",
        f"app.ynab.com/{BUDGET}/budget",
        f"https://app.ynab.com/{BUDGET}/budget?x=1#y",
    ],
)
def test_extract_budget_id(value: str) -> None:
    """Budget ID is taken from a raw ID or any YNAB budget URL."""
    assert extract_budget_id(value) == BUDGET


def test_extract_budget_id_url_without_id() -> None:
    """A bare host has no budget ID."""
    assert extract_budget_id("https://app.ynab.com/") == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (ACCOUNT, ACCOUNT),
        (f"https://app.ynab.com/{BUDGET}/accounts/{ACCOUNT}", ACCOUNT),
        (f"app.ynab.com/{BUDGET}/accounts/{ACCOUNT}/", ACCOUNT),
        (f"https://app.ynab.com/{BUDGET}/budget", ""),
        (f"https://app.ynab.com/{BUDGET}/accounts", ""),
    ],
)
def test_extract_account_id(value: str, expected: str) -> None:
    """Account ID is taken from a raw ID or a YNAB account URL."""
    assert extract_account_id(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("amazon.com", "amazon.com"),
        ("  Amazon.CO.UK ", "amazon.co.uk"),
        ("www.amazon.de", "amazon.de"),
        ("https://www.amazon.com/gp/your-account", "amazon.com"),
    ],
)
def test_normalize_amazon_domain(value: str, expected: str) -> None:
    """Domains are lowercased and stripped of scheme, www and path."""
    assert normalize_amazon_domain(value) == expected


def test_mask_secret() -> None:
    """Only the last 4 characters of a long secret are shown."""
    assert mask_secret(API_KEY) == "...1234"
    assert mask_secret("short") == "****"


def test_render_env_all_accounts() -> None:
    """No account is written as 'none'."""
    content = render_env(SetupValues(API_KEY, BUDGET, None, "amazon.com"))
    assert f"YNAB_BUDGET_ID={BUDGET}\n" in content
    assert "YNAB_ACCOUNT_ID=none\n" in content
    assert "AMAZON_DOMAIN=amazon.com\n" in content


def test_run_setup_defaults_write_private_env(tmp_path: Path) -> None:
    """Accepting defaults picks the most recent budget, all accounts, amazon.ca."""
    values, environ, env_path = _run(tmp_path, ["", "", "", ""])

    assert values == SetupValues(API_KEY, BUDGET, None, "amazon.ca")
    content = env_path.read_text(encoding="utf-8")
    assert f"YNAB_API_KEY={API_KEY}\n" in content
    assert f"YNAB_BUDGET_ID={BUDGET}\n" in content
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert environ == {
        "YNAB_API_KEY": API_KEY,
        "YNAB_BUDGET_ID": BUDGET,
        "YNAB_ACCOUNT_ID": "none",
        "AMAZON_DOMAIN": "amazon.ca",
    }


def test_run_setup_numbered_choices(tmp_path: Path) -> None:
    """Numbers select a budget, an open account, and a listed domain."""
    values, _, _ = _run(tmp_path, ["2", "2", "1", "y"])

    assert values == SetupValues(API_KEY, OTHER_BUDGET, ACCOUNT, "amazon.com")


def test_run_setup_accepts_budget_and_account_urls(tmp_path: Path) -> None:
    """Pasted YNAB URLs are parsed for the budget and account IDs."""
    values, _, _ = _run(
        tmp_path,
        [
            f"https://app.ynab.com/{OTHER_BUDGET}/budget",
            f"https://app.ynab.com/{OTHER_BUDGET}/accounts/{ACCOUNT}",
            "https://www.amazon.co.uk/",
            "",
        ],
    )

    assert values == SetupValues(API_KEY, OTHER_BUDGET, ACCOUNT, "amazon.co.uk")


def test_run_setup_accepts_raw_budget_id(tmp_path: Path) -> None:
    """A pasted bare budget ID is used as-is."""
    values, _, _ = _run(tmp_path, [OTHER_BUDGET, "", "", ""])

    assert values is not None
    assert values.budget_id == OTHER_BUDGET


def test_run_setup_reasks_unknown_budget(tmp_path: Path) -> None:
    """A budget YNAB can't find is rejected and the question asked again."""
    values, _, _ = _run(tmp_path, ["not-a-real-budget", "1", "", "", ""])

    assert values is not None
    assert values.budget_id == BUDGET


def test_run_setup_reasks_rejected_api_key(tmp_path: Path) -> None:
    """A key YNAB rejects (or a blank one) is asked for again."""
    values, _, _ = _run(tmp_path, ["", "", "", ""], secrets=["", "wrong", API_KEY])

    assert values is not None
    assert values.api_key == API_KEY


def test_run_setup_rejects_non_amazon_domain(tmp_path: Path) -> None:
    """Non-Amazon domains are refused until a valid one is entered."""
    values, _, _ = _run(tmp_path, ["", "", "example.com", "amazon.es", ""])

    assert values is not None
    assert values.amazon_domain == "amazon.es"


def test_run_setup_uses_current_domain_as_default(tmp_path: Path) -> None:
    """An existing AMAZON_DOMAIN is offered as the default."""
    values, _, _ = _run(
        tmp_path,
        ["", "", "", ""],
        current_amazon_domain="amazon.com.mx",
    )

    assert values is not None
    assert values.amazon_domain == "amazon.com.mx"


def test_run_setup_declined_save_writes_nothing(tmp_path: Path) -> None:
    """Answering no at the summary leaves no file and no env vars."""
    values, environ, env_path = _run(tmp_path, ["", "", "", "n"])

    assert values is None
    assert not env_path.exists()
    assert environ == {}


def test_run_setup_keeps_existing_file_unless_confirmed(tmp_path: Path) -> None:
    """An existing .env is only replaced after explicit confirmation."""
    env_path = tmp_path / ".env"
    env_path.write_text("ORIGINAL=1\n", encoding="utf-8")

    values, _, _ = _run(tmp_path, [""])
    assert values is None
    assert env_path.read_text(encoding="utf-8") == "ORIGINAL=1\n"

    values, _, _ = _run(tmp_path, ["y", "", "", "", ""])
    assert values is not None
    assert f"YNAB_BUDGET_ID={BUDGET}" in env_path.read_text(encoding="utf-8")
