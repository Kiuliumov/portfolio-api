import base64
import hmac
import json
import os
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parseaddr

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["TABLE_NAME"])
ses = boto3.client("ses")

MAX_BODY_BYTES = 16_384
MAX_PAGE_SIZE = 100
ALLOWED_STATUSES = {"new", "read", "replied", "archived"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


def _response(status, payload):
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json; charset=utf-8",
            "cache-control": "no-store",
            "x-content-type-options": "nosniff",
        },
        "body": json.dumps(payload, default=_json_default),
    }


def _json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def _body(event):
    raw = event.get("body") or ""

    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        except Exception:
            raise ApiError(400, "Invalid request body encoding.")

    if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
        raise ApiError(413, "Request body is too large.")

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise ApiError(400, "Request body must be valid JSON.")

    if not isinstance(data, dict):
        raise ApiError(400, "Request body must be a JSON object.")

    return data


def _clean_text(value, field, maximum, required=False):
    if value is None:
        value = ""

    if not isinstance(value, str):
        raise ApiError(400, f"'{field}' must be a string.")

    value = value.strip()

    if required and not value:
        raise ApiError(400, f"'{field}' is required.")

    if len(value) > maximum:
        raise ApiError(
            400,
            f"'{field}' must be at most {maximum} characters.",
        )

    return value


def _email(value):
    value = _clean_text(value, "email", 254, required=True)

    if not EMAIL_RE.fullmatch(value) or parseaddr(value)[1] != value:
        raise ApiError(400, "'email' must be a valid email address.")

    return value


def _require_admin(token):
    """
    Protect administrative routes using a secret URL path token.

    Configure ADMIN_TOKEN as an environment variable. If it is missing,
    administrative access fails closed.
    """
    expected = os.environ.get("ADMIN_TOKEN", "")

    if not expected:
        raise ApiError(503, "Admin access is not configured.")

    if not isinstance(token, str) or not hmac.compare_digest(
        token.encode("utf-8"),
        expected.encode("utf-8"),
    ):
        raise ApiError(404, "Not found.")


def _create(event):
    data = _body(event)

    name = _clean_text(data.get("name"), "name", 120, required=True)
    email = _email(data.get("email"))
    subject = _clean_text(data.get("subject"), "subject", 200)
    message = _clean_text(
        data.get("message"),
        "message",
        10_000,
        required=True,
    )

    if len(message) < 2:
        raise ApiError(400, "'message' must be at least 2 characters.")

    now = datetime.now(timezone.utc)
    created_at = now.isoformat(timespec="milliseconds").replace(
        "+00:00",
        "Z",
    )
    message_id = str(uuid.uuid4())

    item = {
        "id": message_id,
        "gsi1pk": "MESSAGE",
        "gsi1sk": f"{created_at}#{message_id}",
        "name": name,
        "email": email,
        "subject": subject,
        "message": message,
        "status": "new",
        "createdAt": created_at,
    }

    retention_days = int(os.environ.get("MESSAGE_RETENTION_DAYS", "0"))

    if retention_days > 0:
        item["expiresAt"] = int(now.timestamp()) + retention_days * 86400

    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(id)",
        )
    except (BotoCoreError, ClientError):
        raise ApiError(
            503,
            "Unable to save your message right now.",
        )

    _send_notification(item)

    return _response(
        201,
        {
            "id": message_id,
            "createdAt": created_at,
            "status": "new",
        },
    )


def _send_notification(item):
    if os.environ.get("ENABLE_EMAIL", "false").lower() != "true":
        return

    recipient = os.environ.get("NOTIFICATION_EMAIL", "").strip()
    sender = os.environ.get("SES_FROM_EMAIL", "").strip()

    if not recipient or not sender:
        return

    subject = "New website contact message: " f"{item.get('subject') or '(no subject)'}"

    # Plain text only. Visitor-provided values are not used as email headers.
    body = (
        "New contact message\n\n"
        f"Name: {item['name']}\n"
        f"Email: {item['email']}\n"
        f"Subject: {item.get('subject') or '(no subject)'}\n\n"
        f"Message:\n{item['message']}\n\n"
        f"Message ID: {item['id']}\n"
    )

    try:
        ses.send_email(
            Source=sender,
            Destination={"ToAddresses": [recipient]},
            Message={
                "Subject": {
                    "Data": subject[:200],
                    "Charset": "UTF-8",
                },
                "Body": {
                    "Text": {
                        "Data": body,
                        "Charset": "UTF-8",
                    },
                },
            },
            ReplyToAddresses=[item["email"]],
        )
    except (BotoCoreError, ClientError):
        print(
            json.dumps(
                {
                    "event": "ses_notification_failed",
                    "messageId": item["id"],
                }
            )
        )


