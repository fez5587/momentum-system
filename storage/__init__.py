"""Event model for Milestone 2.

Append-only event store with canonical event types.
"""

from .event_schema import (
    EventType,
    BaseEvent,
    SymbolDiscoveredEvent,
    SymbolStateChangedEvent,
    CriteriaEvaluatedEvent,
    SignalReadyEvent,
    SignalBlockedEvent,
    OrderSubmittedEvent,
    OrderApprovalRequestedEvent,
    OrderApprovedEvent,
    OrderRejectedEvent,
    OrderFilledEvent,
    PositionOpenedEvent,
    PositionClosedEvent,
    RiskRuleTriggeredEvent,
    BrokerHealthChangedEvent,
    SourceHealthChangedEvent,
)

from .event_store import EventStore

__all__ = [
    "EventType",
    "BaseEvent",
    "SymbolDiscoveredEvent",
    "SymbolStateChangedEvent",
    "CriteriaEvaluatedEvent",
    "SignalReadyEvent",
    "SignalBlockedEvent",
    "OrderSubmittedEvent",
    "OrderApprovalRequestedEvent",
    "OrderApprovedEvent",
    "OrderRejectedEvent",
    "OrderFilledEvent",
    "PositionOpenedEvent",
    "PositionClosedEvent",
    "RiskRuleTriggeredEvent",
    "BrokerHealthChangedEvent",
    "SourceHealthChangedEvent",
    "EventStore",
]
