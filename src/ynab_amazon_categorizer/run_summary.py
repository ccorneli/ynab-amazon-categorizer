"""End-of-run summary of what happened to each transaction."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from .models import format_currency_amount

Status = Literal["updated", "skipped", "failed"]


@dataclass(frozen=True, slots=True)
class TransactionOutcome:
    date: str
    amount: float
    status: Status
    detail: str


@dataclass(slots=True)
class RunSummary:
    """Latest outcome per transaction, in the order transactions were handled."""

    total: int
    dry_run: bool = False
    outcomes: dict[str, TransactionOutcome] = field(default_factory=dict)

    def record(
        self,
        transaction_id: str,
        date: str,
        amount: float,
        status: Status,
        detail: str,
    ) -> None:
        previous = self.outcomes.get(transaction_id)
        if previous and previous.status == "failed" and status == "skipped":
            detail = f"{detail} (after a failed update)"
        self.outcomes[transaction_id] = TransactionOutcome(date, amount, status, detail)

    def lines(self) -> list[str]:
        updated = "would update" if self.dry_run else "updated"
        labels: dict[Status, str] = {
            "updated": updated,
            "skipped": "skipped",
            "failed": "update failed",
        }
        title = "Summary (dry run: nothing sent to YNAB)" if self.dry_run else "Summary"
        lines = [f"--- {title} ---"]
        for outcome in self.outcomes.values():
            amount = format_currency_amount(outcome.amount)
            lines.append(
                f"{outcome.date}  {amount:>10}  {labels[outcome.status]}: "
                f"{outcome.detail}"
            )
        statuses = [outcome.status for outcome in self.outcomes.values()]
        counts = [f"{statuses.count('updated')} {updated}"]
        counts += [
            f"{statuses.count(status)} {labels[status]}"
            for status in ("skipped", "failed")
            if status in statuses
        ]
        not_reached = self.total - len(self.outcomes)
        if not_reached > 0:
            counts.append(f"{not_reached} not reached")
        lines.append(", ".join(counts))
        return lines


def describe_update(
    payload: Mapping[str, Any],
    category_names: Mapping[str, str],
) -> str:
    """Describe a YNAB update payload in one line, e.g. 'split 2 ways (A $1, B $2)'."""
    subtransactions = payload.get("subtransactions")
    if subtransactions:
        parts = [
            f"{category_names.get(sub.get('category_id') or '', 'Uncategorized')}"
            f" {format_currency_amount(abs(sub.get('amount', 0)) / 1000)}"
            for sub in subtransactions
        ]
        return f"split {len(parts)} ways ({', '.join(parts)})"
    category_id = payload.get("category_id")
    if category_id:
        return category_names.get(category_id, "Unknown Category")
    return "memo only"