def _list(event):
    query = event.get("queryStringParameters") or {}

    try:
        limit = int(query.get("limit", "25"))
    except (TypeError, ValueError):
        raise ApiError(400, "'limit' must be an integer.")

    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ApiError(
            400,
            f"'limit' must be between 1 and {MAX_PAGE_SIZE}.",
        )

    kwargs = {
        "IndexName": "ByCreatedAt",
        "KeyConditionExpression": Key("gsi1pk").eq("MESSAGE"),
        "ScanIndexForward": False,
        "Limit": limit,
    }

    cursor = query.get("cursor")

    if cursor:
        try:
            decoded = base64.urlsafe_b64decode(cursor.encode("ascii") + b"===")
            last_key = json.loads(decoded.decode("utf-8"))

            required_keys = {"id", "gsi1pk", "gsi1sk"}

            if not isinstance(last_key, dict) or not required_keys.issubset(last_key):
                raise ValueError()

            kwargs["ExclusiveStartKey"] = last_key

        except Exception:
            raise ApiError(400, "Invalid pagination cursor.")

    try:
        result = table.query(**kwargs)
    except (BotoCoreError, ClientError):
        raise ApiError(
            503,
            "Unable to retrieve messages right now.",
        )

    next_cursor = None

    if result.get("LastEvaluatedKey"):
        encoded = json.dumps(
            result["LastEvaluatedKey"],
            separators=(",", ":"),
        ).encode("utf-8")

        next_cursor = base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")

    messages = []

    for item in result.get("Items", []):
        messages.append(
            {
                key: item[key]
                for key in (
                    "id",
                    "name",
                    "email",
                    "subject",
                    "message",
                    "status",
                    "createdAt",
                )
                if key in item
            }
        )

    return _response(
        200,
        {
            "items": messages,
            "nextCursor": next_cursor,
        },
    )


def _get(event):
    message_id = (event.get("pathParameters") or {}).get("id", "")

    if not message_id or len(message_id) > 64:
        raise ApiError(400, "Invalid message ID.")

    try:
        result = table.get_item(
            Key={"id": message_id},
            ConsistentRead=True,
        )
    except (BotoCoreError, ClientError):
        raise ApiError(
            503,
            "Unable to retrieve the message right now.",
        )

    item = result.get("Item")

    if not item:
        raise ApiError(404, "Message not found.")

    return _response(
        200,
        {
            key: item[key]
            for key in (
                "id",
                "name",
                "email",
                "subject",
                "message",
                "status",
                "createdAt",
            )
            if key in item
        },
    )


def _update(event):
    message_id = (event.get("pathParameters") or {}).get("id", "")

    if not message_id or len(message_id) > 64:
        raise ApiError(400, "Invalid message ID.")

    data = _body(event)

    if set(data.keys()) != {"status"}:
        raise ApiError(
            400,
            "Only the 'status' field can be updated.",
        )

    status = data.get("status")

    if not isinstance(status, str) or status not in ALLOWED_STATUSES:
        raise ApiError(
            400,
            "'status' must be one of: " f"{', '.join(sorted(ALLOWED_STATUSES))}.",
        )

    try:
        result = table.update_item(
            Key={"id": message_id},
            UpdateExpression="SET #status = :status",
            ConditionExpression="attribute_exists(id)",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":status": status},
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ApiError(404, "Message not found.")

        raise ApiError(
            503,
            "Unable to update the message right now.",
        )
    except BotoCoreError:
        raise ApiError(
            503,
            "Unable to update the message right now.",
        )

    item = result["Attributes"]

    return _response(
        200,
        {
            "id": item["id"],
            "status": item["status"],
            "createdAt": item["createdAt"],
        },
    )


def handler(event, context):
    try:
        request_context = event.get("requestContext") or {}
        http = request_context.get("http") or {}

        method = (http.get("method") or event.get("httpMethod") or "").upper()

        path = (event.get("rawPath") or event.get("path") or "").rstrip("/")

        path_params = event.get("pathParameters") or {}
        token = path_params.get("token")
        message_id = path_params.get("id")

        # Public endpoint
        if method == "POST" and path.endswith("/messages") and not token:
            return _create(event)

        # Protected endpoint: list messages.
        # Expected route: GET /messages/{token}
        if method == "GET" and token and not message_id:
            _require_admin(token)
            return _list(event)

        # Protected endpoint: retrieve one message.
        # Expected route: GET /messages/{token}/{id}
        if method == "GET" and token and message_id:
            _require_admin(token)
            return _get(event)

        # Protected endpoint: update message status.
        # Expected route: PATCH /messages/{token}/{id}
        if method == "PATCH" and token and message_id:
            _require_admin(token)
            return _update(event)

        return _response(404, {"error": "Not found"})

    except ApiError as exc:
        return _response(exc.status, {"error": exc.message})

    except Exception:
        print(json.dumps({"event": "unhandled_error"}))
        return _response(500, {"error": "Internal server error"})
