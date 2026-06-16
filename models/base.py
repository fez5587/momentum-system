"""Models for Milestone 2.

Event and data models for the event-first architecture.
"""

from pydantic import BaseModel, Field


class EventMode(str):
    """Runtime modes for events."""

    REPLAY = "replay"
    PAPER = "paper"
    LIVE = "live"


class BaseEvent(BaseModel):
    """Base event with common fields."""

    timestamp: str = Field(description="Event timestamp (ISO format)")
    mode: EventMode = Field(description="Runtime mode (replay/paper/live)")
    event_type: str = Field(description="Event type identifier")
    correlation_id: str | None = Field(
        default=None, description="Correlation ID for causal chain"
    )
    message: str = Field(description="Human-readable message")
