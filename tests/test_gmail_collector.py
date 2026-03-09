"""Tests for gmail_collector."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.gmail_collector import (
    collect_emails,
    estimate_priority,
    is_actionable,
    parse_email,
)
from cos.db import connect, get_pending_items, init_db


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "cos.db"
    init_db(path)
    return path


def _mcp_message(**overrides):
    base = {
        "id": "msg_001",
        "threadId": "thread_001",
        "labelIds": ["INBOX", "CATEGORY_PERSONAL"],
        "snippet": "Can we move the migration to next week?",
        "internalDate": "1772809175000",
        "headers": {
            "Subject": "Re: Hosting migration timeline",
            "From": "Client Name <client@example.com>",
            "To": "user@example.com",
            "Date": "Fri, 06 Mar 2026 14:59:35 +0000",
        },
    }
    base.update(overrides)
    return base


class TestParseEmail:
    def test_basic_parse(self):
        result = parse_email(_mcp_message())
        assert result["id"] == "msg_001"
        assert result["thread_id"] == "thread_001"
        assert result["subject"] == "Re: Hosting migration timeline"
        assert result["sender"] == "client@example.com"
        assert result["snippet"] == "Can we move the migration to next week?"

    def test_sender_extraction(self):
        msg = _mcp_message()
        msg["headers"]["From"] = "John Doe <john@company.com>"
        result = parse_email(msg)
        assert result["sender"] == "john@company.com"

    def test_plain_sender(self):
        msg = _mcp_message()
        msg["headers"]["From"] = "plain@email.com"
        result = parse_email(msg)
        assert result["sender"] == "plain@email.com"

    def test_internal_date_parsing(self):
        result = parse_email(_mcp_message())
        assert (
            "2026-03-06" in result["received_at"] or "2026-03" in result["received_at"]
        )

    def test_labels_preserved(self):
        result = parse_email(_mcp_message())
        assert "INBOX" in result["labels"]


class TestIsActionable:
    def test_normal_email(self):
        assert is_actionable({"subject": "Project update needed"})

    def test_calendar_accepted(self):
        assert not is_actionable({"subject": "Accepted: Meeting @ Mon Mar 9, 2026 5pm"})

    def test_calendar_declined(self):
        assert not is_actionable({"subject": "Declined: Team standup"})

    def test_auto_reply(self):
        assert not is_actionable({"subject": "Automatic Reply: OOO"})

    def test_out_of_office(self):
        assert not is_actionable({"subject": "Out of Office: John Doe"})

    def test_undeliverable(self):
        assert not is_actionable({"subject": "Undeliverable: Your message"})


class TestEstimatePriority:
    def test_important_label(self):
        assert (
            estimate_priority(
                {"labels": ["IMPORTANT"], "subject": "Hello", "snippet": ""}
            )
            == "P1"
        )

    def test_urgent_subject(self):
        assert (
            estimate_priority(
                {"labels": [], "subject": "URGENT: Server down", "snippet": ""}
            )
            == "P1"
        )

    def test_invoice(self):
        assert (
            estimate_priority({"labels": [], "subject": "Invoice #1234", "snippet": ""})
            == "P2"
        )

    def test_zoho_ticket(self):
        assert (
            estimate_priority(
                {"labels": [], "subject": "Re: Support", "snippet": "ticket #ABC-123"}
            )
            == "P2"
        )

    def test_normal_email(self):
        assert (
            estimate_priority(
                {"labels": [], "subject": "Quick question", "snippet": ""}
            )
            is None
        )


class TestCollectEmails:
    def test_collect_writes_to_db(self, db_path):
        config = {"paths": {"cos_dir": str(db_path.parent)}}
        messages = [
            _mcp_message(),
            _mcp_message(
                id="msg_002",
                headers={
                    "Subject": "New feature request",
                    "From": "user@example.com",
                    "Date": "Fri, 06 Mar 2026 15:00:00 +0000",
                },
            ),
        ]
        stats = collect_emails(config, messages)
        assert stats["processed"] == 2

        with connect(db_path) as conn:
            pending = get_pending_items(conn, domain_type="email")
            assert len(pending) == 2

    def test_collect_filters_calendar_responses(self, db_path):
        config = {"paths": {"cos_dir": str(db_path.parent)}}
        messages = [
            _mcp_message(),
            _mcp_message(
                id="msg_calendar",
                headers={
                    "Subject": "Accepted: Meeting @ Mon Mar 9",
                    "From": "attendee@example.com",
                    "Date": "Fri, 06 Mar 2026 15:00:00 +0000",
                },
            ),
        ]
        stats = collect_emails(config, messages)
        assert stats["processed"] == 1
        assert stats["filtered"] == 1

    def test_collect_idempotent(self, db_path):
        config = {"paths": {"cos_dir": str(db_path.parent)}}
        messages = [_mcp_message()]
        collect_emails(config, messages)
        stats2 = collect_emails(config, messages)
        assert stats2["processed"] == 0
        assert stats2["skipped"] == 1
