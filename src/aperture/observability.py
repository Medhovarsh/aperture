"""Structured logging and metrics.

Two things an operations team asks for before they will run anything: can I see
what it did, and can I alert on it.

Both are dependency-free on purpose. A security control that drags in a logging
framework and a metrics client has widened its own supply chain to buy convenience,
and this one has to install inside networks that will not reach a package index.

**Logging** is JSON to stderr, one object per line, because that is what every log
shipper ingests without a parser. Reason codes appear as fields rather than being
interpolated into a message, so `reason="acl_mismatch"` is queryable instead of
being a substring someone has to grep for.

**Metrics** are Prometheus text format, exposed at ``/metrics``. Labels are drawn
only from values the deployment controls - purposes, action ids, reason codes -
never from user input, because an attacker who can invent label values can blow up
a metrics store's cardinality until it falls over.

What is deliberately never logged: the content of retrieved records, action
arguments, and assertion tokens. Aperture exists to keep a lid on who sees what;
a log that quietly copies every retrieved document into a shipper would be the
largest hole in the product.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Iterator

LOGGER_NAME = "aperture"

#: Fields that must never be logged, whatever a caller passes.
REDACTED_FIELDS = frozenset(
    {"text", "content", "record_text", "arguments", "assertion", "token", "secret", "password"}
)


class JsonFormatter(logging.Formatter):
    """Renders a log record as one JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in getattr(record, "fields", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", stream=None) -> logging.Logger:
    """Install the JSON formatter on the aperture logger.

    Idempotent, so calling it from both a CLI entry point and a server module does
    not double every line.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level.upper())
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger


def log_event(event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured event.

    Sensitive fields are dropped rather than truncated. Truncating retrieved
    content still leaks its beginning, which for a document is usually the part
    that identifies it.
    """
    safe = {k: v for k, v in fields.items() if k not in REDACTED_FIELDS}
    logging.getLogger(LOGGER_NAME).log(level, event, extra={"fields": safe})


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #


class Metrics:
    """A tiny, thread-safe counter and histogram registry.

    Not a general metrics library. It covers exactly what this service needs to
    expose and nothing else, which is why it fits in one class with no dependencies.
    """

    #: Latency buckets in seconds. Chosen around what a governed retrieval costs:
    #: sub-millisecond for a policy denial, tens of milliseconds for a real query.
    BUCKETS = (0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0)

    def __init__(self) -> None:
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = (
            defaultdict(list)
        )
        self._help: dict[str, str] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((str(k), str(v)) for k, v in (labels or {}).items()))

    def increment(
        self, name: str, value: float = 1.0, labels: dict[str, str] | None = None, help_text: str = ""
    ) -> None:
        """Add to a counter."""
        with self._lock:
            self._counters[(name, self._key(labels))] += value
            if help_text:
                self._help.setdefault(name, help_text)

    def observe(
        self, name: str, value: float, labels: dict[str, str] | None = None, help_text: str = ""
    ) -> None:
        """Record a latency or size observation."""
        with self._lock:
            self._histograms[(name, self._key(labels))].append(value)
            if help_text:
                self._help.setdefault(name, help_text)

    @contextmanager
    def time(self, name: str, labels: dict[str, str] | None = None) -> Iterator[None]:
        """Time a block and record it."""
        started = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, time.perf_counter() - started, labels)

    def snapshot(self) -> dict[str, Any]:
        """Current values, for tests and for the readiness endpoint."""
        with self._lock:
            return {
                "counters": {
                    f"{name}{dict(labels) if labels else ''}": value
                    for (name, labels), value in self._counters.items()
                },
                "histograms": {
                    f"{name}{dict(labels) if labels else ''}": len(values)
                    for (name, labels), values in self._histograms.items()
                },
            }

    def reset(self) -> None:
        """Clear everything. Tests only."""
        with self._lock:
            self._counters.clear()
            self._histograms.clear()

    @staticmethod
    def _render_labels(labels: tuple[tuple[str, str], ...]) -> str:
        if not labels:
            return ""
        inner = ",".join(f'{k}="{str(v)}"' for k, v in labels)
        return "{" + inner + "}"

    def render(self) -> str:
        """Render Prometheus text exposition format."""
        lines: list[str] = []
        with self._lock:
            counters = dict(self._counters)
            histograms = {k: list(v) for k, v in self._histograms.items()}
            help_text = dict(self._help)

        for name in sorted({name for name, _ in counters}):
            if name in help_text:
                lines.append(f"# HELP {name} {help_text[name]}")
            lines.append(f"# TYPE {name} counter")
            for (metric, labels), value in sorted(counters.items()):
                if metric == name:
                    lines.append(f"{name}{self._render_labels(labels)} {value:g}")

        for name in sorted({name for name, _ in histograms}):
            if name in help_text:
                lines.append(f"# HELP {name} {help_text[name]}")
            lines.append(f"# TYPE {name} histogram")
            for (metric, labels), values in sorted(histograms.items()):
                if metric != name:
                    continue
                rendered = dict(labels)
                cumulative = 0
                for bucket in self.BUCKETS:
                    cumulative = sum(1 for v in values if v <= bucket)
                    bucket_labels = tuple(sorted({**rendered, "le": str(bucket)}.items()))
                    lines.append(
                        f"{name}_bucket{self._render_labels(bucket_labels)} {cumulative}"
                    )
                infinite = tuple(sorted({**rendered, "le": "+Inf"}.items()))
                lines.append(f"{name}_bucket{self._render_labels(infinite)} {len(values)}")
                lines.append(f"{name}_sum{self._render_labels(labels)} {sum(values):g}")
                lines.append(f"{name}_count{self._render_labels(labels)} {len(values)}")

        return "\n".join(lines) + "\n"


#: Process-wide registry. A single service exposing one /metrics endpoint needs
#: exactly one of these, and passing it through every constructor would add
#: parameters to code whose job has nothing to do with metrics.
metrics = Metrics()


# --------------------------------------------------------------------------- #
# domain helpers
# --------------------------------------------------------------------------- #


def record_search(purpose: str, returned: int, withheld: list[Any], duration: float) -> None:
    """Record one governed retrieval."""
    metrics.increment(
        "aperture_searches_total",
        labels={"purpose": purpose},
        help_text="Governed retrievals served.",
    )
    metrics.increment("aperture_records_returned_total", value=returned, labels={"purpose": purpose})
    for group in withheld:
        metrics.increment(
            "aperture_records_withheld_total",
            value=getattr(group, "count", 0),
            labels={"reason": str(getattr(group, "reason", "unknown"))},
            help_text="Records withheld, by reason code.",
        )
    metrics.observe(
        "aperture_search_duration_seconds",
        duration,
        help_text="Time to serve a governed retrieval.",
    )
    log_event(
        "context.search",
        purpose=purpose,
        returned=returned,
        withheld=[
            {"reason": str(getattr(g, "reason", "")), "count": getattr(g, "count", 0)}
            for g in withheld
        ],
        duration_ms=round(duration * 1000, 2),
    )


def record_action(event: str, action_id: str, outcome: str, amount: float = 0.0, **fields: Any) -> None:
    """Record one action-gateway outcome."""
    metrics.increment(
        "aperture_actions_total",
        labels={"action": action_id, "outcome": outcome},
        help_text="Action outcomes, by action and result.",
    )
    if amount:
        metrics.increment(
            "aperture_action_amount_total",
            value=amount,
            labels={"action": action_id},
            help_text="Cumulative financial impact of executed actions.",
        )
    log_event(event, action_id=action_id, outcome=outcome, amount=amount, **fields)
