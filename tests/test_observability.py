"""Structured logging and metrics."""

from __future__ import annotations

import io
import json
import logging

import pytest

from aperture.observability import (
    LOGGER_NAME,
    JsonFormatter,
    Metrics,
    configure_logging,
    log_event,
    metrics,
    record_action,
)


@pytest.fixture()
def captured() -> io.StringIO:
    """Capture the aperture logger's output as text."""
    stream = io.StringIO()
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield stream
    logger.removeHandler(handler)


def lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #


def test_events_are_one_json_object_per_line(captured) -> None:
    log_event("context.search", purpose="customer_support", returned=3)
    entry = lines(captured)[0]
    assert entry["event"] == "context.search"
    assert entry["purpose"] == "customer_support"
    assert entry["returned"] == 3
    assert entry["level"] == "info"
    assert entry["ts"].endswith("Z")


def test_reason_codes_are_fields_not_prose(captured) -> None:
    """A reason in a field is queryable; one interpolated into a message is a grep."""
    log_event("context.withheld", reason="acl_mismatch", count=2)
    entry = lines(captured)[0]
    assert entry["reason"] == "acl_mismatch"
    assert "acl_mismatch" not in entry["event"]


@pytest.mark.parametrize(
    "field", ["text", "content", "arguments", "assertion", "token", "secret", "password"]
)
def test_sensitive_fields_never_reach_the_log(captured, field: str) -> None:
    """A log that copies retrieved documents into a shipper is the largest hole possible.

    Truncating would not be enough: the beginning of a document is usually the part
    that identifies it.
    """
    log_event("context.search", **{field: "SENSITIVE-VALUE"}, purpose="p")
    output = captured.getvalue()
    assert "SENSITIVE-VALUE" not in output
    assert "purpose" in output


def test_configure_logging_is_idempotent() -> None:
    """Called from both a CLI entry point and a server module, it must not double lines."""
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    configure_logging()
    configure_logging()
    configure_logging()
    assert len(logger.handlers) == 1


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #


def test_counters_render_in_prometheus_format() -> None:
    registry = Metrics()
    registry.increment("aperture_test_total", labels={"outcome": "ok"}, help_text="A test counter.")
    registry.increment("aperture_test_total", labels={"outcome": "ok"})
    registry.increment("aperture_test_total", labels={"outcome": "denied"})

    body = registry.render()
    assert "# HELP aperture_test_total A test counter." in body
    assert "# TYPE aperture_test_total counter" in body
    assert 'aperture_test_total{outcome="ok"} 2' in body
    assert 'aperture_test_total{outcome="denied"} 1' in body


def test_histograms_render_buckets_sum_and_count() -> None:
    registry = Metrics()
    for value in (0.002, 0.02, 0.2):
        registry.observe("aperture_latency_seconds", value)

    body = registry.render()
    assert "# TYPE aperture_latency_seconds histogram" in body
    assert 'aperture_latency_seconds_bucket{le="+Inf"} 3' in body
    assert "aperture_latency_seconds_count 3" in body
    assert 'aperture_latency_seconds_bucket{le="0.005"} 1' in body


def test_timing_context_manager_records_an_observation() -> None:
    registry = Metrics()
    with registry.time("aperture_block_seconds"):
        pass
    assert registry.snapshot()["histograms"]["aperture_block_seconds"] == 1


def test_metrics_are_thread_safe() -> None:
    from concurrent.futures import ThreadPoolExecutor

    registry = Metrics()

    def bump(_: int) -> None:
        for _ in range(200):
            registry.increment("aperture_race_total")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(bump, range(8)))

    assert registry.snapshot()["counters"]["aperture_race_total"] == 8 * 200


def test_action_outcomes_are_counted_and_logged(captured) -> None:
    metrics.reset()
    record_action("action.executed", action_id="support.refund", outcome="executed", amount=250.0)
    body = metrics.render()
    assert 'aperture_actions_total{action="support.refund",outcome="executed"} 1' in body
    assert 'aperture_action_amount_total{action="support.refund"} 250' in body
    assert lines(captured)[0]["outcome"] == "executed"


def test_metrics_endpoint_serves_prometheus_text(client) -> None:
    client.post(
        "/api/search",
        json={
            "principal_id": "u_kim",
            "purpose": "customer_support",
            "question": "refund window",
        },
    )
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "aperture_searches_total" in response.text
