"""Schema and configuration boundary cases."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import pytest
from pydantic import ValidationError

from triage.llm import retry_after_seconds
from triage.models import Signal, TriageDecision
from triage.settings import Settings


def test_empty_incident_text_is_valid(signal: dict[str, Any]) -> None:
    signal.update(subject="", description="")
    signal.pop("attachments")
    parsed = Signal.model_validate(signal)
    assert parsed.attachments == []


@pytest.mark.parametrize(
    "url",
    [
        "http://provider.invalid/completions",
        "https://user:password@provider.invalid/completions",
        "https://provider.invalid/completions#secret",
        "not-a-url",
    ],
)
def test_provider_requires_https_without_embedded_secrets(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_url=url)


def test_real_signals_cannot_be_routed_to_none(decision: dict[str, Any]) -> None:
    decision["assigned_team"] = "None"
    with pytest.raises(ValidationError):
        TriageDecision.model_validate(decision)


def test_pure_noise_is_valid_without_operational_actions(decision: dict[str, Any]) -> None:
    decision.update(
        category="Not a Mission Signal",
        priority="P4",
        assigned_team="None",
        needs_escalation=False,
        missing_information=[],
        remediation_steps=[],
        next_best_action="Archive the non-incident notification.",
    )
    assert TriageDecision.model_validate(decision).missing_information == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [("3", 3), ("0.1", 1), ("0", 0), ("-1", None), ("NaN", None), ("bad", None)],
)
def test_numeric_retry_after(value: str, expected: int | None) -> None:
    assert retry_after_seconds(value) == expected


def test_http_date_retry_after() -> None:
    future = datetime.now(UTC) + timedelta(seconds=30)
    delay = retry_after_seconds(format_datetime(future, usegmt=True))
    assert delay is not None and 29 <= delay <= 30
