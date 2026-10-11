from ynab_amazon_categorizer.run_summary import RunSummary, describe_update

NAMES = {"c1": "Pets", "c2": "Health"}


def test_dry_run_summary_lists_outcomes_and_counts() -> None:
    summary = RunSummary(total=4, dry_run=True)
    summary.record("t1", "2026-10-04", -32.32, "updated", "split 2 ways")
    summary.record("t2", "2026-10-02", -16.21, "skipped", "no matching order")

    assert summary.lines() == [
        "--- Summary (dry run: nothing sent to YNAB) ---",
        "2026-10-04     -$32.32  would update: split 2 ways",
        "2026-10-02     -$16.21  skipped: no matching order",
        "1 would update, 1 skipped, 2 not reached",
    ]


def test_summary_keeps_latest_outcome_and_notes_failed_update() -> None:
    summary = RunSummary(total=2)
    summary.record("t1", "2026-10-04", -5.0, "failed", "Pets")
    summary.record("t1", "2026-10-04", -5.0, "updated", "Pets")
    summary.record("t2", "2026-10-05", -6.0, "failed", "Pets")
    summary.record("t2", "2026-10-05", -6.0, "skipped", "by you")

    assert summary.lines()[1:] == [
        "2026-10-04      -$5.00  updated: Pets",
        "2026-10-05      -$6.00  skipped: by you (after a failed update)",
        "1 updated, 1 skipped",
    ]


def test_summary_counts_failed_updates() -> None:
    summary = RunSummary(total=1)
    summary.record("t1", "2026-10-04", -5.0, "failed", "Pets")

    assert summary.lines()[-1] == "0 updated, 1 update failed"


def test_describe_split_update() -> None:
    payload = {
        "subtransactions": [
            {"amount": -25300, "category_id": "c1"},
            {"amount": -7020, "category_id": "c2"},
        ]
    }
    assert describe_update(payload, NAMES) == "split 2 ways (Pets $25.30, Health $7.02)"


def test_describe_single_category_and_memo_only() -> None:
    assert describe_update({"category_id": "c1", "memo": "x"}, NAMES) == "Pets"
    assert describe_update({"category_id": "zz"}, NAMES) == "Unknown Category"
    assert describe_update({"memo": "x"}, NAMES) == "memo only"
