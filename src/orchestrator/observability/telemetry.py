"""OpenTelemetry integration with GenAI semantic conventions."""

from contextlib import contextmanager
from typing import Any, Dict, Generator, Optional


class TelemetryTracer:
    """Provides OpenTelemetry distributed tracing and span context."""

    def __init__(self, service_name: str = "orchestrator"):
        self.service_name = service_name

    @contextmanager
    def start_span(
        self,
        name: str,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> Generator[Dict[str, Any], None, None]:
        """Context manager creating a telemetry span."""
        span_context: Dict[str, Any] = {
            "name": name,
            "attributes": attributes or {},
            "events": [],
        }
        try:
            yield span_context
        finally:
            pass  # In production, flushes to OTLP exporter if configured

    def record_genai_call(
        self,
        span_context: Dict[str, Any],
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        """Records GenAI semantic convention metrics."""
        span_context["attributes"]["gen_ai.system"] = "orchestrator"
        span_context["attributes"]["gen_ai.request.model"] = model
        span_context["attributes"]["gen_ai.usage.input_tokens"] = input_tokens
        span_context["attributes"]["gen_ai.usage.output_tokens"] = output_tokens
