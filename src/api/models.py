"""SQLAlchemy models for the LCP gateway."""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .logging_config import get_logger

logger = get_logger("lcp.models")


class Base(DeclarativeBase):
    pass


# ── Phase 1-3 tables (already live in production) ──────────────────────────

class Request(Base):
    """Per-request cost tracking."""
    __tablename__ = "requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(String, nullable=False)
    profile = Column(String, nullable=False, default="unknown")
    model = Column(String, nullable=False)
    provider = Column(String, nullable=False)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    cache_hit_tokens = Column(Integer, default=0)
    cache_miss_tokens = Column(Integer, default=0)
    cost = Column(Float, default=0.0)
    latency_ms = Column(Integer, default=0)
    success = Column(Integer, default=1)  # 1=success, 0=failure
    error_type = Column(String, nullable=True)
    error_detail = Column(Text, nullable=True)  # full traceback or error message
    tools_blocked = Column(String, nullable=True)  # comma-separated list


# ── API Key Management ────────────────────────────────────────────────────

class ApiKey(Base):
    """Virtual API keys for authentication and spend tracking."""
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key_hash = Column(String, unique=True, nullable=False)
    key_prefix = Column(String, nullable=False)  # first 8 chars for display
    name = Column(String, nullable=False)
    allowed_profiles = Column(String, nullable=True)  # comma-separated or null=all
    spend_limit = Column(Float, default=0.0)  # 0 = unlimited
    total_spend = Column(Float, default=0.0)
    status = Column(String, default="active")  # active, revoked
    created_at = Column(String, nullable=False, default=lambda: datetime.now(timezone.utc).isoformat())
    last_used_at = Column(String, nullable=True)
    expires_at = Column(String, nullable=True)
    revoked_at = Column(String, nullable=True)
    metadata_tags = Column(String, nullable=True)  # JSON string


class ProviderCredential(Base):
    """Encrypted API key for an upstream provider, managed via the UI.

    The raw key is encrypted with Fernet (see src.api.crypto) using the master
    key from ``LCP_SECRET_KEY`` (or the on-disk fallback). Only ciphertext is
    stored here — the gateway config only ever references the env var name.
    """
    __tablename__ = "provider_credentials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String, unique=True, nullable=False, index=True)
    encrypted_key = Column(Text, nullable=False)  # Fernet token (ciphertext)
    created_at = Column(String, nullable=False, default=lambda: datetime.now(timezone.utc).isoformat())
    updated_at = Column(String, nullable=False, default=lambda: datetime.now(timezone.utc).isoformat())


class Budget(Base):
    """Spending budgets per key and/or profile."""
    __tablename__ = "budgets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    key_id = Column(Integer, ForeignKey("api_keys.id"), nullable=True)  # null = global/profile budget
    profile = Column(String, nullable=True)  # null = all profiles
    amount = Column(Float, nullable=False)  # budget cap in USD
    current_spend = Column(Float, default=0.0)
    period = Column(String, default="monthly")  # monthly, total
    threshold_pct = Column(String, default="80")  # comma-separated alert thresholds
    action = Column(String, default="log")  # log, block (hard stop)
    status = Column(String, default="active")  # active, paused, exceeded
    created_at = Column(String, nullable=False, default=lambda: datetime.now(timezone.utc).isoformat())
    last_alert_at = Column(String, nullable=True)


# ── Alerting ────────────────────────────────────────────────────────────────

class Alert(Base):
    """Persisted alert for webhook notifications and UI history."""
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(String, nullable=False, default=lambda: datetime.now(timezone.utc).isoformat())
    dedup_key = Column(String, nullable=False, index=True)
    rule = Column(String, nullable=False)
    severity = Column(String, nullable=False)  # info, warning, critical
    title = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    metadata_json = Column(Text, nullable=True)  # JSON blob
    status = Column(String, default="firing")  # firing, resolved
    acknowledged = Column(Integer, default=0)
    acknowledged_at = Column(String, nullable=True)
    resolved_at = Column(String, nullable=True)


# ── Provider Health / Failover Tracking ────────────────────────────────────

class FailoverEvent(Base):
    """Records a chain fallback: provider A failed → provider B took over."""
    __tablename__ = "failover_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(String, nullable=False, default=lambda: datetime.now(timezone.utc).isoformat())
    profile = Column(String, nullable=False)
    from_provider = Column(String, nullable=False)
    to_provider = Column(String, nullable=False)
    reason = Column(String, nullable=False)  # error_type from the failing provider
    error_message = Column(Text, nullable=True)
    request_id = Column(Integer, ForeignKey("requests.id"), nullable=True)


