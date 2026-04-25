import html
import logging
import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from bson import ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient, ReturnDocument


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("lab1-worker")


MONGODB_URI = os.environ["MONGODB_URI"]
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "5"))
SMTP_HOST = os.environ.get("SMTP_HOST", "localhost")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "1025"))
EMAIL_FROM = os.environ.get("EMAIL_FROM", "worker@mzinga.io")


def get_database(client: MongoClient):
    return client.get_default_database(default="mzinga")


def relation_value_to_object_id(value: Any) -> ObjectId | None:
    if isinstance(value, dict):
        value = value.get("id") or value.get("_id") or value.get("value")

    if isinstance(value, ObjectId):
        return value

    if isinstance(value, str) and ObjectId.is_valid(value):
        return ObjectId(value)

    return None


def extract_relation_ids(references: Any) -> list[ObjectId]:
    if not references:
        return []

    object_ids: list[ObjectId] = []
    for reference in references:
        if isinstance(reference, dict):
            value = reference.get("value")
        else:
            value = reference

        object_id = relation_value_to_object_id(value)
        if object_id:
            object_ids.append(object_id)

    return object_ids


def resolve_emails(users_collection, references: Any) -> list[str]:
    object_ids = extract_relation_ids(references)
    if not object_ids:
        return []

    users = users_collection.find(
        {"_id": {"$in": object_ids}},
        {"email": 1},
    )
    return [user["email"] for user in users if user.get("email")]


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
            doc_id = html.escape(str(((node.get("doc") or {}).get("value") or {}).get("id", "")), quote=True)
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


def send_email(document: dict[str, Any], to: list[str], cc: list[str], bcc: list[str], body_html: str) -> None:
    subject = document.get("subject") or "(no subject)"

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


def claim_next_communication(communications_collection) -> dict[str, Any] | None:
    return communications_collection.find_one_and_update(
        {"status": "pending"},
        {"$set": {"status": "processing"}},
        return_document=ReturnDocument.AFTER,
    )


def process_document(db, document: dict[str, Any]) -> None:
    communications = db.communications
    users = db.users
    document_id = document["_id"]

    try:
        to = resolve_emails(users, document.get("tos"))
        cc = resolve_emails(users, document.get("ccs"))
        bcc = resolve_emails(users, document.get("bccs"))

        if not to:
            raise ValueError("No valid email addresses found for 'tos' users.")

        body_html = serialize_body_to_html(document.get("body"))
        send_email(document, to, cc, bcc, body_html)

        communications.update_one(
            {"_id": document_id},
            {"$set": {"status": "sent"}},
        )
        logger.info("Communication %s sent to %s", document_id, ", ".join(to))
    except Exception:
        communications.update_one(
            {"_id": document_id},
            {"$set": {"status": "failed"}},
        )
        logger.exception("Communication %s failed", document_id)


def main() -> None:
    logger.info("Starting worker with MongoDB=%s SMTP=%s:%s", MONGODB_URI, SMTP_HOST, SMTP_PORT)
    client = MongoClient(MONGODB_URI)
    db = get_database(client)
    communications = db.communications

    while True:
        document = claim_next_communication(communications)
        if not document:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        logger.info("Claimed communication %s", document["_id"])
        process_document(db, document)


if __name__ == "__main__":
    main()
