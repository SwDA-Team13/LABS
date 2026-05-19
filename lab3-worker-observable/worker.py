import html
import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Callable

import requests
from dotenv import load_dotenv
import structlog
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode
from prometheus_client import start_http_server
from structlog.contextvars import bind_contextvars, merge_contextvars, unbind_contextvars


load_dotenv()

MZINGA_URL = os.environ["MZINGA_URL"].rstrip("/")
MZINGA_EMAIL = os.environ["MZINGA_EMAIL"]
MZINGA_PASSWORD = os.environ["MZINGA_PASSWORD"]
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "5"))
SMTP_HOST = os.environ.get("SMTP_HOST", "localhost")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "1025"))
EMAIL_FROM = os.environ.get("EMAIL_FROM", "worker@mzinga.io")
OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318").rstrip("/")
SERVICE_NAME_VALUE = os.environ.get("OTEL_SERVICE_NAME", "email-worker")
SERVICE_VERSION_VALUE = os.environ.get("OTEL_SERVICE_VERSION", "1.0.0")
PROMETHEUS_PORT = int(os.environ.get("PROMETHEUS_PORT", "8000"))


otel_resource = Resource(
    attributes={
        SERVICE_NAME: SERVICE_NAME_VALUE,
        SERVICE_VERSION: SERVICE_VERSION_VALUE,
    },
)


def configure_tracing(resource: Resource):
    tracer_provider = TracerProvider(resource=resource)
    otlp_exporter = OTLPSpanExporter(endpoint=f"{OTLP_ENDPOINT}/v1/traces")
    tracer_provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
    trace.set_tracer_provider(tracer_provider)
    RequestsInstrumentor().instrument()
    return trace.get_tracer(SERVICE_NAME_VALUE)


def configure_metrics(resource: Resource):
    start_http_server(port=PROMETHEUS_PORT)
    metric_reader = PrometheusMetricReader()
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)
    return metrics.get_meter(SERVICE_NAME_VALUE)


tracer = configure_tracing(otel_resource)
meter = configure_metrics(otel_resource)

emails_processed = meter.create_counter(
    name="emails_processed_total",
    description="Total number of communications processed",
    unit="1",
)
processing_duration = meter.create_histogram(
    name="email_processing_duration_seconds",
    description="End-to-end duration of processing one communication",
    unit="s",
)
smtp_duration = meter.create_histogram(
    name="smtp_send_duration_seconds",
    description="Duration of the SMTP send call",
    unit="s",
)
poll_counter = meter.create_counter(
    name="worker_poll_total",
    description="Number of poll cycles",
    unit="1",
)


def add_trace_context(_, __, event_dict: dict[str, Any]) -> dict[str, Any]:
    span = trace.get_current_span()
    span_context = span.get_span_context()
    if span_context.is_valid:
        event_dict["trace_id"] = format(span_context.trace_id, "032x")
        event_dict["span_id"] = format(span_context.span_id, "016x")
    return event_dict


