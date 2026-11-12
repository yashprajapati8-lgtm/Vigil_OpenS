"""All tunables in one place.

Two sources:
  * vigil.yaml describes YOUR site: which services to watch, how they call each
    other, what customers come to do and how that is measured, which fixes are
    allowed on which service, and the autonomy level. See load_site().
  * Environment variables tune the engine itself (timings, thresholds, secrets).

Without a vigil.yaml, VIGIL watches the built-in demo shop.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("vigil.config")

# Fixes this build can execute (see policy.py). `wait` is always allowed.
ACTIONS = ("restart", "rollback", "scale_out")
AUTONOMY_LEVELS = ("L0", "L1", "L2", "L3")
DEFAULT_CONFIG_PATH = "/etc/vigil/vigil.yaml"


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    prometheus_url: str = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
    data_dir: Path = Path(os.getenv("VIGIL_DATA_DIR", "/data"))

    tick_seconds: float = _f("VIGIL_TICK_SECONDS", 5)
    rate_window: str = os.getenv("VIGIL_RATE_WINDOW", "20s")
    query_timeout_s: float = _f("VIGIL_QUERY_TIMEOUT_S", 3)
    # A sample older than this many ticks means telemetry is stale: no decisions.
    stale_after_ticks: int = _i("VIGIL_STALE_AFTER_TICKS", 3)

    # ---- the site (vigil.yaml; these defaults are the built-in demo shop)
    site_name: str = "Demo shop"
    # Services VIGIL watches for anomalies. The entry is the customers' view (the
    # storefront): VIGIL measures impact and verifies recovery there, never acts on it.
    monitored: tuple[str, ...] = ("payment", "inventory")
    user_facing: str = "storefront"
    # Calls VIGIL knows about before seeing them in metrics (the entry has no edge metrics).
    static_edges: tuple[tuple[str, str], ...] = (("storefront", "payment"), ("payment", "inventory"))
    # What customers come to do, in words people read: "12 checkouts failed".
    unit: str = "checkout"
    units: str = "checkouts"
    # How it is measured: a counter of attempts by outcome, and an optional latency histogram.
    user_metric: str = "checkout_requests_total"
    user_outcome_label: str = "outcome"
    user_ok: tuple[str, ...] = ("ok",)
    user_failed: tuple[str, ...] = ("failed", "dropped")   # other outcomes (e.g. client_error) count as neither
    user_duration_metric: str | None = "checkout_duration_seconds"
    # Fixes allowed per service; a service not listed may get every fix in ACTIONS.
    allowed_actions: dict[str, frozenset[str]] = field(default_factory=dict)
    # One line per service (and the entry) that the dashboard shows under its name.
    about: dict[str, str] = field(default_factory=lambda: {
        "storefront": "Where customers check out", "payment": "Charges the card", "inventory": "Reserves the stock"})
    config_path: str | None = None          # the vigil.yaml these settings came from

    # Baseline learning.
    baseline_seconds: int = _i("VIGIL_BASELINE_SECONDS", 180)
    min_rps_for_rates: float = _f("VIGIL_MIN_RPS", 0.5)

    # Adaptive baseline: after the first 3 minutes, keep learning normal from healthy
    # traffic so it never goes stale (see detector.py, "Adapting").
    adapt: bool = os.getenv("VIGIL_ADAPT", "1") not in ("0", "false", "no", "off")
    adapt_window_s: float = _f("VIGIL_ADAPT_WINDOW_S", 7200)    # learn from the last 2 h of healthy ticks
    adapt_every_s: float = _f("VIGIL_ADAPT_EVERY_S", 600)       # re-learn every 10 min
    adapt_min_s: float = _f("VIGIL_ADAPT_MIN_S", 600)           # ...once it holds 10 min of healthy ticks
    adapt_hold_s: float = _f("VIGIL_ADAPT_HOLD_S", 120)         # after an incident, wait 2 min before learning
    adapt_step_z: float = _f("VIGIL_ADAPT_STEP_Z", 1.0)         # normal moves at most 1 sigma per re-learn
    drift_max_z: float = _f("VIGIL_DRIFT_MAX_Z", 3.0)           # ...and at most 3 sigma from what a human accepted

    # Email alerts (notify.py): off unless an SMTP host and a recipient are set.
    # VIGIL runs on a server nobody sits at; email is how it reaches a person.
    smtp_host: str = os.getenv("VIGIL_SMTP_HOST", "")
    smtp_port: int = _i("VIGIL_SMTP_PORT", 587)
    smtp_user: str = os.getenv("VIGIL_SMTP_USER", "")
    smtp_password: str = os.getenv("VIGIL_SMTP_PASSWORD", "")           # Gmail: an app password
    smtp_security: str = os.getenv("VIGIL_SMTP_SECURITY", "starttls")   # starttls | ssl | none
    alert_to: tuple[str, ...] = tuple(a.strip() for a in os.getenv("VIGIL_ALERT_TO", "").split(",") if a.strip())
    alert_from: str = os.getenv("VIGIL_ALERT_FROM", "")                 # default: the SMTP user
    # Which moments send an email. Each goes at most once per incident.
    alert_on: tuple[str, ...] = tuple(a.strip() for a in os.getenv(
        "VIGIL_ALERT_ON", "acting,awaiting_approval,escalated,resolved,telemetry_degraded,baseline_drift").split(",") if a.strip())
    alert_max_per_hour: int = _i("VIGIL_ALERT_MAX_PER_HOUR", 20)
    console_url: str = os.getenv("VIGIL_CONSOLE_URL", "http://localhost:3000")   # links in emails

    # Detection rule (see detector.py).
    z_flag: float = _f("VIGIL_Z_FLAG", 3.0)
    z_extreme: float = _f("VIGIL_Z_EXTREME", 6.0)
    open_window: int = _i("VIGIL_OPEN_WINDOW", 4)       # look at the last 4 ticks
    open_hits: int = _i("VIGIL_OPEN_HITS", 3)           # 3 of them flagged -> open
    close_clean: int = _i("VIGIL_CLOSE_CLEAN", 4)       # 4 clean in a row -> clear
    # Without impact (no failing checkouts, no errors, no failing calls), a slowdown
    # must persist: 10 of the last 12 ticks (~1 min). Filters host-wide blips such as
    # a CPU-heavy docker build, which slows everything for ~20 s and hurts no one.
    open_window_quiet: int = _i("VIGIL_OPEN_WINDOW_QUIET", 12)
    open_hits_quiet: int = _i("VIGIL_OPEN_HITS_QUIET", 10)
    memory_limit_mb: float = _f("VIGIL_MEMORY_LIMIT_MB", 256)
    leak_horizon_s: float = _f("VIGIL_LEAK_HORIZON_S", 120)

    history_points: int = _i("VIGIL_HISTORY_POINTS", 720)  # 1 h of 5 s ticks for charts

    # Diagnosis.
    diagnose_delay_s: float = _f("VIGIL_DIAGNOSE_DELAY_S", 10)   # let connected symptoms join first
    edge_bad_fail_ratio: float = _f("VIGIL_EDGE_BAD_FAIL_RATIO", 0.05)
    edge_bad_p95_ms: float = _f("VIGIL_EDGE_BAD_P95_MS", 500)
    change_window_s: float = _f("VIGIL_CHANGE_WINDOW_S", 600)    # a deploy this recent is a suspect
    groq_api_key: str = field(default_factory=lambda: os.getenv("GROQ_API_KEY", ""), repr=False)
    groq_model: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    llm_timeout_s: float = _f("VIGIL_LLM_TIMEOUT_S", 10)

    # Decide, act, verify.
    actuator_url: str = os.getenv("ACTUATOR_URL", "http://actuator:8000")
    actuator_token: str = field(default_factory=lambda: os.getenv("ACTUATOR_TOKEN", ""), repr=False)
    autonomy: str = os.getenv("VIGIL_AUTONOMY") or "L2"   # vigil.yaml may set it; a non-empty env var wins
    horizon_min: float = _f("VIGIL_HORIZON_MIN", 5)
    restart_downtime_s: float = _f("VIGIL_RESTART_DOWNTIME_S", 3)
    verify_settle_s: float = _f("VIGIL_VERIFY_SETTLE_S", 10)
    verify_timeout_s: float = _f("VIGIL_VERIFY_TIMEOUT_S", 90)
    verify_clean_ticks: int = _i("VIGIL_VERIFY_CLEAN_TICKS", 4)
    recovered_fail_ratio: float = _f("VIGIL_RECOVERED_FAIL_RATIO", 0.02)
    scale_in_load_ratio: float = _f("VIGIL_SCALE_IN_LOAD_RATIO", 1.5)   # traffic within 1.5x normal...
    scale_in_calm_ticks: int = _i("VIGIL_SCALE_IN_CALM_TICKS", 12)     # ...for 1 min -> stop the replica
    rollback_ready_s: float = _f("VIGIL_ROLLBACK_READY_S", 40)   # previous version must be healthy by then
    # No fix known and no checkout failing: keep watching this long before paging a human.
    watch_without_impact_s: float = _f("VIGIL_WATCH_WITHOUT_IMPACT_S", 180)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "vigil.sqlite3"

    @property
    def model_dir(self) -> Path:
        return self.data_dir / "models"

    def allowed(self, service: str | None) -> frozenset[str]:
        """The fixes VIGIL may use on this service."""
        return self.allowed_actions.get(service or "", frozenset(ACTIONS))

    @property
    def demand_signal(self) -> str:
        """Evidence name for customer demand: checkout -> checkout_attempts_per_s."""
        return re.sub(r"[^a-z0-9]+", "_", self.unit.lower()).strip("_") + "_attempts_per_s"


# ---- vigil.yaml

class ConfigError(ValueError):
    """vigil.yaml is missing something or says something VIGIL cannot use."""


_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")          # service names: Prometheus/Docker label values
_METRIC = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_WORD = re.compile(r"^[A-Za-z][A-Za-z -]{0,39}$")


def _keys(where: str, obj, required: set[str], optional: set[str]) -> dict:
    if not isinstance(obj, dict):
        raise ConfigError(f"{where}: expected a mapping (key: value), got {type(obj).__name__}")
    unknown = set(obj) - required - optional
    if unknown:
        raise ConfigError(f"{where}: unknown key {sorted(unknown)[0]!r}; "
                          f"allowed: {', '.join(sorted(required | optional))}")
    missing = required - set(obj)
    if missing:
        raise ConfigError(f"{where}: missing {sorted(missing)[0]!r}")
    return obj


def _str(where: str, value, pattern: re.Pattern, what: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise ConfigError(f"{where}: {value!r} is not a valid {what}")
    return value


def _str_list(where: str, value, pattern: re.Pattern, what: str, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ConfigError(f"{where}: expected a {'' if allow_empty else 'non-empty '}list")
    out = tuple(_str(f"{where}[{i}]", v, pattern, what) for i, v in enumerate(value))
    if len(set(out)) != len(out):
        raise ConfigError(f"{where}: lists the same value twice")
    return out


def _about(where: str, value) -> str:
    if not isinstance(value, str) or not 0 < len(value.strip()) <= 60:
        raise ConfigError(f"{where}: expected one short line (1 to 60 characters)")
    return value.strip()


def parse_site(doc) -> dict:
    """Validate a parsed vigil.yaml and return the Settings fields it sets.
    Every mistake is a ConfigError that names the exact key: VIGIL must never
    watch the wrong thing quietly because of a typo."""
    doc = _keys("vigil.yaml", doc, {"version", "customers", "services"}, {"site", "autonomy"})
    if doc["version"] != 1:
        raise ConfigError(f"vigil.yaml: version {doc['version']!r} is not supported (this build reads version 1)")
    out: dict = {}

    if "site" in doc:
        site = _keys("site", doc["site"], set(), {"name"})
        if "name" in site:
            if not isinstance(site["name"], str) or not 0 < len(site["name"]) <= 80:
                raise ConfigError("site.name: expected text of 1 to 80 characters")
            out["site_name"] = site["name"]

    services = doc["services"]
    if not isinstance(services, dict) or not services:
        raise ConfigError("services: list at least one service to watch")
    names = [_str(f"services.{k}", k, _NAME, "service name (lowercase letters, digits, - _ .)") for k in services]

    c = _keys("customers", doc["customers"], {"entry", "calls", "unit", "units", "metric"},
              {"outcome_label", "ok", "failed", "duration_metric", "about"})
    entry = _str("customers.entry", c["entry"], _NAME, "service name")
    if entry in services:
        raise ConfigError(f"customers.entry: {entry!r} is also under services. The entry is where customers "
                          "arrive; VIGIL measures it but never acts on it, so it must not be a watched service")
    entry_calls = _str_list("customers.calls", c["calls"], _NAME, "service name")
    out["user_facing"] = entry
    out["unit"] = _str("customers.unit", c["unit"], _WORD, "word (letters, spaces, hyphens)")
    out["units"] = _str("customers.units", c["units"], _WORD, "word (letters, spaces, hyphens)")
    out["user_metric"] = _str("customers.metric", c["metric"], _METRIC, "Prometheus metric name")
    out["user_outcome_label"] = _str("customers.outcome_label", c.get("outcome_label", "outcome"), _LABEL,
                                     "Prometheus label name")
    ok = _str_list("customers.ok", c.get("ok", ["ok"]), _LABEL, "outcome value")
    failed = _str_list("customers.failed", c.get("failed", ["failed"]), _LABEL, "outcome value")
    if set(ok) & set(failed):
        raise ConfigError(f"customers: outcome {sorted(set(ok) & set(failed))[0]!r} is both ok and failed")
    out["user_ok"], out["user_failed"] = ok, failed
    dur = c.get("duration_metric")
    out["user_duration_metric"] = None if dur is None else _str("customers.duration_metric", dur, _METRIC,
                                                                 "Prometheus metric name")

    about: dict[str, str] = {}
    if "about" in c:
        about[entry] = _about("customers.about", c["about"])
    edges = [(entry, t) for t in entry_calls]
    allowed: dict[str, frozenset[str]] = {}
    for name in names:
        spec = services[name] or {}
        spec = _keys(f"services.{name}", spec, set(), {"calls", "allow", "about"})
        if "about" in spec:
            about[name] = _about(f"services.{name}.about", spec["about"])
        for t in _str_list(f"services.{name}.calls", spec.get("calls", []), _NAME, "service name", allow_empty=True):
            if t == name:
                raise ConfigError(f"services.{name}.calls: a service cannot call itself")
            edges.append((name, t))
        if "allow" in spec:
            acts = _str_list(f"services.{name}.allow", spec["allow"], _LABEL, "action", allow_empty=True)
            bad = [a for a in acts if a not in ACTIONS]
            if bad:
                raise ConfigError(f"services.{name}.allow: unknown action {bad[0]!r}; "
                                  f"allowed: {', '.join(ACTIONS)} (or [] for none)")
            allowed[name] = frozenset(acts)
    for caller, target in edges:
        if target not in services:
            where = "customers.calls" if caller == entry else f"services.{caller}.calls"
            raise ConfigError(f"{where}: {target!r} is not under services. List every service VIGIL should "
                              "follow a call into, so the blame walk can reach it")
    out["monitored"] = tuple(names)
    out["static_edges"] = tuple(edges)
    out["allowed_actions"] = allowed
    out["about"] = about

    if "autonomy" in doc:
        if doc["autonomy"] not in AUTONOMY_LEVELS:
            raise ConfigError(f"autonomy: {doc['autonomy']!r} is not one of {', '.join(AUTONOMY_LEVELS)}")
        out["autonomy"] = doc["autonomy"]
    return out


def load_settings(path: str | None = None) -> Settings:
    """Settings for this process: vigil.yaml for the site, env vars for the engine.
    A path given explicitly (argument or VIGIL_CONFIG) must exist; the default
    path may be missing, then VIGIL watches the built-in demo shop."""
    explicit = path or os.getenv("VIGIL_CONFIG") or None
    target = Path(explicit or DEFAULT_CONFIG_PATH)
    if not target.is_file():
        if explicit:
            raise ConfigError(f"VIGIL_CONFIG points to {target}, which is not a file")
        log.info("no %s: watching the built-in demo shop", target)
        return Settings()
    import yaml  # only needed when a config file is used
    try:
        doc = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{target}: not valid YAML ({exc})") from exc
    fields = parse_site(doc)
    env_autonomy = os.getenv("VIGIL_AUTONOMY")
    if env_autonomy:                       # an explicit env var wins over the file
        if env_autonomy not in AUTONOMY_LEVELS:
            raise ConfigError(f"VIGIL_AUTONOMY={env_autonomy!r} is not one of {', '.join(AUTONOMY_LEVELS)}")
        fields["autonomy"] = env_autonomy
    settings = dataclasses.replace(Settings(), config_path=str(target), **fields)
    log.info("site %r from %s: watching %s; customers %s via %s", settings.site_name, target,
             ", ".join(settings.monitored), settings.units, settings.user_facing)
    return settings


SETTINGS = load_settings()
