"""Tests for cos.db module."""

import sqlite3
from pathlib import Path

import pytest

from cos.db import (
    classify_item,
    connect,
    content_hash,
    finish_run,
    get_active_queue,
    get_pending_items,
    get_today_briefing,
    init_db,
    insert_email,
    insert_event,
    insert_health_check,
    insert_task,
    is_cached,
    record_action,
    start_run,
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    return path


@pytest.fixture
def conn(db_path):
    with connect(db_path) as c:
        yield c


def _sample_email(**overrides):
    base = {
        "id": "msg_001",
        "thread_id": "thread_001",
        "subject": "Re: Hosting migration",
        "sender": "client@example.com",
        "snippet": "Can we move to next week?",
        "labels": ["client"],
        "received_at": "2026-03-08T06:00:00Z",
    }
    base.update(overrides)
    return base


def _sample_event(**overrides):
    base = {
        "id": "evt_001",
        "calendar_id": "primary",
        "summary": "Client X meeting",
        "start_time": "2026-03-08T10:00:00+03:00",
        "end_time": "2026-03-08T11:00:00+03:00",
        "location": "Google Meet",
        "is_calendly": True,
        "prep_needed": True,
    }
    base.update(overrides)
    return base


def _sample_task(**overrides):
    base = {
        "id": "task_abc123",
        "file_path": "Projects/validough.md",
        "line_number": 42,
        "content": "Fix checkout flow",
        "project": "validough",
    }
    base.update(overrides)
    return base


def _sample_health(**overrides):
    base = {
        "id": "validough_2026-03-08",
        "project": "validough",
        "status": "warning",
        "uptime": 99.2,
        "errors_24h": 3,
        "last_error": "Neon connection timeout",
        "last_deploy": "2026-03-07T14:30:00Z",
        "checked_at": "2026-03-08T06:00:00Z",
    }
    base.update(overrides)
    return base


class TestSchema:
    def test_init_creates_tables(self, conn):
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        assert "emails" in tables
        assert "events" in tables
        assert "tasks" in tables
        assert "health_checks" in tables
        assert "work_queue" in tables
        assert "classifications" in tables
        assert "actions" in tables
        assert "runs" in tables

    def test_wal_mode(self, conn):
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_foreign_keys_on(self, conn):
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1


class TestInserts:
    def test_insert_email(self, conn):
        queue_id = insert_email(conn, _sample_email())
        assert queue_id is not None

        row = conn.execute("SELECT * FROM emails WHERE id = 'msg_001'").fetchone()
        assert row["subject"] == "Re: Hosting migration"
        assert row["sender"] == "client@example.com"

        wq = conn.execute(
            "SELECT * FROM work_queue WHERE id = ?", (queue_id,)
        ).fetchone()
        assert wq["domain_type"] == "email"
        assert wq["domain_id"] == "msg_001"

    def test_insert_email_duplicate(self, conn):
        insert_email(conn, _sample_email())
        result = insert_email(conn, _sample_email())
        assert result is None

    def test_insert_event(self, conn):
        queue_id = insert_event(conn, _sample_event())
        assert queue_id is not None

        row = conn.execute("SELECT * FROM events WHERE id = 'evt_001'").fetchone()
        assert row["summary"] == "Client X meeting"
        assert row["is_calendly"] == 1

    def test_insert_task(self, conn):
        queue_id = insert_task(conn, _sample_task())
        assert queue_id is not None

        row = conn.execute("SELECT * FROM tasks WHERE id = 'task_abc123'").fetchone()
        assert row["content"] == "Fix checkout flow"

    def test_insert_health_check(self, conn):
        queue_id = insert_health_check(conn, _sample_health())
        assert queue_id is not None

        row = conn.execute(
            "SELECT * FROM health_checks WHERE id = 'validough_2026-03-08'"
        ).fetchone()
        assert row["status"] == "warning"
        assert row["errors_24h"] == 3


class TestClassification:
    def test_classify_item(self, conn):
        queue_id = insert_email(conn, _sample_email())
        classify_item(
            conn,
            queue_id,
            "dispatch",
            reason="Meeting confirmation",
            model="claude-sonnet-4-20250514",
        )

        cls = conn.execute(
            "SELECT * FROM classifications WHERE queue_id = ?", (queue_id,)
        ).fetchone()
        assert cls["category"] == "dispatch"
        assert cls["reason"] == "Meeting confirmation"

        wq = conn.execute(
            "SELECT * FROM work_queue WHERE id = ?", (queue_id,)
        ).fetchone()
        assert wq["status"] == "classified"

    def test_multiple_classifications(self, conn):
        queue_id = insert_email(conn, _sample_email())
        classify_item(conn, queue_id, "prep", reason="First pass")
        classify_item(conn, queue_id, "dispatch", reason="Reclassified")

        rows = conn.execute(
            "SELECT * FROM classifications WHERE queue_id = ? ORDER BY created_at",
            (queue_id,),
        ).fetchall()
        assert len(rows) == 2
        assert rows[-1]["category"] == "dispatch"


class TestCaching:
    def test_is_cached_no_classification(self, conn):
        assert not is_cached(conn, "email", "msg_001", _sample_email())

    def test_is_cached_same_content(self, conn):
        email = _sample_email()
        queue_id = insert_email(conn, email)
        classify_item(conn, queue_id, "dispatch")
        assert is_cached(conn, "email", "msg_001", email)

    def test_is_cached_changed_content(self, conn):
        email = _sample_email()
        queue_id = insert_email(conn, email)
        classify_item(conn, queue_id, "dispatch")
        changed = _sample_email(subject="Updated subject")
        assert not is_cached(conn, "email", "msg_001", changed)


class TestActions:
    def test_record_action(self, conn):
        queue_id = insert_email(conn, _sample_email())
        action_id = record_action(
            conn,
            queue_id,
            agent="email",
            action_type="draft_created",
            external_ref="draft_xyz",
            output_summary="Reply draft for hosting migration",
        )
        assert action_id is not None

        row = conn.execute(
            "SELECT * FROM actions WHERE id = ?", (action_id,)
        ).fetchone()
        assert row["agent"] == "email"
        assert row["external_ref"] == "draft_xyz"


class TestRuns:
    def test_run_lifecycle(self, conn):
        run_id = start_run(conn, "collector", source="gmail")
        assert run_id is not None

        finish_run(
            conn, run_id, status="completed", items_processed=15, budget_used=0.42
        )

        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        assert row["status"] == "completed"
        assert row["items_processed"] == 15
        assert row["budget_used"] == 0.42
        assert row["finished_at"] is not None


class TestQueries:
    def test_get_pending_items(self, conn):
        insert_email(conn, _sample_email())
        insert_event(conn, _sample_event())

        pending = get_pending_items(conn)
        assert len(pending) == 2

        emails_only = get_pending_items(conn, domain_type="email")
        assert len(emails_only) == 1

    def test_content_hash_deterministic(self):
        data = {"a": 1, "b": "test"}
        assert content_hash(data) == content_hash(data)
        assert content_hash(data) != content_hash({"a": 2})
