"""Optional bounded Countme metrics for the review dashboard."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from typing import Protocol


_ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
_HEADERS = "OTEL_EXPORTER_OTLP_HEADERS"
_MAX_COUNT = 10_000
_OPERATION_COUNTS = {
    "queue.refresh": frozenset({"pages", "items"}),
    "review.complete": frozenset(),
    "review.findings": frozenset(),
    "review.failed": frozenset(),
    "review.cancelled": frozenset(),
    "review.incomplete": frozenset(),
    "landing.complete": frozenset(),
    "landing.blocked": frozenset(),
    "landing.failed": frozenset(),
    "landing.incomplete": frozenset(),
}
_STATE_NAMES = frozenset(
    {"reviews.active", "landings.active", "check_workers.active"}
)


class _Exporter(Protocol):
    def record(
        self, name: str, value: float | int, attributes: dict[str, int]
    ) -> None: ...


class ReviewObservability:
    status = "disabled"

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        exporter: _Exporter | None = None,
    ) -> "ReviewObservability":
        environment = os.environ if environment is None else environment
        if not environment.get(_ENDPOINT, "").strip():
            return NoopReviewObservability()
        return OtlpReviewObservability(exporter)

    def operation(
        self, name: str, duration_seconds: float, **counts: int
    ) -> None:
        raise NotImplementedError

    def state(self, name: str, value: int) -> None:
        raise NotImplementedError


class NoopReviewObservability(ReviewObservability):
    def operation(
        self, name: str, duration_seconds: float, **counts: int
    ) -> None:
        return None

    def state(self, name: str, value: int) -> None:
        return None


class OtlpReviewObservability(ReviewObservability):
    status = "ready"

    def __init__(
        self, exporter: _Exporter | None
    ) -> None:
        self._exporter = exporter

    def operation(
        self, name: str, duration_seconds: float, **counts: int
    ) -> None:
        allowed = _OPERATION_COUNTS.get(name)
        if allowed is None or set(counts) != allowed:
            raise ValueError("Countme operation attributes are not allowed")
        if not isinstance(duration_seconds, (int, float)) or isinstance(
            duration_seconds, bool
        ) or not math.isfinite(duration_seconds) or duration_seconds < 0:
            raise ValueError("Countme duration must be a non-negative number")
        self._emit(name, duration_seconds, counts)

    def state(self, name: str, value: int) -> None:
        if name not in _STATE_NAMES:
            raise ValueError("Countme state is not allowed")
        self._emit(name, self._count(value), {})

    def _emit(
        self, name: str, value: float | int, attributes: Mapping[str, int]
    ) -> None:
        if self.status == "unavailable":
            return
        try:
            exporter = self._exporter
            if exporter is None:
                exporter = _OtlpMetricExporter(self._failed)
                self._exporter = exporter
            exporter.record(
                name,
                value,
                {key: self._count(count) for key, count in attributes.items()},
            )
        except Exception:
            self._failed()

    def _failed(self) -> None:
        self.status = "unavailable"

    @staticmethod
    def _count(value: int) -> int:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise ValueError("Countme count must be a non-negative integer")
        return min(value, _MAX_COUNT)


class _OtlpMetricExporter:
    def __init__(self, failed) -> None:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import (
            MetricExportResult,
            MetricExporter,
            PeriodicExportingMetricReader,
        )

        class FailureIsolatingExporter(MetricExporter):
            def __init__(self, exporter) -> None:
                super().__init__(
                    preferred_temporality=exporter._preferred_temporality,
                    preferred_aggregation=exporter._preferred_aggregation,
                )
                self._exporter = exporter

            def export(self, metrics_data, timeout_millis=10_000, **kwargs):
                try:
                    result = self._exporter.export(
                        metrics_data, timeout_millis=timeout_millis, **kwargs
                    )
                except Exception:
                    failed()
                    return MetricExportResult.FAILURE
                if result is MetricExportResult.FAILURE:
                    failed()
                return result

            def shutdown(self, timeout_millis=30_000, **kwargs):
                try:
                    self._exporter.shutdown(
                        timeout_millis=timeout_millis, **kwargs
                    )
                except Exception:
                    failed()

            def force_flush(self, timeout_millis=10_000):
                try:
                    return self._exporter.force_flush(
                        timeout_millis=timeout_millis
                    )
                except Exception:
                    failed()
                    return False

        exporter = OTLPMetricExporter()
        reader = PeriodicExportingMetricReader(
            FailureIsolatingExporter(exporter),
            export_interval_millis=60_000,
            export_timeout_millis=1_000,
        )
        self._provider = MeterProvider(metric_readers=[reader])
        meter = self._provider.get_meter("bluefin.review.countme")
        self._operations = {
            name: meter.create_histogram(
                f"bluefin.review.countme.{name.replace('.', '_')}.seconds",
                unit="s",
            )
            for name in _OPERATION_COUNTS
        }
        self._states = {
            name: meter.create_gauge(
                f"bluefin.review.countme.{name.replace('.', '_')}",
                unit="1",
            )
            for name in _STATE_NAMES
        }

    def record(
        self, name: str, value: float | int, attributes: dict[str, int]
    ) -> None:
        if name in self._operations:
            self._operations[name].record(value, attributes or None)
        else:
            self._states[name].set(value)