class ProviderHealth(Base):
    """Persisted circuit-breaker health for a (provider, base_url, profile).

    The in-memory circuit breaker writes through to this table on every
    mutation (``record_success`` / ``record_failure`` / ``reset`` /
    ``force_status``) and reloads it at boot via ``attach_engine``, so live
    provider health — status, failure counts, cooldown timers, manual
    overrides — survives a restart/redeploy instead of resetting to healthy.

    ``tripped_until`` is epoch seconds (so a persisted cooldown stays correct
    across restarts); ``updated_at`` is an ISO timestamp for diagnostics.
    """
    __tablename__ = "provider_health"
    __table_args__ = (
        UniqueConstraint("provider", "base_url", "profile", name="uq_provider_health_key"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String, nullable=False, index=True)
    base_url = Column(String, nullable=False)
    profile = Column(String, nullable=False)
    status = Column(String, nullable=False, default="healthy")  # healthy | degraded | dead
    consecutive_failures = Column(Integer, nullable=False, default=0)
    last_failure = Column(String, nullable=True)  # ISO timestamp
    last_failure_reason = Column(Text, nullable=True)
    last_success = Column(String, nullable=True)  # ISO timestamp
    tripped_until = Column(Float, nullable=True)  # epoch seconds; None = not tripped / indefinite
    manual_override = Column(String, nullable=True)  # None | 'degraded' | 'dead'
    updated_at = Column(String, nullable=False, default=lambda: datetime.now(timezone.utc).isoformat())


class ModelCapability(Base):
    """Per-task capability scores for model routing.

    Populated from public benchmarks (LiveBench, Arena), runtime benchmark
    runs (``lcp_benchmark``), and manual user entry (``manual``).

    ``release_label`` separates scores for different releases of the SAME
    logical model (e.g. ``deepseek-v4-pro`` 2026-06-25 vs 2026-08-13). The
    registry's ``active_release`` picks which release feeds the router.
    """
    __tablename__ = "model_capabilities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    model = Column(String, nullable=False, index=True)
    task_type = Column(String, nullable=False, index=True)
    score = Column(Float, nullable=False)  # 0.0–1.0 normalized
    source = Column(String, nullable=False, default="livebench")  # livebench, arena, gateway_yaml (historical), lcp_benchmark, manual
    benchmark_category = Column(String, nullable=True)  # raw LiveBench category (coding, math, etc.)
    raw_score = Column(Float, nullable=True)  # original score before normalization (e.g. 70.0 out of 100)
    release_label = Column(String, nullable=True, index=True)  # e.g. "2026-08-13"; None = unversioned/legacy
    updated_at = Column(String, nullable=False, default=lambda: datetime.now(timezone.utc).isoformat())


class ModelCapabilitySubtask(Base):
    """Per-subtask LiveBench scores (e.g. theory_of_mind, zebra_puzzle).

    LiveBench's ``all_tasks.csv`` / ``table_<release>.csv`` grades each model
    down to individual tasks (23 tasks across 7 categories). These rows back
    the "Subtask breakdown" panel on the Models page, keyed by the model's
    ``benchmark_key`` with the same ``source`` + ``release_label`` semantics
    as ``ModelCapability``.
    """
    __tablename__ = "model_capability_subtasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    model = Column(String, nullable=False, index=True)  # benchmark_key (logical)
    category = Column(String, nullable=False, index=True)  # reasoning, coding, math, …
    task = Column(String, nullable=False, index=True)  # theory_of_mind, zebra_puzzle, …
    score = Column(Float, nullable=False)  # 0.0–1.0 normalized
    source = Column(String, nullable=False, default="livebench")  # livebench | lcp_benchmark
    raw_score = Column(Float, nullable=True)  # original 0–100
    release_label = Column(String, nullable=True, index=True)
    updated_at = Column(String, nullable=False, default=lambda: datetime.now(timezone.utc).isoformat())


class CapabilityMetric(Base):
    """Imported benchmark metrics — the source of truth for capability scores.

    One row per (schema, release, model, category, task) datum. Top-level
    category scores have ``category`` set and ``task`` NULL; per-subtask
    scores (e.g. theory_of_mind) have both set. Values are 0–100.

    The typed query tables (``model_capabilities``,
    ``model_capability_subtasks``) are MATERIALIZED from these rows on import
    so the router and Models page keep their existing fast query paths.
    """
    __tablename__ = "capability_metrics"

    id = Column(Integer, primary_key=True, autoincrement=True)
    schema_id = Column(String, nullable=False, index=True)  # dataset id, e.g. "livebench"
    release_label = Column(String, nullable=False, index=True)  # snapshot date, e.g. "2026-06-25"
    model = Column(String, nullable=False, index=True)  # logical / benchmark key
    category = Column(String, nullable=True, index=True)  # NULL = top-level rollup
    task = Column(String, nullable=True, index=True)  # NULL = category-level datum
    value = Column(Float, nullable=False)  # 0–100
    source = Column(String, nullable=False, default="livebench")
    updated_at = Column(String, nullable=False, default=lambda: datetime.now(timezone.utc).isoformat())


class ModelRegistryEntry(Base):
    """Explicit model registry: canonical logical model ↔ benchmark key ↔ providers.

    Providers use different model-ID conventions (Command Code prefixes
    ``deepseek/...``, OpenCode uses bare names). This table pins those
    relationships explicitly so the router never has to guess from string
    patterns.

    Each logical model has exactly one row. ``benchmark_key`` is the STABLE,
    release-independent key used inside ``model_capabilities`` (e.g.
    ``deepseek-v4-flash``). ``active_release`` names the CURRENT model version
    (``2026-08-13`` for DeepSeek V4 Pro 0813) whose scores feed the router,
    while ``benchmark_release`` names the LiveBench leaderboard snapshot those
    scores were taken from (``2026-06-25``) — the benchmark date is separate
    from the model version.

    ``provider_mappings_json`` pins the exact provider-side model ID for each
    provider, e.g. ``{"opencode": "deepseek-v4-pro", "commandcode":
    "deepseek/deepseek-v4-pro", "deepseek": "deepseek-v4-pro"}`` — so the same
    logical model exposed by multiple providers resolves to ONE identity and
    ONE scoring regardless of provider naming. The provider keys themselves
    are also the canonical "providers" list shown in the UI.
    """
    __tablename__ = "model_registry"

    id = Column(Integer, primary_key=True, autoincrement=True)
    logical_name = Column(String, unique=True, nullable=False, index=True)
    benchmark_key = Column(String, nullable=False)  # stable key in model_capabilities
    provider_mappings_json = Column(Text, nullable=False, default="{}")  # {provider: provider-side model ID}
    active_release = Column(String, nullable=True)  # CURRENT model version (e.g. 2026-08-13); None = newest
    benchmark_release = Column(String, nullable=True)  # leaderboard snapshot date (e.g. 2026-06-25)
    quantization = Column(String, nullable=True)  # e.g. "Q4_K_M"; None = unquantized
    updated_at = Column(String, nullable=False, default=lambda: datetime.now(timezone.utc).isoformat())


class BenchmarkRun(Base):
    """A LiveBench benchmark execution, tracked as a background job.

    ``target_kind`` is ``provider`` (benchmark the raw model directly against
    its provider) or ``profile`` (future: route the benchmark through an LCP
    profile to measure council / dynamic-routed profiles end-to-end).

    ``target_json`` holds the target spec — ``{"provider": ..., "model": ...}``
    for provider-kind, ``{"profile": ...}`` for profile-kind.
    """
    __tablename__ = "benchmark_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    target_kind = Column(String, nullable=False, default="provider")  # provider | profile
    target_json = Column(Text, nullable=False)  # JSON object
    categories_json = Column(Text, nullable=True)  # JSON array, or null = all categories
    status = Column(String, nullable=False, default="queued")  # queued | running | done | failed
    started_at = Column(String, nullable=True)
    finished_at = Column(String, nullable=True)
    result_json = Column(Text, nullable=True)  # per-category scores + raw output
    error = Column(Text, nullable=True)
    created_at = Column(String, nullable=False, default=lambda: datetime.now(timezone.utc).isoformat())


class SetupState(Base):
    """First-run setup wizard progress.

    One row per installable module step plus a ``wizard`` marker row that
    records when the user skipped the wizard (status=skipped).
    """
    __tablename__ = "setup_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String, unique=True, nullable=False, index=True)  # e.g. provider:deepseek, module:livebench, wizard
    status = Column(String, nullable=False)  # done | skipped | failed | running
    detail = Column(Text, nullable=True)
    updated_at = Column(String, nullable=False, default=lambda: datetime.now(timezone.utc).isoformat())


class Setting(Base):
    """Admin-configurable key/value settings (e.g. cost-cache TTL).

    Values are stored as strings (like the rest of the schema); typed
    accessors live in src.api.cost_cache.SettingsStore.
    """
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String, unique=True, nullable=False, index=True)  # e.g. cost_cache_ttl_minutes
    value = Column(Text, nullable=False)
    updated_at = Column(String, nullable=False, default=lambda: datetime.now(timezone.utc).isoformat())


