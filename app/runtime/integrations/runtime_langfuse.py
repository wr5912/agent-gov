from __future__ import annotations

import os
import uuid
import warnings
from collections.abc import Mapping
from contextlib import nullcontext, suppress
from datetime import datetime, timezone
from typing import Any, Optional

from ..json_types import JsonObject
from ..message_utils import to_plain
from ..settings import AppSettings


def ensure_langfuse_otel_compat() -> None:
    """Backfill the OpenTelemetry env constant expected by Langfuse 4.x."""
    try:
        import opentelemetry.sdk.environment_variables as otel_env
    except Exception:
        return
    if not hasattr(otel_env, "OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED"):
        # google-adk 2.0.0 pins OpenTelemetry <=1.41.1, while Langfuse 4.6.1
        # imports this newer constant. The constant value is only the env name.
        otel_env.OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED = "OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED"


_DSPY_INSTRUMENTED = False


class RuntimeLangfuseClient:
    """Small adapter for Langfuse runtime enrichment and OTEL environment."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.client: Any | None = None
        self.unavailable = False

    def get_client(self) -> Any | None:
        if not self.settings.langfuse_enabled or self.unavailable:
            return None
        if not self.settings.langfuse_public_key or not self.settings.langfuse_secret_key:
            return None
        if self.client is not None:
            return self.client
        try:
            ensure_langfuse_otel_compat()
            from langfuse import Langfuse

            self.client = Langfuse(
                public_key=self.settings.langfuse_public_key,
                secret_key=self.settings.langfuse_secret_key,
                base_url=self.settings.langfuse_base_url,
                environment=self.settings.langfuse_deployment_environment,
                flush_interval=max(self.settings.langfuse_export_interval_ms / 1000, 0.1),
            )
            return self.client
        except Exception as exc:
            self.unavailable = True
            print(f"[WARN] failed to initialize Langfuse runtime enrichment: {exc}", flush=True)
            return None

    def current_trace_ref(self) -> tuple[Optional[str], Optional[str]]:
        client = self.client or self.get_client()
        if client is None:
            return None, None
        try:
            trace_id = client.get_current_trace_id()
            trace_url = client.get_trace_url(trace_id=trace_id) if trace_id else None
            return trace_id, trace_url
        except Exception as exc:
            print(f"[WARN] failed to read current Langfuse trace: {exc}", flush=True)
            return None, None

    def fetch_trace(self, trace_id: str) -> Optional[JsonObject]:
        if not trace_id or not self.settings.langfuse_enabled:
            return None
        if not self.settings.langfuse_public_key or not self.settings.langfuse_secret_key:
            return None
        try:
            ensure_langfuse_otel_compat()
            from langfuse.api.client import LangfuseAPI

            client = LangfuseAPI(
                base_url=self.settings.langfuse_base_url,
                username=self.settings.langfuse_public_key,
                password=self.settings.langfuse_secret_key,
                x_langfuse_public_key=self.settings.langfuse_public_key,
                timeout=10,
            )
            trace = client.trace.get(trace_id, fields="core,io,scores,observations,metrics")
            return to_plain(trace)
        except Exception as exc:
            return {"fetch_status": "failed", "error": f"{exc.__class__.__name__}: {exc}"}

    def start_observation(self, **kwargs: Any) -> Any:
        client = self.get_client()
        if client is None:
            return nullcontext(None)
        try:
            return client.start_as_current_observation(**kwargs)
        except Exception as exc:
            print(f"[WARN] failed to start Langfuse observation: {exc}", flush=True)
            return nullcontext(None)

    def propagate_attributes(
        self,
        *,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        metadata: Optional[Mapping[str, str]] = None,
        trace_name: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> Any:
        if not self.settings.langfuse_enabled:
            return nullcontext()
        clean_metadata = {key: value for key, value in (metadata or {}).items() if value}
        clean_tags = [tag for tag in (tags or []) if tag]
        try:
            ensure_langfuse_otel_compat()
            from langfuse import propagate_attributes

            return propagate_attributes(
                user_id=user_id,
                session_id=session_id,
                metadata=clean_metadata or None,
                trace_name=trace_name,
                tags=clean_tags or None,
            )
        except Exception as exc:
            print(f"[WARN] failed to propagate Langfuse attributes: {exc}", flush=True)
            return nullcontext()

    @staticmethod
    def update_observation(observation: Any, **kwargs: Any) -> None:
        if observation is None:
            return
        clean = {key: value for key, value in kwargs.items() if value is not None}
        try:
            observation.update(**clean)
        except Exception as exc:
            print(f"[WARN] failed to update Langfuse observation: {exc}", flush=True)

    def emit_sdk_child_observations(self, parent: Any, children: list[dict[str, Any]]) -> None:
        """把 SDK message 投影出的子观测（逐工具 span / 逐轮 generation）挂到 parent 之下。

        parent 为观测对象时用 `parent.start_observation`（平级子、非 current）；parent 为 None 时
        回退到 client 级 `start_observation`，自动挂到当前 OTEL ambient span（治理 job 的 root span）。
        每条独立 try/except，绝不因观测失败中断主流程。
        """
        if not children:
            return
        factory = getattr(parent, "start_observation", None)
        if factory is None:
            client = self.get_client()
            factory = getattr(client, "start_observation", None) if client is not None else None
        if factory is None:
            return
        for child in children:
            try:
                kwargs = {
                    "as_type": child.get("kind", "span"),
                    "name": child.get("name"),
                    "input": child.get("input"),
                    "output": child.get("output"),
                    "metadata": child.get("metadata") or None,
                    "model": child.get("model"),
                    "usage_details": child.get("usage_details"),
                    "cost_details": child.get("cost_details"),
                    "level": child.get("level"),
                }
                observation = factory(**{key: value for key, value in kwargs.items() if value is not None})
                end = getattr(observation, "end", None)
                if callable(end):
                    end()
            except Exception as exc:
                print(f"[WARN] failed to emit Langfuse sdk child observation: {exc}", flush=True)

    @staticmethod
    def set_trace_attributes(
        observation: Any,
        *,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        metadata: Optional[Mapping[str, str]] = None,
        trace_name: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> None:
        if observation is None:
            return
        otel_span = getattr(observation, "_otel_span", None)
        if otel_span is None:
            return

        attributes: dict[str, str | list[str]] = {}
        if session_id:
            attributes["session.id"] = session_id
        if user_id:
            attributes["user.id"] = user_id
        if trace_name:
            attributes["langfuse.trace.name"] = trace_name
        clean_tags = [tag for tag in (tags or []) if tag]
        if clean_tags:
            attributes["langfuse.trace.tags"] = clean_tags
        for key, value in (metadata or {}).items():
            if value:
                attributes[f"langfuse.trace.metadata.{key}"] = value
        if not attributes:
            return

        try:
            otel_span.set_attributes(attributes)
        except AttributeError:
            for key, value in attributes.items():
                with suppress(Exception):
                    otel_span.set_attribute(key, value)
        except Exception as exc:
            print(f"[WARN] failed to set Langfuse trace attributes: {exc}", flush=True)

    @staticmethod
    def set_trace_io(observation: Any, *, input: Any, output: Any) -> None:
        if observation is None:
            return
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message="Trace-level input/output is deprecated.*",
                    category=DeprecationWarning,
                )
                observation.set_trace_io(input=input, output=output)
        except Exception as exc:
            print(f"[WARN] failed to set Langfuse trace input/output: {exc}", flush=True)

    def upsert_trace(
        self,
        trace_id: Optional[str],
        *,
        name: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        input: Any = None,
        output: Any = None,
        metadata: Optional[Mapping[str, str]] = None,
        tags: Optional[list[str]] = None,
    ) -> None:
        if not trace_id or not self.settings.langfuse_enabled:
            return
        clean_metadata = {key: value for key, value in (metadata or {}).items() if value}
        clean_tags = [tag for tag in (tags or []) if tag]
        try:
            ensure_langfuse_otel_compat()
            from langfuse.api.ingestion.types.ingestion_event import IngestionEvent_TraceCreate
            from langfuse.api.ingestion.types.trace_body import TraceBody

            client = self.get_client()
            api = getattr(client, "api", None) if client is not None else None
            if api is None:
                return
            event = IngestionEvent_TraceCreate(
                id=f"runtime-trace-upsert-{uuid.uuid4()}",
                timestamp=datetime.now(timezone.utc).isoformat(),
                body=TraceBody(
                    id=trace_id,
                    name=name,
                    user_id=user_id,
                    session_id=session_id,
                    input=to_plain(input),
                    output=to_plain(output),
                    metadata=clean_metadata or None,
                    tags=clean_tags or None,
                ),
            )
            response = api.ingestion.batch(batch=[event], metadata={"source": "agent-gov-runtime"})
            ingestion_errors = getattr(getattr(response, "data", None), "errors", None) or []
            if ingestion_errors:
                details = "; ".join(
                    f"status={getattr(item, 'status', 'unknown')} message={getattr(item, 'message', None) or 'unknown'}" for item in ingestion_errors
                )
                raise RuntimeError(f"Langfuse trace ingestion rejected: {details}")
        except Exception as exc:
            print(f"[WARN] failed to upsert Langfuse trace: {exc}", flush=True)

    def flush(self) -> None:
        client = self.client or self.get_client()
        if client is None:
            return
        try:
            client.flush()
        except Exception as exc:
            print(f"[WARN] failed to flush Langfuse runtime enrichment: {exc}", flush=True)

    def build_env(self) -> dict[str, str]:
        env = self.build_otel_env()
        if not env:
            return {}
        signals = set(self.settings.langfuse_otel_signals)
        env.update(
            {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "OTEL_LOG_USER_PROMPTS": "1",
                "OTEL_LOG_TOOL_DETAILS": "1",
                "OTEL_LOG_TOOL_CONTENT": "1",
                "OTEL_LOG_RAW_API_BODIES": "1",
            }
        )
        if "traces" in signals:
            env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] = "1"
        return env

    def build_otel_env(self) -> dict[str, str]:
        if not self.settings.langfuse_enabled:
            return {}
        if not self.settings.langfuse_public_key or not self.settings.langfuse_secret_key:
            raise ValueError("LANGFUSE_ENABLED=true requires LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY")

        signals = set(self.settings.langfuse_otel_signals)
        if not signals:
            raise ValueError("LANGFUSE_OTEL_SIGNALS must include at least one of: traces, metrics, logs")

        env = {
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_EXPORTER_OTLP_ENDPOINT": self.settings.langfuse_effective_otel_endpoint,
            "OTEL_EXPORTER_OTLP_HEADERS": self.settings.langfuse_otel_headers,
            "OTEL_SERVICE_NAME": self.settings.langfuse_service_name,
            "OTEL_RESOURCE_ATTRIBUTES": self.settings.langfuse_resource_attributes,
            "OTEL_METRIC_EXPORT_INTERVAL": str(self.settings.langfuse_export_interval_ms),
            "OTEL_LOGS_EXPORT_INTERVAL": str(self.settings.langfuse_export_interval_ms),
            "OTEL_TRACES_EXPORT_INTERVAL": str(self.settings.langfuse_export_interval_ms),
        }
        if "traces" in signals:
            env["OTEL_TRACES_EXPORTER"] = "otlp"
        if "metrics" in signals:
            env["OTEL_METRICS_EXPORTER"] = "otlp"
        if "logs" in signals:
            env["OTEL_LOGS_EXPORTER"] = "otlp"
        return env

    def apply_otel_env(self) -> dict[str, str]:
        env = self.build_otel_env()
        os.environ.update(env)
        return env

    def instrument_dspy(self) -> bool:
        global _DSPY_INSTRUMENTED
        if not self.settings.langfuse_enabled:
            return False
        if not self.settings.langfuse_public_key or not self.settings.langfuse_secret_key:
            return False
        if _DSPY_INSTRUMENTED:
            return True
        try:
            ensure_langfuse_otel_compat()
            self.apply_otel_env()
            from openinference.instrumentation.dspy import DSPyInstrumentor

            DSPyInstrumentor().instrument()
            _DSPY_INSTRUMENTED = True
            return True
        except Exception as exc:
            print(f"[WARN] failed to instrument DSPy with Langfuse: {exc}", flush=True)
            return False
