"""Event schema definitions for Milestone 2.

All canonical event types that can be emitted by replay, paper, and live modes.
"""

from enum import Enum
from datetime import datetime
from pydantic import BaseModel, Field


class EventMode(str, Enum):
    """Runtime modes for events."""

    REPLAY = "replay"
    PAPER = "paper"
    LIVE = "live"


class EventType(str, Enum):
    """Canonical event types."""

    SYMBOL_DISCOVERED = "symbol_discovered"
    SYMBOL_STATE_CHANGED = "symbol_state_changed"
    CRITERIA_EVALUATED = "criteria_evaluated"
    SIGNAL_READY = "signal_ready"
    SIGNAL_BLOCKED = "signal_blocked"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_APPROVAL_REQUESTED = "order_approval_requested"
    ORDER_APPROVED = "order_approved"
    ORDER_REJECTED = "order_rejected"
    ORDER_FILLED = "order_filled"
    ORDER_CANCELLED = "order_cancelled"
    POSITION_OPENED = "position_opened"
    POSITION_CLOSED = "position_closed"
    ACCOUNT_SUMMARY_UPDATED = "account_summary_updated"
    ACCOUNT_POSITIONS_UPDATED = "account_positions_updated"
    ACCOUNT_ORDERS_UPDATED = "account_orders_updated"
    RISK_RULE_TRIGGERED = "risk_rule_triggered"
    BROKER_HEALTH_CHANGED = "broker_health_changed"
    SOURCE_HEALTH_CHANGED = "source_health_changed"
    SESSION_SUMMARY = "session_summary"
    MODULE_TICK = "module_tick"
    DATA_VALIDATION = "data_validation"
    SYMBOL_EVALUATION_DETAIL = "symbol_evaluation_detail"


class BaseEvent(BaseModel):
    """Base event with common fields."""

    timestamp: datetime = Field(description="Event timestamp")
    mode: EventMode = Field(description="Runtime mode (replay/paper/live)")
    event_type: EventType = Field(description="Event type identifier")
    correlation_id: str | None = Field(
        default=None, description="Correlation ID for causal chain"
    )
    message: str = Field(description="Human-readable message")
    payload: dict = Field(default_factory=dict, description="Structured event data")


class SymbolDiscoveredEvent(BaseEvent):
    """Event when a symbol is discovered."""

    event_type: EventType = Field(
        default=EventType.SYMBOL_DISCOVERED, description="Event type identifier"
    )
    symbol: str = Field(description="Ticker symbol")
    symbol_data: dict = Field(default_factory=dict, description="Symbol metadata")


class SymbolStateChangedEvent(BaseEvent):
    """Event when symbol state changes."""

    event_type: EventType = Field(
        default=EventType.SYMBOL_STATE_CHANGED, description="Event type identifier"
    )
    symbol: str = Field(description="Ticker symbol")
    previous_state: str | None = Field(default=None, description="Previous state")
    new_state: str = Field(description="New state")
    state_reason: str | None = Field(
        default=None, description="Reason for state change"
    )


class CriteriaEvaluatedEvent(BaseEvent):
    """Event when criteria are evaluated."""

    event_type: EventType = Field(
        default=EventType.CRITERIA_EVALUATED, description="Event type identifier"
    )
    symbol: str = Field(description="Ticker symbol")
    criteria_results: dict = Field(description="Criteria evaluation results")
    total_criteria: int = Field(description="Total number of criteria")
    passed_criteria: int = Field(description="Number of criteria passed")
    success_score_pct: float = Field(description="Success score percentage")


class SignalReadyEvent(BaseEvent):
    """Event when signal is ready."""

    event_type: EventType = Field(
        default=EventType.SIGNAL_READY, description="Event type identifier"
    )
    symbol: str = Field(description="Ticker symbol")
    signal_type: str = Field(description="Type of signal (e.g., first_pullback)")
    confidence: float = Field(description="Signal confidence score")
    signal_data: dict = Field(default_factory=dict, description="Execution metadata")


class SignalBlockedEvent(BaseEvent):
    """Event when signal is blocked."""

    event_type: EventType = Field(
        default=EventType.SIGNAL_BLOCKED, description="Event type identifier"
    )
    symbol: str = Field(description="Ticker symbol")
    blocking_reason: str = Field(description="Reason signal is blocked")
    unmet_criteria: list[str] = Field(
        default_factory=list, description="List of unmet criteria"
    )


