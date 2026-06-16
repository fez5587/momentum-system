"""Entry cutoff logic."""

from datetime import time
from pydantic import BaseModel, Field


class EntryCutoffConfig(BaseModel):
    """Configuration for entry cutoff rules."""

    no_new_entries_after: time = Field(
        default=time(10, 30), description="Cutoff time for new entries"
    )
    minutes_past_cutoff_max: int = Field(
        default=5, description="Max minutes past cutoff to allow entry"
    )


class EntryCutoffResult(BaseModel):
    """Result of entry cutoff check."""

    passed: bool = Field(description="Whether entry is allowed")
    reason: str | None = Field(default=None, description="Reason if entry is blocked")


def check_entry_cutoff(
    trigger_time: time,
    cutoff_config: EntryCutoffConfig = EntryCutoffConfig(),
) -> EntryCutoffResult:
    """Check if entry is allowed based on cutoff time."""
    cutoff_time = cutoff_config.no_new_entries_after

    if trigger_time < cutoff_time:
        return EntryCutoffResult(passed=True, reason=None)

    minutes_past_cutoff = (trigger_time.hour * 60 + trigger_time.minute) - (
        cutoff_time.hour * 60 + cutoff_time.minute
    )

    if minutes_past_cutoff <= cutoff_config.minutes_past_cutoff_max:
        return EntryCutoffResult(
            passed=True,
            reason=f"{minutes_past_cutoff} min past cutoff (max {cutoff_config.minutes_past_cutoff_max})",
        )

    return EntryCutoffResult(
        passed=False,
        reason=f"{minutes_past_cutoff} min past cutoff (exceeds max {cutoff_config.minutes_past_cutoff_max})",
    )
