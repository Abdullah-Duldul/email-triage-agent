"""Gmail API client — OAuth, fetch, label, and draft operations."""

from __future__ import annotations

import base64
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .classifier import EmailInput

logger = logging.getLogger(__name__)

# gmail.modify = read + label + draft; does NOT grant send permission
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# All AI labels live under this parent so they group together in Gmail sidebar
_LABEL_PREFIX = "AI"

CATEGORY_LABEL: dict[str, str] = {
    "urgent-action": f"{_LABEL_PREFIX}/Urgent-Action",
    "needs-reply": f"{_LABEL_PREFIX}/Needs-Reply",
    "reference-only": f"{_LABEL_PREFIX}/Reference-Only",
    "newsletter": f"{_LABEL_PREFIX}/Newsletter",
    "spam-likely": f"{_LABEL_PREFIX}/Spam-Likely",
}
MANUAL_REVIEW_LABEL = f"{_LABEL_PREFIX}/Manual-Review"


def authenticate(
    credentials_path: str | Path = "credentials.json",
    token_path: str | Path = "token.json",
) -> Credentials:
    """Run the OAuth2 flow and persist the token for future runs.

    On the first call this opens a browser window for the user to authorise
    the application.  Subsequent calls load and (if necessary) refresh the
    saved token without any browser interaction.

    Args:
        credentials_path: Path to the OAuth desktop client ``credentials.json``
            downloaded from Google Cloud Console.
        token_path: Where to save (and later reload) the access/refresh token.

    Returns:
        A valid, ready-to-use ``google.oauth2.credentials.Credentials`` object.

    Raises:
        FileNotFoundError: If ``credentials_path`` does not exist.
    """
    credentials_path = Path(credentials_path)
    token_path = Path(token_path)

    if not credentials_path.exists():
        raise FileNotFoundError(
            f"credentials.json not found at {credentials_path}. "
            "Download it from Google Cloud Console → OAuth 2.0 Client IDs."
        )

    creds: Optional[Credentials] = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing expired Gmail token.")
            creds.refresh(Request())
        else:
            logger.info("Starting OAuth flow — a browser window will open.")
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)

        token_path.write_text(creds.to_json())
        logger.info("Token saved to %s", token_path)

    return creds


