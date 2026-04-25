import html
import logging
import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Callable

import requests
from dotenv import load_dotenv


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("lab2-worker-rest")


MZINGA_URL = os.environ["MZINGA_URL"].rstrip("/")
MZINGA_EMAIL = os.environ["MZINGA_EMAIL"]
MZINGA_PASSWORD = os.environ["MZINGA_PASSWORD"]
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "5"))
SMTP_HOST = os.environ.get("SMTP_HOST", "localhost")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "1025"))
EMAIL_FROM = os.environ.get("EMAIL_FROM", "worker@mzinga.io")


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
        logger.info("Authenticated with MZinga API as %s", MZINGA_EMAIL)

    def headers(self) -> dict[str, str]:
        if not self.token:
            self.login()
        return {"Authorization": f"Bearer {self.token}"}

    def with_reauth(self, request_fn: Callable[[], requests.Response]) -> requests.Response:
        response = request_fn()
        if response.status_code != 401:
            response.raise_for_status()
            return response

        logger.warning("JWT expired or rejected; authenticating again")
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


def send_email(document: dict[str, Any], to: list[str], cc: list[str], bcc: list[str]) -> None:
    subject = document.get("subject") or "(no subject)"
    body_html = serialize_body_to_html(document.get("body"))

    message = MIMEMultipart("alternative")
    message["From"] = EMAIL_FROM
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    if cc:
        message["Cc"] = ", ".join(cc)
    message.attach(MIMEText(body_html, "html", "utf-8"))

    recipients = to + cc + bcc
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.sendmail(EMAIL_FROM, recipients, message.as_string())


def process_document(api: MZingaApi, document: dict[str, Any]) -> None:
    document_id = document["id"]
    logger.info("Processing communication %s", document_id)

    api.update_status(document_id, "processing")
    try:
        to = extract_emails(document.get("tos"))
        cc = extract_emails(document.get("ccs"))
        bcc = extract_emails(document.get("bccs"))

        if not to:
            raise ValueError("No valid email addresses found for 'tos' users.")

        send_email(document, to, cc, bcc)
        api.update_status(document_id, "sent")
        logger.info("Communication %s sent to %s", document_id, ", ".join(to))
    except Exception:
        api.update_status(document_id, "failed")
        logger.exception("Communication %s failed", document_id)


def main() -> None:
    api = MZingaApi()
    api.login()
    logger.info("Worker started. Polling every %s seconds", POLL_INTERVAL_SECONDS)

    while True:
        pending_documents = api.fetch_pending()
        if not pending_documents:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        for document in pending_documents:
            process_document(api, document)


if __name__ == "__main__":
    main()
