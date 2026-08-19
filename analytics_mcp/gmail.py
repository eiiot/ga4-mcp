"""Read-only Gmail tools for the hosted Google MCP service."""

from __future__ import annotations

import base64
from typing import Any, Callable

import httpx
from google.auth.credentials import Credentials
from google.auth.transport.requests import Request

_credential_provider: Callable[[], Credentials] | None = None
_GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"


def set_credential_provider(provider: Callable[[], Credentials]) -> None:
    global _credential_provider
    _credential_provider = provider


def _credentials() -> Credentials:
    if _credential_provider is None:
        raise RuntimeError("Hosted Gmail credentials are not configured")
    credentials = _credential_provider()
    if not credentials.valid:
        credentials.refresh(Request())
    return credentials


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = httpx.get(
        f"{_GMAIL_API}/{path}",
        params=params,
        headers={"Authorization": f"Bearer {_credentials().token}"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _decode(data: str | None) -> str | None:
    if not data:
        return None
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding).decode(
        "utf-8", errors="replace"
    )


def _plain_text(payload: dict[str, Any]) -> str | None:
    if payload.get("mimeType") == "text/plain":
        return _decode(payload.get("body", {}).get("data"))
    for part in payload.get("parts", []):
        text = _plain_text(part)
        if text:
            return text
    return None


def normalize_message(message: dict[str, Any]) -> dict[str, Any]:
    """Return useful message fields without exposing MIME/base64 noise."""
    payload = message.get("payload", {})
    headers = {
        header.get("name", "").lower(): header.get("value")
        for header in payload.get("headers", [])
    }
    return {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "label_ids": message.get("labelIds", []),
        "snippet": message.get("snippet"),
        "from": headers.get("from"),
        "to": headers.get("to"),
        "cc": headers.get("cc"),
        "subject": headers.get("subject"),
        "date": headers.get("date"),
        "body_text": _plain_text(payload),
    }


def search_messages(
    query: str,
    max_results: int = 25,
    page_token: str | None = None,
) -> dict[str, Any]:
    """Search the connected Gmail account using Gmail search syntax."""
    result = _get(
        "messages",
        {
            "q": query,
            "maxResults": max(1, min(max_results, 100)),
            "pageToken": page_token,
        },
    )
    return {
        "messages": result.get("messages", []),
        "next_page_token": result.get("nextPageToken"),
        "result_size_estimate": result.get("resultSizeEstimate", 0),
    }


def get_message(message_id: str) -> dict[str, Any]:
    """Get one Gmail message, including selected headers and plain-text body."""
    return normalize_message(_get(f"messages/{message_id}", {"format": "full"}))


def list_threads(
    query: str | None = None,
    max_results: int = 25,
    page_token: str | None = None,
) -> dict[str, Any]:
    """List threads in the connected Gmail account, optionally using Gmail search syntax."""
    result = _get(
        "threads",
        {
            "q": query,
            "maxResults": max(1, min(max_results, 100)),
            "pageToken": page_token,
        },
    )
    return {
        "threads": result.get("threads", []),
        "next_page_token": result.get("nextPageToken"),
        "result_size_estimate": result.get("resultSizeEstimate", 0),
    }


def get_thread(thread_id: str) -> dict[str, Any]:
    """Get a Gmail thread with normalized messages in chronological order."""
    thread = _get(f"threads/{thread_id}", {"format": "full"})
    return {
        "id": thread.get("id"),
        "history_id": thread.get("historyId"),
        "messages": [
            normalize_message(message) for message in thread.get("messages", [])
        ],
    }
