"""Unit tests for the email classifier — all Anthropic calls are mocked."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from email_triage.classifier import ClassificationResult, EmailClassifier, EmailInput

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _classify_response(
    *,
    category: str = "urgent-action",
    confidence: float = 0.95,
    reasoning: str = "Production outage.",
    suggested_action: str = "Escalate immediately.",
) -> MagicMock:
    """Return a mock Anthropic response containing a classify_email tool_use block."""
    block = SimpleNamespace(
        type="tool_use",
        name="classify_email",
        input={
            "category": category,
            "confidence": confidence,
            "reasoning": reasoning,
            "suggested_action": suggested_action,
        },
    )
    resp = MagicMock()
    resp.content = [block]
    resp.stop_reason = "tool_use"
    return resp


def _draft_response(text: str = "Happy to help, looking into it now.") -> MagicMock:
    """Return a mock Anthropic response containing a plain-text draft block."""
    block = SimpleNamespace(type="text", text=text)
    resp = MagicMock()
    resp.content = [block]
    return resp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def urgent_email() -> EmailInput:
    return EmailInput(
        message_id="test-001",
        thread_id="thread-001",
        subject="URGENT: Production database is down",
        sender="ops-alerts@company.com",
        body="Our primary production database went offline at 14:32 UTC.",
    )


@pytest.fixture()
def reply_email() -> EmailInput:
    return EmailInput(
        message_id="test-002",
        thread_id="thread-002",
        subject="Coffee catch-up?",
        sender="alice@example.com",
        body="Hey, free for coffee next week? Happy to work around your schedule.",
    )


# ---------------------------------------------------------------------------
# 1. Email body truncation (field_validator)
# ---------------------------------------------------------------------------


class TestEmailInputValidator:
    def test_oversized_body_truncated_to_4000_chars(self):
        email = EmailInput(
            message_id="trunc-001",
            subject="Big email",
            sender="sender@example.com",
            body="x" * 5_000,
        )
        assert len(email.body) == 4_000

    def test_body_within_limit_is_unchanged(self):
        body = "Short body."
        email = EmailInput(
            message_id="short-001",
            subject="Normal email",
            sender="sender@example.com",
            body=body,
        )
        assert email.body == body


# ---------------------------------------------------------------------------
# 2. Confidence threshold — needs_human_review property
# ---------------------------------------------------------------------------


class TestConfidenceThreshold:
    def test_needs_review_true_when_confidence_below_07(self):
        result = ClassificationResult(
            category="needs-reply",
            confidence=0.65,
            reasoning="Unclear intent.",
            suggested_action="Review manually.",
        )
        assert result.needs_human_review is True

    def test_needs_review_false_at_exactly_07(self):
        result = ClassificationResult(
            category="urgent-action",
            confidence=0.70,
            reasoning="Meets threshold.",
            suggested_action="Act now.",
        )
        assert result.needs_human_review is False

    def test_needs_review_false_above_07(self):
        result = ClassificationResult(
            category="newsletter",
            confidence=0.92,
            reasoning="Clear bulk content.",
            suggested_action="Archive.",
        )
        assert result.needs_human_review is False


# ---------------------------------------------------------------------------
# 3. Happy path + tool_use block parsing
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_returns_valid_classification_result(self, urgent_email):
        """classify() returns a well-formed ClassificationResult for a high-confidence email."""
        with patch("email_triage.classifier.anthropic.Anthropic") as MockAnthropic:
            mock_client = MockAnthropic.return_value
            mock_client.messages.create.return_value = _classify_response(
                category="urgent-action",
                confidence=0.95,
                reasoning="Production outage requires immediate attention.",
                suggested_action="Escalate to on-call engineer.",
            )

            result = EmailClassifier(api_key="test-key").classify(urgent_email)

        assert isinstance(result, ClassificationResult)
        assert result.category == "urgent-action"
        assert result.confidence == 0.95
        assert result.reasoning == "Production outage requires immediate attention."
        assert result.suggested_action == "Escalate to on-call engineer."
        assert result.draft_reply is None  # urgent-action never triggers a draft

    def test_tool_use_block_fields_mapped_correctly(self, urgent_email):
        """All four tool_use input fields land on the right ClassificationResult attributes."""
        with patch("email_triage.classifier.anthropic.Anthropic") as MockAnthropic:
            mock_client = MockAnthropic.return_value
            mock_client.messages.create.return_value = _classify_response(
                category="newsletter",
                confidence=0.88,
                reasoning="Bulk marketing digest.",
                suggested_action="Move to newsletters folder.",
            )

            result = EmailClassifier(api_key="test-key").classify(urgent_email)

        assert result.category == "newsletter"
        assert result.confidence == 0.88
        assert result.reasoning == "Bulk marketing digest."
        assert result.suggested_action == "Move to newsletters folder."

    def test_missing_tool_use_block_raises_value_error(self, urgent_email):
        """classify() raises ValueError when the Claude response contains no classify_email block."""
        empty_resp = MagicMock()
        empty_resp.content = []
        empty_resp.stop_reason = "end_turn"

        with patch("email_triage.classifier.anthropic.Anthropic") as MockAnthropic:
            mock_client = MockAnthropic.return_value
            mock_client.messages.create.return_value = empty_resp

            with pytest.raises(ValueError, match="classify_email"):
                EmailClassifier(api_key="test-key").classify(urgent_email)


# ---------------------------------------------------------------------------
# 4 & 5. Draft generation — second Sonnet call behaviour
# ---------------------------------------------------------------------------


class TestDraftGeneration:
    def test_second_call_made_for_needs_reply_high_confidence(self, reply_email):
        """classify() calls messages.create twice when category=needs-reply and confidence>=0.7."""
        draft_text = "Sure, Tuesday at 10 am works for me!"

        with patch("email_triage.classifier.anthropic.Anthropic") as MockAnthropic:
            mock_client = MockAnthropic.return_value
            mock_client.messages.create.side_effect = [
                _classify_response(category="needs-reply", confidence=0.85),
                _draft_response(draft_text),
            ]

            result = EmailClassifier(api_key="test-key").classify(reply_email)

        assert result.category == "needs-reply"
        assert result.draft_reply == draft_text
        assert mock_client.messages.create.call_count == 2

    def test_no_second_call_for_needs_reply_low_confidence(self, reply_email):
        """classify() skips the draft call when confidence<0.7 (needs_human_review=True)."""
        with patch("email_triage.classifier.anthropic.Anthropic") as MockAnthropic:
            mock_client = MockAnthropic.return_value
            mock_client.messages.create.return_value = _classify_response(
                category="needs-reply",
                confidence=0.60,  # below 0.7 → needs_human_review → no draft
            )

            result = EmailClassifier(api_key="test-key").classify(reply_email)

        assert result.draft_reply is None
        assert mock_client.messages.create.call_count == 1

    @pytest.mark.parametrize("category", ["urgent-action", "reference-only", "newsletter", "spam-likely"])
    def test_no_draft_for_non_reply_categories(self, urgent_email, category):
        """classify() makes exactly one API call for every category other than needs-reply."""
        with patch("email_triage.classifier.anthropic.Anthropic") as MockAnthropic:
            mock_client = MockAnthropic.return_value
            mock_client.messages.create.return_value = _classify_response(
                category=category, confidence=0.90
            )

            result = EmailClassifier(api_key="test-key").classify(urgent_email)

        assert result.draft_reply is None
        assert mock_client.messages.create.call_count == 1