structlog.configure(
    processors=[
        merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        add_trace_context,
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(20),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)
log = structlog.get_logger().bind(service=SERVICE_NAME_VALUE)


class MZingaApi:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.token: str | None = None

    def login(self) -> None:
        response = self.session.post(
            f"{MZINGA_URL}/api/users/login",
            json={
                "email": MZINGA_EMAIL,
                "password": MZINGA_PASSWORD,
            },
            timeout=15,
        )
        response.raise_for_status()
        self.token = response.json()["token"]
        log.info("authenticated", email=MZINGA_EMAIL)

    def headers(self) -> dict[str, str]:
        if not self.token:
            self.login()
        return {"Authorization": f"Bearer {self.token}"}

    def with_reauth(self, request_fn: Callable[[], requests.Response]) -> requests.Response:
        response = request_fn()
        if response.status_code != 401:
            response.raise_for_status()
            return response

        log.warning("jwt_rejected_reauthenticating")
        self.login()
        response = request_fn()
        response.raise_for_status()
        return response

    def fetch_pending(self) -> list[dict[str, Any]]:
        response = self.with_reauth(
            lambda: self.session.get(
                f"{MZINGA_URL}/api/communications",
                params={
                    "where[status][equals]": "pending",
                    "depth": 1,
                },
                headers=self.headers(),
                timeout=15,
            ),
        )
        return response.json().get("docs", [])

    def fetch_communication(self, document_id: str) -> dict[str, Any]:
        response = self.with_reauth(
            lambda: self.session.get(
                f"{MZINGA_URL}/api/communications/{document_id}",
                params={"depth": 1},
                headers=self.headers(),
                timeout=15,
            ),
        )
        return response.json()

    def update_status(self, document_id: str, status: str) -> dict[str, Any]:
        response = self.with_reauth(
            lambda: self.session.patch(
                f"{MZINGA_URL}/api/communications/{document_id}",
                json={"status": status},
                headers=self.headers(),
                timeout=15,
            ),
        )
        return response.json()


def extract_emails(relationships: Any) -> list[str]:
    if not relationships:
        return []

    emails: list[str] = []
    for relationship in relationships:
        if not isinstance(relationship, dict):
            continue

        value = relationship.get("value") or {}
        if isinstance(value, dict) and value.get("email"):
            emails.append(value["email"])

    return emails


def serialize_text_node(node: dict[str, Any]) -> str:
    text = html.escape(str(node.get("text", ""))).replace("\n", "<br/>")
    if not text:
        return ""

    result = f"<span>{text}</span>"
    if node.get("bold"):
        result = f"<strong>{result}</strong>"
    if node.get("code"):
        result = f"<code>{result}</code>"
    if node.get("italic"):
        result = f"<em>{result}</em>"
    return result


def serialize_children(children: Any) -> str:
    if not isinstance(children, list):
        return ""

    return "".join(serialize_node(child) for child in children if child)


def serialize_node(node: Any) -> str:
    if isinstance(node, str):
        return html.escape(node)

    if not isinstance(node, dict):
        return ""

    if "text" in node:
        return serialize_text_node(node)

    node_type = node.get("type")
    children_html = serialize_children(node.get("children"))

    if node_type == "upload":
        value = node.get("value") or {}
        url = html.escape(str(value.get("url", "")), quote=True)
        title = html.escape(str(value.get("title") or value.get("url") or "Attachment"))
        if not url:
            return ""
        return f'<p><a href="{url}">{title}</a></p>'

    if not children_html:
        return ""

    if node_type in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return f"<{node_type}>{children_html}</{node_type}>"

    if node_type == "quote":
        return f"<blockquote>{children_html}</blockquote>"

    if node_type == "ul":
        return f"<ul>{children_html}</ul>"

    if node_type == "ol":
        return f"<ol>{children_html}</ol>"

    if node_type == "li":
        return f"<li>{children_html}</li>"

    if node_type == "indent":
        return f'<p style="padding-left: 20px">{children_html}</p>'

    if node_type == "link":
        new_tab = ' target="_blank" rel="noopener noreferrer"' if node.get("newTab") else ""
        if node.get("linkType") == "internal":
            doc_value = (node.get("doc") or {}).get("value") or {}
            doc_id = html.escape(str(doc_value.get("id", "")), quote=True)
            return f'<a data-doc-id="{doc_id}"{new_tab}>{children_html}</a>'

        url = html.escape(str(node.get("url", "")), quote=True)
        return f'<a href="{url}"{new_tab}>{children_html}</a>'

    return f"<p>{children_html}</p>"


def serialize_body_to_html(body: Any) -> str:
    if isinstance(body, list):
        return serialize_children(body)

    if isinstance(body, str):
        return html.escape(body)

    return ""


def send_email(
    document: dict[str, Any],
    body_html: str,
    to: list[str],
    cc: list[str],
    bcc: list[str],
) -> None:
    subject = document.get("subject") or "(no subject)"
    recipients = to + cc + bcc

    with tracer.start_as_current_span("send_email") as span:
        span.set_attribute("recipient_count", len(recipients))
        span.set_attribute("smtp.host", SMTP_HOST)
        span.set_attribute("smtp.port", SMTP_PORT)
        started_at = time.perf_counter()

        try:
            message = MIMEMultipart("alternative")
            message["From"] = EMAIL_FROM
            message["To"] = ", ".join(to)
            message["Subject"] = subject
            if cc:
                message["Cc"] = ", ".join(cc)
            message.attach(MIMEText(body_html, "html", "utf-8"))

            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
                smtp.sendmail(EMAIL_FROM, recipients, message.as_string())
        finally:
            smtp_duration.record(time.perf_counter() - started_at)


def process_document(api: MZingaApi, document: dict[str, Any]) -> None:
    document_id = document["id"]
    bind_contextvars(doc_id=document_id)

    try:
        with tracer.start_as_current_span("process_communication") as span:
            span.set_attribute("doc_id", document_id)
            started_at = time.perf_counter()
            recipients: list[str] = []

            try:
                document = api.fetch_communication(document_id)
                span.set_attribute("communication.subject", document.get("subject") or "")
                log.info("processing_started")
                api.update_status(document_id, "processing")

                to = extract_emails(document.get("tos"))
                cc = extract_emails(document.get("ccs"))
                bcc = extract_emails(document.get("bccs"))
                recipients = to + cc + bcc
                span.set_attribute("recipient_count", len(recipients))

                if not to:
                    raise ValueError("No valid email addresses found for 'tos' users.")

                body = document.get("body")
                with tracer.start_as_current_span("serialize_body") as serialize_span:
                    node_count = len(body) if isinstance(body, list) else 0
                    serialize_span.set_attribute("node_count", node_count)
                    body_html = serialize_body_to_html(body)

                send_email(document, body_html, to, cc, bcc)
                api.update_status(document_id, "sent")

                duration_s = time.perf_counter() - started_at
                span.set_attribute("processing.status", "sent")
                span.set_attribute("processing.duration_s", duration_s)
                processing_duration.record(duration_s)
                emails_processed.add(
                    1,
                    {
                        "status": "sent",
                        "recipient_count": len(recipients),
                    },
                )
                log.info(
                    "processing_completed",
                    status="sent",
                    recipient_count=len(recipients),
                    duration_s=round(duration_s, 3),
                )
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                span.set_attribute("processing.status", "failed")
                try:
                    api.update_status(document_id, "failed")
                except Exception as status_exc:
                    log.error("failed_status_update_failed", error=str(status_exc))
                duration_s = time.perf_counter() - started_at
                processing_duration.record(duration_s)
                emails_processed.add(
                    1,
                    {
                        "status": "failed",
                        "recipient_count": len(recipients),
                    },
                )
                log.exception("processing_failed", error=str(exc))
    finally:
        unbind_contextvars("doc_id")


def main() -> None:
    api = MZingaApi()
    api.login()
    log.info(
        "worker_started",
        poll_interval_s=POLL_INTERVAL_SECONDS,
        prometheus_port=PROMETHEUS_PORT,
    )

    while True:
        pending_documents = api.fetch_pending()
        if not pending_documents:
            poll_counter.add(1, {"result": "empty"})
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        poll_counter.add(1, {"result": "found"})
        log.info("pending_documents_found", count=len(pending_documents))
        for document in pending_documents:
            process_document(api, document)


if __name__ == "__main__":
    main()