class GmailClient:
    """High-level Gmail API wrapper used by the triage pipeline.

    Args:
        credentials_path: Path to ``credentials.json``.
        token_path: Path to ``token.json`` (created on first auth).
    """

    def __init__(
        self,
        credentials_path: str | Path = "credentials.json",
        token_path: str | Path = "token.json",
    ) -> None:
        creds = authenticate(credentials_path, token_path)
        self._svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self._label_id_cache: dict[str, str] = {}  # label name → Gmail label id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_unread_messages(self, limit: int = 20) -> list[EmailInput]:
        """Fetch unread messages from the inbox.

        Args:
            limit: Maximum number of messages to return.

        Returns:
            List of :class:`~email_triage.classifier.EmailInput` objects,
            ordered newest-first.
        """
        response = (
            self._svc.users()
            .messages()
            .list(userId="me", labelIds=["INBOX", "UNREAD"], maxResults=limit)
            .execute()
        )
        messages = response.get("messages", [])
        logger.info("Found %d unread message(s) in inbox.", len(messages))

        results: list[EmailInput] = []
        for msg_stub in messages:
            try:
                full = (
                    self._svc.users()
                    .messages()
                    .get(userId="me", id=msg_stub["id"], format="full")
                    .execute()
                )
                results.append(self._parse_message(full))
            except HttpError as exc:
                logger.warning("Skipping message %s: %s", msg_stub["id"], exc)

        return results

    def apply_triage_label(
        self,
        message_id: str,
        category: str,
        needs_human_review: bool,
    ) -> str:
        """Apply the appropriate AI label to a message.

        Low-confidence results (``needs_human_review=True``) receive the
        ``AI/Manual-Review`` label instead of the category label so a human
        can adjudicate before any action is taken.

        Args:
            message_id: Gmail message ID.
            category: Triage category string from :class:`ClassificationResult`.
            needs_human_review: Whether confidence was below threshold.

        Returns:
            The name of the label that was applied.
        """
        label_name = MANUAL_REVIEW_LABEL if needs_human_review else CATEGORY_LABEL[category]
        label_id = self._get_or_create_label(label_name)

        self._svc.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [label_id]},
        ).execute()

        logger.debug("Applied label %r to message %s", label_name, message_id)
        return label_name

    def create_draft_reply(
        self,
        message_id: str,
        thread_id: str,
        to_address: str,
        subject: str,
        reply_body: str,
    ) -> str:
        """Save a draft reply into Gmail Drafts.

        Args:
            message_id: The original message ID (used for ``In-Reply-To`` header).
            thread_id: The Gmail thread ID so the draft is threaded correctly.
            to_address: Reply-to address (the original sender).
            subject: Original subject line; ``Re:`` prefix is added if absent.
            reply_body: Plain-text body of the reply.

        Returns:
            The Gmail draft ID of the created draft.
        """
        reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"

        mime = MIMEMultipart()
        mime["To"] = to_address
        mime["Subject"] = reply_subject
        mime["In-Reply-To"] = message_id
        mime["References"] = message_id
        mime.attach(MIMEText(reply_body, "plain"))

        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        draft = (
            self._svc.users()
            .drafts()
            .create(
                userId="me",
                body={"message": {"raw": raw, "threadId": thread_id}},
            )
            .execute()
        )
        draft_id: str = draft["id"]
        logger.debug("Created draft %s for message %s", draft_id, message_id)
        return draft_id

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_or_create_label(self, name: str) -> str:
        """Return the Gmail label ID for *name*, creating it if necessary.

        Results are cached in-process to avoid redundant API calls when
        processing multiple messages in the same run.

        Args:
            name: Full label name, e.g. ``"AI/Urgent-Action"``.

        Returns:
            The Gmail label ID string.
        """
        if name in self._label_id_cache:
            return self._label_id_cache[name]

        existing = self._svc.users().labels().list(userId="me").execute()
        for label in existing.get("labels", []):
            self._label_id_cache[label["name"]] = label["id"]

        if name not in self._label_id_cache:
            created = (
                self._svc.users()
                .labels()
                .create(
                    userId="me",
                    body={
                        "name": name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                )
                .execute()
            )
            self._label_id_cache[name] = created["id"]
            logger.info("Created Gmail label %r", name)

        return self._label_id_cache[name]

    @staticmethod
    def _parse_message(raw: dict) -> EmailInput:
        """Convert a raw Gmail API message object into an :class:`EmailInput`.

        Handles both simple and multipart MIME structures. Prefers
        ``text/plain`` over ``text/html`` when both are present.

        Args:
            raw: Full message dict from the Gmail API (format=``full``).

        Returns:
            Parsed :class:`EmailInput` ready to pass to the classifier.
        """
        headers = {h["name"].lower(): h["value"] for h in raw["payload"].get("headers", [])}
        subject = headers.get("subject", "(no subject)")
        sender = headers.get("from", "(unknown sender)")

        body = _extract_body(raw["payload"])

        return EmailInput(
            message_id=raw["id"],
            thread_id=raw.get("threadId", ""),
            subject=subject,
            sender=sender,
            body=body,
        )


def _extract_body(payload: dict) -> str:
    """Recursively extract plain-text body from a Gmail message payload.

    Gmail encodes body data as base64url.  Multipart messages are walked
    depth-first; ``text/plain`` is preferred over ``text/html``.

    Args:
        payload: The ``payload`` dict from a Gmail API message object.

    Returns:
        Decoded plain-text body, or an empty string if none is found.
    """
    mime_type: str = payload.get("mimeType", "")

    if mime_type.startswith("multipart/"):
        parts = payload.get("parts", [])
        # Prefer text/plain over text/html at this level before recursing
        for part in parts:
            if part.get("mimeType") == "text/plain":
                text = _decode_body_data(part.get("body", {}).get("data", ""))
                if text:
                    return text
        # Fall back: recurse into nested parts
        for part in parts:
            text = _extract_body(part)
            if text:
                return text

    else:
        return _decode_body_data(payload.get("body", {}).get("data", ""))

    return ""


def _decode_body_data(data: str) -> str:
    """Decode a base64url-encoded Gmail body data string.

    Args:
        data: Base64url string from ``payload.body.data``.

    Returns:
        UTF-8 decoded string, substituting replacement characters on errors.
    """
    if not data:
        return ""
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
