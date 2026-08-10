"""
Infrastructure Package

Production engineering components:
- Health checks (liveness, readiness)
- Structured logging (QuantLogger)
- Alert management (event-based, symptom-based)
- Metrics collection

Philosophy (from Jane Street talk):
- Every order is critical - defense in depth
- Event-based monitoring - enumerate every edge case
- Symptom-based alerting - alert on symptoms, not causes
- "Trade Too Good" anomaly detection
"""

from src.strategy.logging import QuantLogger, LogLevel
from src.infra.alerts import (
    AlertManager,
    Alert,
    AlertSeverity,
    AlertChannel,
    SlackChannel,
    PrintChannel,
    FileChannel,
    AnomalyDetector,
    build_default_alert_manager,
)

__all__ = [
    "QuantLogger",
    "LogLevel",
    "AlertManager",
    "Alert",
    "AlertSeverity",
    "AlertChannel",
    "SlackChannel",
    "PrintChannel",
    "FileChannel",
    "AnomalyDetector",
    "build_default_alert_manager",
]