class CostPluginCacheEntry(Base):
    """Cached cost-plugin scrape results (subscriptions, balances).

    The background refresher writes live-scraped data here; the HTTP
    endpoints read ONLY from this table so a frontend request never
    triggers (or blocks on) a live scrape. ``stale_error`` is set when the
    last refresh failed and we are serving the previous payload.
    """
    __tablename__ = "cost_plugin_cache"
    __table_args__ = (
        UniqueConstraint("provider", "kind", name="uq_cost_plugin_cache_provider_kind"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String, nullable=False, index=True)
    kind = Column(String, nullable=False, index=True)  # subscription | balance
    payload_json = Column(Text, nullable=False)
    fetched_at = Column(String, nullable=False, default=lambda: datetime.now(timezone.utc).isoformat())
    stale_error = Column(Text, nullable=True)


class RoutingDecision(Base):
    """Persisted dynamic-routing decision (survives restarts/rebuilds).

    One row per ``select_step`` decision so the Recent-routing-decisions view
    on the Providers → Routing tab is auditable across restarts.
    """
    __tablename__ = "routing_decisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(String, nullable=False, default=lambda: datetime.now(timezone.utc).isoformat(), index=True)
    profile = Column(String, nullable=False, index=True)
    task = Column(String, nullable=False)
    policy = Column(String, nullable=False)
    action = Column(String, nullable=False)  # prefer | reorder | keep_default | explore | below_min_score
    provider = Column(String, nullable=True)
    model = Column(String, nullable=True)
    score = Column(Float, nullable=True)
    rules_json = Column(Text, nullable=True)   # JSON array of fired rule descriptions
    from_provider = Column(String, nullable=True)
    from_model = Column(String, nullable=True)
    note = Column(Text, nullable=True)

    # ── Classification rationale (routing observability) ────────────────────
    path = Column(String, nullable=True)   # which stage won: keyword:<task> | semantic |
                                           #   agentic_prompt | tool_count | token_count |
                                           #   casual | default
    keyword = Column(String, nullable=True)   # exact TASK_SIGNALS keyword that matched
    intent_text = Column(Text, nullable=True)   # the "newest genuine user instruction" classified
    semantic_json = Column(Text, nullable=True)  # top-5 (task, score) JSON, or None
    min_score = Column(Float, nullable=True)  # semantic gate applied (plugins.router.min_score)
    sem_available = Column(Boolean, nullable=True)  # embedder was up when classified
    # No transcript capture here. `conversation_json` used to hold a trimmed copy of the request
    # messages: 101 MB of a 139 MB DB (73%), all of it duplicating what the harness's own session
    # stores already hold. The text that drove the decision is in `intent_text` above, and the
    # conversation key is `conversation_id`. Stripped in simplify-lcp M7 (alembic 022).


class RoutingJudgment(Base):
    """Human judgment on a recorded routing decision.

    One row per review of a ``routing_decisions`` row (verdict + expected task).
    Accumulates into a labeled real-traffic dataset that can later seed
    regression tests / classifier tuning.
    """
    __tablename__ = "routing_judgments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    decision_id = Column(Integer, nullable=True, index=True)
    profile = Column(String, nullable=False, default="")
    task = Column(String, nullable=False, default="")
    path = Column(String, nullable=True)
    verdict = Column(String, nullable=False)  # correct | wrong | ambiguous
    expected_task = Column(String, nullable=True)
    note = Column(Text, nullable=True)
    judged_at = Column(String, nullable=False,
                       default=lambda: datetime.now(timezone.utc).isoformat(),
                       index=True)


# ── Engine + session factory ───────────────────────────────────────────────

def get_engine(db_path: str):
    """Create a SQLAlchemy engine with WAL mode for SQLite."""
    engine = create_engine(f"sqlite:///{db_path}", echo=False)
    # Enable WAL mode on connect
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def set_wal(dbapi_connection, connection_record):
        dbapi_connection.execute("PRAGMA journal_mode=WAL")

    return engine


def get_session(engine) -> Session:
    """Create a new session."""
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal()
