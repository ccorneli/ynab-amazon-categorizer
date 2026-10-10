"""Interactive first-run setup that writes a local .env file."""

import os
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from .exceptions import YNABAuthError, YNABNotFoundError
from .ynab_client import YNABClient

Ask = Callable[[str], str]


class DataClient(Protocol):
    """The part of YNABClient the wizard needs."""

    def get_data(self, endpoint: str) -> dict[str, Any]:
        """Fetch the ``data`` object for a YNAB API endpoint."""
        ...


ClientFactory = Callable[[str], DataClient]

ENV_FILENAME = ".env"
API_KEY_URL = "https://app.ynab.com/settings/developer"
DEFAULT_AMAZON_DOMAIN = "amazon.ca"
COMMON_AMAZON_DOMAINS = (
    "amazon.com",
    "amazon.ca",
    "amazon.co.uk",
    "amazon.de",
    "amazon.fr",
    "amazon.com.au",
    "amazon.in",
)
_ENV_FILE_MODE = 0o600
_MIN_LEN_TO_SHOW_TAIL = 8


@dataclass(frozen=True)
class SetupValues:
    """Values collected by the setup wizard."""

    api_key: str
    budget_id: str
    account_id: str | None
    amazon_domain: str


def _default_client_factory(api_key: str) -> DataClient:
    return YNABClient(api_key, budget_id="")


def mask_secret(value: str) -> str:
    """Show only the last 4 characters of a secret."""
    if len(value) < _MIN_LEN_TO_SHOW_TAIL:
        return "****"
    return f"...{value[-4:]}"


def _url_path_segments(value: str) -> list[str]:
    if "://" not in value:
        value = f"https://{value}"
    return [segment for segment in urlparse(value).path.split("/") if segment]


def extract_budget_id(value: str) -> str:
    """Return a budget ID from a raw ID or a YNAB URL (https://app.ynab.com/<id>/...)."""
    value = value.strip()
    if "/" not in value:
        return value
    segments = _url_path_segments(value)
    return segments[0] if segments else ""


def extract_account_id(value: str) -> str:
    """Return an account ID from a raw ID or a URL like .../<budget>/accounts/<id>."""
    value = value.strip()
    if "/" not in value:
        return value
    segments = _url_path_segments(value)
    if "accounts" in segments:
        index = segments.index("accounts")
        if index + 1 < len(segments):
            return segments[index + 1]
    return ""


def normalize_amazon_domain(value: str) -> str:
    """Reduce input like 'https://www.Amazon.com/' to 'amazon.com'."""
    domain = value.strip().lower()
    if "://" in domain:
        domain = urlparse(domain).netloc
    domain = domain.split("/", 1)[0]
    return domain.removeprefix("www.")


def _pick(ask: Ask, message: str, options: list[str], default: int) -> int | str:
    """Show numbered options; return a 0-based index or the raw non-numeric text."""
    for number, label in enumerate(options, 1):
        print(f"  {number}. {label}")
    while True:
        raw = ask(f"{message} [{default}]: ").strip()
        if not raw:
            return default - 1
        if not raw.isdigit():
            return raw
        number = int(raw)
        if 1 <= number <= len(options):
            return number - 1
        print(f"Enter a number from 1 to {len(options)}.")


def _confirm(ask: Ask, message: str, *, default: bool) -> bool:
    suffix = "(Y/n)" if default else "(y/N)"
    answer = ask(f"{message} {suffix}: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _prompt_api_key(
    ask_secret: Ask,
    client_factory: ClientFactory,
) -> tuple[str, list[dict[str, Any]]]:
    """Ask for the API key until YNAB accepts it; return it with the budget list."""
    print(f"\nYNAB Personal Access Token (create one at {API_KEY_URL})")
    while True:
        api_key = ask_secret("API key (input hidden): ").strip()
        if not api_key:
            print("An API key is required.")
            continue
        try:
            data = client_factory(api_key).get_data("/budgets")
        except YNABAuthError:
            print("YNAB rejected that API key. Please try again.")
            continue
        return api_key, list(data.get("budgets") or [])


def _prompt_budget(ask: Ask, budgets: list[dict[str, Any]]) -> str:
    """Pick a budget from the list, or accept a pasted budget ID / URL."""
    budgets = sorted(
        budgets,
        key=lambda budget: budget.get("last_modified_on") or "",
        reverse=True,
    )
    options = [budget.get("name") or "(unnamed budget)" for budget in budgets]
    print("\nBudgets:")
    while True:
        if options:
            choice = _pick(ask, "Budget number, or paste a budget ID/URL", options, 1)
        else:
            choice = ask("Budget ID or YNAB budget URL: ")
        if isinstance(choice, int):
            return budgets[choice]["id"]
        budget_id = extract_budget_id(choice)
        if budget_id:
            return budget_id
        print("Could not find a budget ID in that input.")


