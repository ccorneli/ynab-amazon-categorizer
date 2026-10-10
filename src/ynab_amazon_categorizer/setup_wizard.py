"""Interactive first-run setup that writes a local .env file."""

import io
import os
import tempfile
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from dotenv.parser import Binding, parse_stream

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
    current: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Ask for the API key until YNAB accepts it; return it with the budget list.

    A blank answer keeps ``current`` (the key from the existing .env), if any.
    """
    print(f"\nYNAB Personal Access Token (create one at {API_KEY_URL})")
    while True:
        hint = f", Enter to keep {mask_secret(current)}" if current else ""
        api_key = ask_secret(f"API key (input hidden{hint}): ").strip() or current
        if not api_key:
            print("An API key is required.")
            continue
        try:
            data = client_factory(api_key).get_data("/budgets")
        except YNABAuthError:
            print("YNAB rejected that API key. Please try again.")
            if api_key == current:
                current = None
            continue
        return api_key, list(data.get("budgets") or [])


def _prompt_budget(
    ask: Ask,
    budgets: list[dict[str, Any]],
    current: str | None = None,
) -> str:
    """Pick a budget from the list, or accept a pasted budget ID / URL.

    ``current`` (the existing budget ID) is the default choice when given.
    """
    budgets = sorted(
        budgets,
        key=lambda budget: budget.get("last_modified_on") or "",
        reverse=True,
    )
    ids = [budget["id"] for budget in budgets]
    options = [budget.get("name") or "(unnamed budget)" for budget in budgets]
    if current and current not in ids:
        ids.insert(0, current)
        options.insert(0, f"Current budget ({current})")
    default = ids.index(current) + 1 if current else 1
    print("\nBudgets:")
    while True:
        if options:
            choice = _pick(
                ask,
                "Budget number, or paste a budget ID/URL",
                options,
                default,
            )
        else:
            choice = ask("Budget ID or YNAB budget URL: ")
        if isinstance(choice, int):
            return ids[choice]
        budget_id = extract_budget_id(choice)
        if budget_id:
            return budget_id
        print("Could not find a budget ID in that input.")


def _prompt_account(
    ask: Ask,
    client: DataClient,
    budget_id: str,
    current: str | None = None,
) -> str | None:
    """Pick one open account, or all accounts (None).

    ``current`` is the default if it is an open account in this budget.
    """
    data = client.get_data(f"/budgets/{budget_id}/accounts")
    accounts = [
        account
        for account in data.get("accounts") or []
        if not account.get("closed") and not account.get("deleted")
    ]
    options = ["All accounts", *(a.get("name") or "(unnamed)" for a in accounts)]
    ids = [account.get("id") for account in accounts]
    default = ids.index(current) + 2 if current in ids else 1
    print("\nWhich accounts should be processed?")
    while True:
        choice = _pick(
            ask,
            "Account number, or paste an account ID/URL",
            options,
            default,
        )
        if isinstance(choice, int):
            return None if choice == 0 else accounts[choice - 1]["id"]
        account_id = extract_account_id(choice)
        if not account_id:
            print("Could not find an account ID in that input.")
        elif any(account.get("id") == account_id for account in accounts):
            return account_id
        else:
            print(f"{account_id} is not an open account in this budget.")


def _prompt_budget_and_account(
    ask: Ask,
    client: DataClient,
    budgets: list[dict[str, Any]],
    existing: SetupValues | None = None,
) -> tuple[str, str | None]:
    """Ask for a budget (re-asking if YNAB can't find it), then an account."""
    current_budget = existing.budget_id if existing else None
    current_account = existing.account_id if existing else None
    while True:
        budget_id = _prompt_budget(ask, budgets, current_budget)
        try:
            return budget_id, _prompt_account(
                ask,
                client,
                budget_id,
                current_account if budget_id == current_budget else None,
            )
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
        if not domain:
            print("Enter a domain such as amazon.com.")
            continue
        if domain in COMMON_AMAZON_DOMAINS or domain == current:
            return domain
        print(f"{domain} isn't one of the common Amazon sites listed above.")
        if _confirm(
            ask,
            f"Double-check the spelling. Use {domain} anyway?",
            default=False,
        ):
            return domain


def _parse_env(text: str) -> list[Binding]:
    """Parse .env text the same way python-dotenv does when loading it."""
    return list(parse_stream(io.StringIO(text)))


def read_env_file(path: Path) -> tuple[dict[str, str], str]:
    """Read an existing .env.

    Returns the parsed KEY=VALUE pairs and the original text, so the file can
    later be updated in place.
    """
    if not path.is_file():
        return {}, ""
    text = path.read_text(encoding="utf-8")
    values = {
        binding.key: binding.value
        for binding in _parse_env(text)
        if binding.key is not None and binding.value is not None
    }
    return values, text


def _existing_values(file_values: dict[str, str]) -> SetupValues | None:
    """Build SetupValues from a parsed .env, or None if it sets none of them."""
    if not any(key in file_values for key in _MANAGED_KEYS):
        return None
    account_id = file_values.get("YNAB_ACCOUNT_ID", "")
    return SetupValues(
        file_values.get("YNAB_API_KEY", ""),
        file_values.get("YNAB_BUDGET_ID", ""),
        account_id if account_id.lower() not in ("", "none") else None,
        file_values.get("AMAZON_DOMAIN", ""),
    )


def _managed_values(values: SetupValues) -> dict[str, str]:
    return {
        "YNAB_API_KEY": values.api_key,
        "YNAB_BUDGET_ID": values.budget_id,
        "YNAB_ACCOUNT_ID": values.account_id or "none",
        "AMAZON_DOMAIN": values.amazon_domain,
    }


_MANAGED_KEYS = ("YNAB_API_KEY", "YNAB_BUDGET_ID", "YNAB_ACCOUNT_ID", "AMAZON_DOMAIN")


def render_env(values: SetupValues, existing_text: str = "") -> str:
    """Render collected values as .env content.

    With ``existing_text``, managed keys are updated where they already are and
    everything else (comments, blanks, other keys, multiline values) is kept
    unchanged; managed keys that were missing are appended.
    """
    managed = _managed_values(values)
    if not existing_text:
        body = "".join(f"{key}={value}\n" for key, value in managed.items())
        return (
            "# YNAB Amazon Categorizer configuration (created by setup).\n"
            "# Contains your API key: keep this file private and out of git.\n"
            f"{body}"
        )
    output: list[str] = []
    written: set[str] = set()
    for binding in _parse_env(existing_text):
        original = binding.original.string
        key = binding.key
        if key is None or key not in managed:
            output.append(original)
            continue
        if key in written:
            continue  # drop duplicate assignments of a managed key
        prefix = "export " if original.lstrip().startswith("export ") else ""
        output.append(f"{prefix}{key}={managed[key]}\n")
        written.add(key)
    content = "".join(output)
    if not content.endswith("\n"):
        content += "\n"
    return content + "".join(
        f"{key}={value}\n" for key, value in managed.items() if key not in written
    )


def write_env_file(path: Path, content: str) -> None:
    """Atomically write content to path, readable and writable only by the owner.

    The content goes to a fresh temp file in the same directory (mkstemp creates
    it 0600), which then replaces path. Existing permissions are never reused,
    and a symlink at path is replaced instead of followed.
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".env.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as env_file:
            env_file.write(content)
            env_file.flush()
            os.fsync(env_file.fileno())
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


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
    current_amazon_domain: str | None = None,
    client_factory: ClientFactory = _default_client_factory,
    environ: MutableMapping[str, str] = os.environ,
) -> SetupValues | None:
    """Prompt for configuration, write env_path, and export the values.

    Values from an existing env_path are offered as defaults, so pressing Enter
    keeps them. The file is updated in place: comments, blank lines and other
    keys are preserved.
    Returns None if the user declines to update or save.
    """
    print("\n=== YNAB Amazon Categorizer setup ===")
    if env_path.exists() and not _confirm(
        ask,
        f"{env_path} already exists. Update it? (Enter keeps current values)",
        default=False,
    ):
        print("Setup cancelled; existing file left unchanged.")
        return None

    file_values, existing_text = read_env_file(env_path)
    existing = _existing_values(file_values)

    api_key, budgets = _prompt_api_key(
        ask_secret,
        client_factory,
        (existing.api_key or None) if existing else None,
    )
    budget_id, account_id = _prompt_budget_and_account(
        ask,
        client_factory(api_key),
        budgets,
        existing,
    )
    amazon_domain = _prompt_amazon_domain(
        ask,
        normalize_amazon_domain(
            current_amazon_domain or file_values.get("AMAZON_DOMAIN", ""),
        )
        or DEFAULT_AMAZON_DOMAIN,
    )
    values = SetupValues(api_key, budget_id, account_id, amazon_domain)

    _print_summary(values, env_path)
    if not _confirm(ask, "Save this configuration?", default=True):
        print("Setup cancelled; nothing was written.")
        return None

    write_env_file(env_path, render_env(values, existing_text))
    environ["YNAB_API_KEY"] = values.api_key
    environ["YNAB_BUDGET_ID"] = values.budget_id
    environ["YNAB_ACCOUNT_ID"] = values.account_id or "none"
    environ["AMAZON_DOMAIN"] = values.amazon_domain
    print(f"✓ Saved {env_path}")
    return values
