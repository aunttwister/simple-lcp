"""Alert manager — webhook notifications and alert history.

Handles configurable alert rules, webhook dispatch, and alert lifecycle
(firing, resolving, acknowledging).
"""

import json
import threading
import time
import urllib.request
import urllib.error
import ssl
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from .logging_config import get_logger
from .models import Alert, get_session

if TYPE_CHECKING:  # pragma: no cover — runtime import only for type hints
    from .component import Component
    from .runtime import Runtime
else:
    from .component import Component
    from .runtime import Runtime

logger = get_logger("lcp.alerts")


class AlertManager:
    """Manages alert rules and webhook dispatch."""

    def __init__(self, engine=None):
        self._engine = engine
        self._config: dict = {
            "webhook_url": "",
            "webhook_secret": "",
            "enabled": False,
            "rules": {
                "budget_breach": {"enabled": True, "min_severity": "warning"},
                "provider_dead": {"enabled": True, "min_severity": "critical"},
                "provider_degraded": {"enabled": True, "min_severity": "warning"},
                "error_spike": {"enabled": False, "min_severity": "warning", "threshold": 10, "window_minutes": 5},
                "circuit_breaker_trip": {"enabled": True, "min_severity": "warning"},
                "circuit_breaker_recovery": {"enabled": True, "min_severity": "info"},
                # Dependency-level rules: a profile left with no route after a
                # provider removal, and a logical model left with no provider.
                "profile_invalid": {"enabled": True, "min_severity": "critical"},
                "registry_orphan": {"enabled": True, "min_severity": "warning"},
            },
        }
        self._alert_history: list[dict] = []  # in-memory cache (bounded)
        self._active_alerts: dict[str, dict] = {}  # dedup_key -> alert
        self._last_fire: dict[str, float] = {}  # dedup_key -> timestamp for cooldown
        self._cooldown_seconds = 300  # 5 minutes
        # Error spike tracking
        self._error_counts: list[tuple[float, int]] = []  # (timestamp, count)

    @property
    def config(self) -> dict:
        return self._config

    def update_config(self, updates: dict) -> dict:
        """Update alert configuration. Returns new config."""
        if "webhook_url" in updates:
            self._config["webhook_url"] = updates["webhook_url"]
        if "webhook_secret" in updates:
            self._config["webhook_secret"] = updates["webhook_secret"]
        if "enabled" in updates:
            self._config["enabled"] = updates["enabled"]
        if "rules" in updates:
            for rule_name, rule_updates in updates["rules"].items():
                if rule_name in self._config["rules"]:
                    self._config["rules"][rule_name].update(rule_updates)
        if "cooldown_seconds" in updates:
            self._cooldown_seconds = int(updates["cooldown_seconds"])
        logger.info("alert_config_updated")
        return self._config

    def list_alerts(self, limit: int = 50, status: str | None = None) -> list[dict]:
        """List alert history, newest first.

        Reads from DB when an engine is bound; falls back to the in-memory
        cache otherwise (backward compatible with engine-less usage).
        """
        if self._engine is None:
            alerts = self._alert_history
            if status:
                alerts = [a for a in alerts if a.get("status") == status]
            return sorted(alerts, key=lambda a: a.get("timestamp", ""), reverse=True)[:limit]
        try:
            with get_session(self._engine) as session:
                query = session.query(Alert).order_by(Alert.timestamp.desc())
                if status:
                    query = query.filter(Alert.status == status)
                rows = query.limit(limit).all()
                return [_alert_to_dict(r) for r in rows]
        except Exception as e:
            logger.error("alert_list_failed", error=str(e))
            return []

    def acknowledge(self, alert_id: str) -> bool:
        """Acknowledge an alert by its dedup key."""
        now = datetime.now(timezone.utc).isoformat()
        if alert_id in self._active_alerts:
            self._active_alerts[alert_id]["acknowledged"] = True
            self._active_alerts[alert_id]["acknowledged_at"] = now
        if self._engine is not None:
            try:
                with get_session(self._engine) as session:
                    alert = session.query(Alert).filter(Alert.dedup_key == alert_id).first()
                    if alert:
                        alert.acknowledged = 1
                        alert.acknowledged_at = now
                        session.commit()
                        return True
            except Exception as e:
                logger.error("alert_acknowledge_failed", error=str(e))
                return False
        return alert_id in self._active_alerts

    # ── Alert Firing ──────────────────────────────────────────────────────

    def fire(
        self,
        rule: str,
        severity: str,
        title: str,
        message: str,
        dedup_key: str = "",
        metadata: dict | None = None,
    ) -> dict | None:
        """Fire an alert if the rule is enabled and severity meets threshold.
        Returns the alert dict if fired, None if suppressed."""
        rule_cfg = self._config["rules"].get(rule, {})
        if not rule_cfg.get("enabled", True):
            return None

        # Check severity threshold
        severities = {"info": 0, "warning": 1, "critical": 2}
        min_sev = severities.get(rule_cfg.get("min_severity", "info"), 0)
        current_sev = severities.get(severity, 0)
        if current_sev < min_sev:
            return None

        # Cooldown check
        if dedup_key:
            now = time.time()
            last = self._last_fire.get(dedup_key, 0)
            if now - last < self._cooldown_seconds:
                logger.debug("alert_suppressed", rule=rule, dedup_key=dedup_key,
                             cooldown_remaining=int(self._cooldown_seconds - (now - last)))
                return None
            self._last_fire[dedup_key] = now

        timestamp = datetime.now(timezone.utc).isoformat()
        alert = {
            "dedup_key": dedup_key or f"{rule}:{title}:{int(time.time())}",
            "rule": rule,
            "severity": severity,
            "title": title,
            "message": message,
            "metadata": metadata or {},
            "status": "firing",
            "acknowledged": False,
            "timestamp": timestamp,
        }

        self._alert_history.append(alert)
        self._active_alerts[alert["dedup_key"]] = alert

        # Persist to DB
        self._persist_alert(alert)

        # Keep history bounded (in-memory cap only — DB handles its own)
        if len(self._alert_history) > 500:
            self._alert_history = self._alert_history[-500:]

        logger.info("alert_fired", rule=rule, severity=severity, title=title)
        self._dispatch_webhook(alert)
        return alert

    def resolve(self, dedup_key: str) -> bool:
        """Resolve an active alert."""
        now = datetime.now(timezone.utc).isoformat()
        found = dedup_key in self._active_alerts
        if found:
            alert = self._active_alerts.pop(dedup_key)
            alert["status"] = "resolved"
            alert["resolved_at"] = now
        # Update in DB
        if self._engine is not None:
            try:
                with get_session(self._engine) as session:
                    db_alert = session.query(Alert).filter(Alert.dedup_key == dedup_key).first()
                    if db_alert:
                        db_alert.status = "resolved"
                        db_alert.resolved_at = now
                        session.commit()
                        logger.info("alert_resolved", dedup_key=dedup_key)
                        self._dispatch_webhook(
                            {k: v for k, v in db_alert.__dict__.items() if not k.startswith("_")}
                        )
                        return True
                    # Not found in DB — fall back to in-memory result
                    return found
            except Exception as e:
                logger.error("alert_resolve_failed", error=str(e))
                return False
        return found

    def _persist_alert(self, alert: dict) -> None:
        """Write alert to SQLite. Non-blocking — failures are logged not raised."""
        if self._engine is None:
            return
        try:
            with get_session(self._engine) as session:
                db_alert = Alert(
                    timestamp=alert["timestamp"],
                    dedup_key=alert["dedup_key"],
                    rule=alert["rule"],
                    severity=alert["severity"],
                    title=alert["title"],
                    message=alert["message"],
                    metadata_json=json.dumps(alert.get("metadata", {})),
                    status=alert["status"],
                    acknowledged=1 if alert.get("acknowledged") else 0,
                )
                session.add(db_alert)
                session.commit()
        except Exception as e:
            logger.error("alert_persist_failed", error=str(e), dedup_key=alert.get("dedup_key"))

    def get_active_alerts(self) -> list[dict]:
        """Get currently active (firing) alerts."""
        return list(self._active_alerts.values())

    # ── Webhook Dispatch ──────────────────────────────────────────────────

    def _dispatch_webhook(self, alert: dict) -> None:
        """Send alert as webhook POST in a background thread."""
        if not self._config.get("enabled"):
            return
        url = self._config.get("webhook_url", "").strip()
        if not url:
            return

        secret = self._config.get("webhook_secret", "")
        payload = json.dumps({
            "event": "alert",
            "alert": alert,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        def _send():
            try:
                req = urllib.request.Request(
                    url,
                    data=payload.encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "X-LCP-Webhook-Secret": secret,
                        "User-Agent": "LCP-AlertManager/1.0",
                    },
                )
                ctx = ssl.create_default_context()
                with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
                    logger.info("webhook_sent", status=resp.status, dedup_key=alert.get("dedup_key"))
            except Exception as e:
                logger.error("webhook_failed", error=str(e), dedup_key=alert.get("dedup_key"))

        t = threading.Thread(target=_send, daemon=True)
        t.start()

    def test_webhook(self) -> dict:
        """Send a test webhook to verify configuration."""
        test_alert = {
            "dedup_key": "test",
            "rule": "test",
            "severity": "info",
            "title": "Test Alert",
            "message": "This is a test alert from LCP webhook.",
            "status": "firing",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._dispatch_webhook(test_alert)
        return {"ok": True, "webhook_url": self._config.get("webhook_url", "")}


# ── Helpers ─────────────────────────────────────────────────────────────────

def _alert_to_dict(alert: "Alert") -> dict:
    """Convert an Alert ORM object to a dict matching the legacy in-memory format."""
    return {
        "id": alert.id,
        "dedup_key": alert.dedup_key,
        "rule": alert.rule,
        "severity": alert.severity,
        "title": alert.title,
        "message": alert.message,
        "metadata": json.loads(alert.metadata_json) if alert.metadata_json else {},
        "status": alert.status,
        "acknowledged": bool(alert.acknowledged),
        "acknowledged_at": alert.acknowledged_at,
        "resolved_at": alert.resolved_at,
        "timestamp": alert.timestamp,
    }


# ── Module-level singleton ────────────────────────────────────────────────
_alert_manager: AlertManager | None = None


def get_alert_manager(engine=None) -> AlertManager:
    """Get the active alert manager.

    Delegates to the runtime's AlertManagerComponent when bound (engine
    injected at construction — no silent _engine mutation). Otherwise the
    legacy singleton.
    """
    from .runtime import get_runtime
    rt = get_runtime()
    if rt is not None:
        try:
            comp = rt.resolve("alert_manager")
        except Exception:  # noqa: BLE001 — inactive/unbound → legacy
            comp = None
        if comp is not None and getattr(comp, "manager", None) is not None:
            return comp.manager
    global _alert_manager
    if _alert_manager is None:
        _alert_manager = AlertManager(engine)
    elif engine is not None and _alert_manager._engine is None:
        _alert_manager._engine = engine
    return _alert_manager


# ── Component-runtime adapter (Phase C) ────────────────────────────────────


def bind_runtime(rt: "Runtime") -> None:
    """Bind an active Runtime so ``get_alert_manager()`` delegates to it."""
    from .runtime import bind_active_runtime
    bind_active_runtime(rt)


class AlertManagerComponent(Component):
    """The alert manager as a runtime component.

    ``requires=["engine"]`` — engine injected at construction (no getter-side
    mutation of a live instance).
    """

    name = "alert_manager"
    requires = ["engine"]
    provides = ["alert_manager"]

    def __init__(self) -> None:
        super().__init__()
        self.manager: Optional[AlertManager] = None

    @property
    def service(self) -> Optional[AlertManager]:
        return self.manager

    def setup(self, rt: "Runtime") -> Optional[Any]:
        self.manager = AlertManager(rt.resolve("engine"))
        return None