def _prompt_account(ask: Ask, client: DataClient, budget_id: str) -> str | None:
    """Pick one open account, or all accounts (None)."""
    data = client.get_data(f"/budgets/{budget_id}/accounts")
    accounts = [
        account
        for account in data.get("accounts") or []
        if not account.get("closed") and not account.get("deleted")
    ]
    options = ["All accounts", *(a.get("name") or "(unnamed)" for a in accounts)]
    print("\nWhich accounts should be processed?")
    while True:
        choice = _pick(ask, "Account number, or paste an account ID/URL", options, 1)
        if isinstance(choice, int):
            return None if choice == 0 else accounts[choice - 1]["id"]
        account_id = extract_account_id(choice)
        if account_id:
            return account_id
        print("Could not find an account ID in that input.")


def _prompt_budget_and_account(
    ask: Ask,
    client: DataClient,
    budgets: list[dict[str, Any]],
) -> tuple[str, str | None]:
    """Ask for a budget (re-asking if YNAB can't find it), then an account."""
    while True:
        budget_id = _prompt_budget(ask, budgets)
        try:
            return budget_id, _prompt_account(ask, client, budget_id)
        except YNABNotFoundError:
            print(f"YNAB has no budget with ID {budget_id}. Please try again.")


def _prompt_amazon_domain(ask: Ask, current: str) -> str:
    """Confirm which Amazon site order links in memos should point to."""
    options = list(COMMON_AMAZON_DOMAINS)
    if current not in options:
        options.insert(0, current)
    print("\nWhich Amazon site do you order from? (used for order links in memos)")
    while True:
        choice = _pick(
            ask,
            "Number, or type a domain (e.g. amazon.es)",
            options,
            options.index(current) + 1,
        )
        domain = (
            options[choice]
            if isinstance(choice, int)
            else normalize_amazon_domain(choice)
        )
        if domain.startswith("amazon."):
            return domain
        print("That doesn't look like an Amazon domain (expected e.g. amazon.com).")


def render_env(values: SetupValues) -> str:
    """Render collected values as .env file content."""
    return (
        "# YNAB Amazon Categorizer configuration (created by setup).\n"
        "# Contains your API key: keep this file private and out of git.\n"
        f"YNAB_API_KEY={values.api_key}\n"
        f"YNAB_BUDGET_ID={values.budget_id}\n"
        f"YNAB_ACCOUNT_ID={values.account_id or 'none'}\n"
        f"AMAZON_DOMAIN={values.amazon_domain}\n"
    )


def write_env_file(path: Path, content: str) -> None:
    """Write content to path, readable and writable only by the owner."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _ENV_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as env_file:
        env_file.write(content)
    path.chmod(_ENV_FILE_MODE)


def _print_summary(values: SetupValues, path: Path) -> None:
    print("\n--- Setup summary ---")
    print(f"  API key:       {mask_secret(values.api_key)}")
    print(f"  Budget ID:     {values.budget_id}")
    print(f"  Account:       {values.account_id or 'All accounts'}")
    print(f"  Amazon domain: {values.amazon_domain}")
    print(f"  File:          {path}")


def run_setup(  # noqa: PLR0913
    ask: Ask,
    ask_secret: Ask,
    env_path: Path,
    *,
    current_amazon_domain: str = DEFAULT_AMAZON_DOMAIN,
    client_factory: ClientFactory = _default_client_factory,
    environ: MutableMapping[str, str] = os.environ,
) -> SetupValues | None:
    """Prompt for configuration, write env_path, and export the values.

    Returns None if the user declines to overwrite or save.
    """
    print("\n=== YNAB Amazon Categorizer setup ===")
    if env_path.exists() and not _confirm(
        ask,
        f"{env_path} already exists. Overwrite it?",
        default=False,
    ):
        print("Setup cancelled; existing file left unchanged.")
        return None

    api_key, budgets = _prompt_api_key(ask_secret, client_factory)
    budget_id, account_id = _prompt_budget_and_account(
        ask,
        client_factory(api_key),
        budgets,
    )
    amazon_domain = _prompt_amazon_domain(
        ask,
        normalize_amazon_domain(current_amazon_domain) or DEFAULT_AMAZON_DOMAIN,
    )
    values = SetupValues(api_key, budget_id, account_id, amazon_domain)

    _print_summary(values, env_path)
    if not _confirm(ask, "Save this configuration?", default=True):
        print("Setup cancelled; nothing was written.")
        return None

    write_env_file(env_path, render_env(values))
    environ["YNAB_API_KEY"] = values.api_key
    environ["YNAB_BUDGET_ID"] = values.budget_id
    environ["YNAB_ACCOUNT_ID"] = values.account_id or "none"
    environ["AMAZON_DOMAIN"] = values.amazon_domain
    print(f"✓ Saved {env_path}")
    return values
