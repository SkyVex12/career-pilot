from __future__ import annotations

import copy

import pytest
from copilot_evals.live_answer_judge import accounting
from copilot_evals.sensei_runtime import Budget, budget_reporting


def ledger(*rows):
    budget = object.__new__(Budget)
    budget.entries = [{"status": status, "charged_usd": amount} for status, amount in rows]
    return budget


@pytest.mark.parametrize("status", ["reserved", "usage_unpriced"])
def test_unresolved_reservation_is_not_billed_upper_bound(status):
    budget = ledger(("usage", 0.125), (status, 2.0))
    before = copy.deepcopy(budget.entries)
    result = accounting(budget)
    assert result["cost_usd"] is None
    assert result["known_cost_usd"] == 0.125
    assert result["recorded_price_subtotal_usd"] == 0.125
    assert result["reservation_usd"] == 2.0
    assert result["unresolved_reserved_usd"] == 2.0
    assert result["accounting_total_usd"] == 2.125
    assert result["cost_upper_usd"] is None
    assert result["actual_billed_cost_usd"] is None
    assert budget.entries == before
    assert budget.total == 2.125


def test_missing_unmetered_usage_retains_unknown_total():
    result = accounting(ledger(("usage", 0.125)), missing_usage=1)
    assert result["cost_usd"] is None
    assert result["unmetered_interrupted_requests"] == 1
    assert result["unpriced_or_unresolved_requests"] == 1
    assert result["recorded_price_subtotal_usd"] == 0.125
    assert result["accounting_total_usd"] == 0.125
    assert result["cost_upper_usd"] is None
    assert result["actual_billed_cost_usd"] is None


def test_fully_priced_is_rate_card_observation_not_invoice_bound():
    result = accounting(ledger(("usage", 0.125), ("usage", 0.25)))
    assert result["cost_usd"] == 0.375
    assert result["known_cost_usd"] == 0.375
    assert result["recorded_price_subtotal_usd"] == 0.375
    assert result["accounting_total_usd"] == 0.375
    assert result["reservation_usd"] == 0.0
    assert result["cost_upper_usd"] is None
    assert result["actual_billed_cost_usd"] is None
    assert result["cost_basis"] == "recorded_usage_at_configured_rates_not_invoice"


def test_presubmission_zero_does_not_assert_zero_invoice():
    result = accounting(ledger(("failed_before_submission", 0.0)))
    assert result["cost_usd"] == 0.0  # Legacy rate-card summary remains compatible.
    assert result["recorded_price_subtotal_usd"] == 0.0
    assert result["accounting_total_usd"] == 0.0
    assert result["actual_billed_cost_usd"] is None
    assert result["cost_upper_usd"] is None


def test_empty_report_has_no_unfounded_invoice_claim():
    result = budget_reporting(ledger())
    assert result["recorded_price_subtotal_usd"] == 0.0
    assert result["reservation_usd"] == 0.0
    assert result["accounting_total_usd"] == 0.0
    assert result["actual_billed_cost_usd"] is None
    assert result["cost_upper_usd"] is None


def test_retry_and_unpriced_rows_each_preserve_accounting_amount():
    result = accounting(ledger(("reserved", 2.0), ("usage", 0.25), ("usage_unpriced", 3.0)))
    assert result["requests"] == 3
    assert result["unpriced_or_unresolved_requests"] == 2
    assert result["recorded_price_subtotal_usd"] == 0.25
    assert result["reservation_usd"] == 5.0
    assert result["accounting_total_usd"] == 5.25
    assert result["cost_usd"] is None
    assert result["cost_upper_usd"] is None