class OrderSubmittedEvent(BaseEvent):
    """Event when order is submitted."""

    event_type: EventType = Field(
        default=EventType.ORDER_SUBMITTED, description="Event type identifier"
    )
    order_id: str = Field(description="Order ID")
    symbol: str = Field(description="Ticker symbol")
    side: str = Field(description="Buy or sell")
    quantity: int = Field(description="Order quantity")
    price: float = Field(description="Order price")


class OrderApprovalRequestedEvent(BaseEvent):
    """Event when order approval is requested."""

    event_type: EventType = Field(
        default=EventType.ORDER_APPROVAL_REQUESTED, description="Event type identifier"
    )
    order_id: str = Field(description="Order ID")
    symbol: str = Field(description="Ticker symbol")
    requested_by: str = Field(description="Who requested approval")
    approval_mode: str = Field(description="Approval mode (auto/manual)")
    execution_mode: str = Field(description="Target execution mode")
    execution_request: dict = Field(
        default_factory=dict, description="Pending execution payload"
    )


class OrderApprovedEvent(BaseEvent):
    """Event when order is approved."""

    event_type: EventType = Field(
        default=EventType.ORDER_APPROVED, description="Event type identifier"
    )
    order_id: str = Field(description="Order ID")
    symbol: str = Field(description="Ticker symbol")
    approved_by: str = Field(description="Who approved the order")
    approval_notes: str | None = Field(default=None, description="Approval notes")


class OrderRejectedEvent(BaseEvent):
    """Event when order is rejected."""

    event_type: EventType = Field(
        default=EventType.ORDER_REJECTED, description="Event type identifier"
    )
    order_id: str = Field(description="Order ID")
    symbol: str = Field(description="Ticker symbol")
    rejected_by: str = Field(description="Who rejected the order")
    rejection_reason: str = Field(description="Reason for rejection")


class OrderFilledEvent(BaseEvent):
    """Event when order is filled."""

    event_type: EventType = Field(
        default=EventType.ORDER_FILLED, description="Event type identifier"
    )
    order_id: str = Field(description="Order ID")
    symbol: str = Field(description="Ticker symbol")
    fill_price: float = Field(description="Fill price")
    fill_quantity: int = Field(description="Filled quantity")
    slippage: float | None = Field(default=None, description="Slippage amount")


class OrderCancelledEvent(BaseEvent):
    event_type: EventType = Field(
        default=EventType.ORDER_CANCELLED, description="Event type identifier"
    )
    order_id: str = Field(description="Order ID")
    symbol: str = Field(description="Ticker symbol")
    cancel_reason: str | None = Field(default=None, description="Cancel reason")


class PositionOpenedEvent(BaseEvent):
    """Event when position is opened."""

    event_type: EventType = Field(
        default=EventType.POSITION_OPENED, description="Event type identifier"
    )
    position_id: str = Field(description="Position ID")
    symbol: str = Field(description="Ticker symbol")
    entry_price: float = Field(description="Entry price")
    quantity: int = Field(description="Position quantity")
    stop_loss_price: float = Field(description="Stop loss price")


class PositionClosedEvent(BaseEvent):
    """Event when position is closed."""

    event_type: EventType = Field(
        default=EventType.POSITION_CLOSED, description="Event type identifier"
    )
    position_id: str = Field(description="Position ID")
    symbol: str = Field(description="Ticker symbol")
    exit_price: float = Field(description="Exit price")
    exit_reason: str = Field(description="Reason for position close")
    realized_pnl: float = Field(description="Realized profit/loss")


class AccountSummaryUpdatedEvent(BaseEvent):
    event_type: EventType = Field(
        default=EventType.ACCOUNT_SUMMARY_UPDATED,
        description="Event type identifier",
    )
    broker_name: str = Field(description="Broker identifier")
    account_id: str = Field(description="Broker account identifier")
    account_desc: str = Field(description="Display name for account")
    total_equity: float = Field(description="Total account equity")
    cash_balance: float = Field(description="Available cash balance")
    buying_power: float = Field(description="Buying power")
    net_liquidating_value: float = Field(description="Net liquidation value")


class AccountPositionsUpdatedEvent(BaseEvent):
    event_type: EventType = Field(
        default=EventType.ACCOUNT_POSITIONS_UPDATED,
        description="Event type identifier",
    )
    broker_name: str = Field(description="Broker identifier")
    account_id: str = Field(description="Broker account identifier")
    positions: list[dict] = Field(description="Serialized account positions")


class AccountOrdersUpdatedEvent(BaseEvent):
    event_type: EventType = Field(
        default=EventType.ACCOUNT_ORDERS_UPDATED,
        description="Event type identifier",
    )
    broker_name: str = Field(description="Broker identifier")
    account_id: str = Field(description="Broker account identifier")
    orders: list[dict] = Field(description="Serialized account orders")


class RiskRuleTriggeredEvent(BaseEvent):
    """Event when risk rule is triggered."""

    event_type: EventType = EventType.RISK_RULE_TRIGGERED
    rule_type: str = Field(description="Type of risk rule")
    rule_value: float = Field(description="Value that triggered the rule")
    current_state: dict = Field(description="Current system state when triggered")
    action_taken: str = Field(description="Action taken in response")


class BrokerHealthChangedEvent(BaseEvent):
    """Event when broker health changes."""

    event_type: EventType = Field(
        default=EventType.BROKER_HEALTH_CHANGED, description="Event type identifier"
    )
    broker_name: str = Field(description="Broker identifier")
    previous_health: str = Field(description="Previous health status")
    new_health: str = Field(description="New health status")
    health_reason: str | None = Field(
        default=None, description="Reason for health change"
    )


class SourceHealthChangedEvent(BaseEvent):
    """Event when source health changes."""

    event_type: EventType = EventType.SOURCE_HEALTH_CHANGED
    source_name: str = Field(description="Source identifier")
    source_type: str = Field(description="Source type (broker/market data/news)")
    previous_health: str = Field(description="Previous health status")
    new_health: str = Field(description="New health status")
    health_reason: str | None = Field(
        default=None, description="Reason for health change"
    )


class SessionSummaryEvent(BaseEvent):
    event_type: EventType = Field(default=EventType.SESSION_SUMMARY)
    session_id: str = Field(description="Unique session identifier")
    status: str = Field(description="Session status")
    summary_data: dict = Field(default_factory=dict, description="Session summary data")


class ModuleTickEvent(BaseEvent):
    """Event for module execution telemetry."""

    event_type: EventType = Field(
        default=EventType.MODULE_TICK, description="Event type identifier"
    )
    module: str = Field(description="Module name (ingestion, research, evaluation, etc.)")
    stage: str = Field(description="Stage (started, processing, completed, failed)")
    duration_ms: float = Field(description="Execution duration in milliseconds")
    input_count: int = Field(description="Number of inputs processed")
    output_count: int = Field(description="Number of outputs produced")
    metrics: dict = Field(default_factory=dict, description="Custom module metrics")
    errors: list[dict] = Field(default_factory=list, description="Any errors that occurred")


class DataValidationEvent(BaseEvent):
    """Event for data quality checkpoints."""

    event_type: EventType = Field(
        default=EventType.DATA_VALIDATION, description="Event type identifier"
    )
    module: str = Field(description="Module performing validation")
    validation_type: str = Field(
        description="Type of validation (schema, checksum, business_rules)"
    )
    valid: bool = Field(description="Whether validation passed")
    details: dict = Field(default_factory=dict, description="Validation details")


class SymbolEvaluationDetailEvent(BaseEvent):
    """Event for detailed symbol evaluation metrics at each stage."""

    event_type: EventType = Field(
        default=EventType.SYMBOL_EVALUATION_DETAIL, description="Event type identifier"
    )
    symbol: str = Field(description="Ticker symbol")
    stage: str = Field(
        description="Evaluation stage (bars_loaded, gap_checked, rvol_calculated, etc.)"
    )
    metrics: dict = Field(description="Metrics at this stage")
    passed: bool = Field(description="Whether this stage passed")
    blocking_reason: str | None = Field(
        default=None, description="If failed, why it failed"
    )
