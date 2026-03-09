"""Tests for collectors.sweep module."""

import json
from pathlib import Path

import pytest

from cos.db import (
    classify_item,
    connect,
    init_db,
    insert_email,
    insert_event,
    insert_task,
    insert_health_check,
    record_action,
)
from collectors.sweep import (
    apply_actions,
    export_sweep_items,
    export_yours_items,
    mark_done,
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "cos.db"
    init_db(path)
    return path


@pytest.fixture
def config(tmp_path):
    return {
        "paths": {"cos_dir": str(tmp_path)},
    }


def _setup_classified_email(conn, email_id, subject, category, sender="a@b.com"):
    insert_email(
        conn,
        {
            "id": email_id,
            "thread_id": f"thread_{email_id}",
            "subject": subject,
            "sender": sender,
            "snippet": "snippet",
            "labels": ["INBOX"],
            "received_at": "2026-03-09T08:00:00Z",
        },
    )
    qid = conn.execute(
        "SELECT id FROM work_queue WHERE domain_id = ?", (email_id,)
    ).fetchone()["id"]
    classify_item(conn, qid, category, f"test: {category}")
    return qid


def _setup_classified_task(conn, task_id, content, category, project="myproj"):
    insert_task(
        conn,
        {
            "id": task_id,
            "file_path": "/vault/tasks.md",
            "content": content,
            "project": project,
        },
    )
    qid = conn.execute(
        "SELECT id FROM work_queue WHERE domain_id = ?", (task_id,)
    ).fetchone()["id"]
    classify_item(conn, qid, category, f"test: {category}")
    return qid


def _setup_classified_health(conn, check_id, project, status, category):
    insert_health_check(
        conn,
        {
            "id": check_id,
            "project": project,
            "status": status,
            "errors_24h": 3 if status != "ok" else 0,
            "last_error": "Connection timeout" if status != "ok" else None,
            "checked_at": "2026-03-09T06:00:00Z",
        },
    )
    qid = conn.execute(
        "SELECT id FROM work_queue WHERE domain_id = ?", (check_id,)
    ).fetchone()["id"]
    classify_item(conn, qid, category, f"test: {category}")
    return qid


# --- export_sweep_items ---


class TestExportSweepItems:
    def test_exports_dispatch_and_prep(self, config, db_path):
        with connect(db_path) as conn:
            _setup_classified_email(conn, "e1", "Meeting confirmation", "dispatch")
            _setup_classified_email(conn, "e2", "Client proposal", "prep")
            _setup_classified_email(conn, "e3", "Pricing decision", "yours")
            _setup_classified_email(conn, "e4", "Newsletter", "skip")

        items = export_sweep_items(config)
        assert len(items["dispatch"]) == 1
        assert len(items["prep"]) == 1
        assert items["dispatch"][0]["title"] == "Meeting confirmation"
        assert items["prep"][0]["title"] == "Client proposal"

    def test_excludes_pending_items(self, config, db_path):
        with connect(db_path) as conn:
            insert_email(
                conn,
                {
                    "id": "e_pending",
                    "thread_id": "t_pending",
                    "subject": "Not classified",
                    "sender": "a@b.com",
                    "snippet": "s",
                    "labels": [],
                    "received_at": "2026-03-09T08:00:00Z",
                },
            )

        items = export_sweep_items(config)
        assert len(items["dispatch"]) == 0
        assert len(items["prep"]) == 0

    def test_includes_extra_field(self, config, db_path):
        with connect(db_path) as conn:
            _setup_classified_email(conn, "e1", "Test", "dispatch")

        items = export_sweep_items(config)
        assert items["dispatch"][0]["extra"] is not None  # thread_id

    def test_empty_db(self, config, db_path):
        items = export_sweep_items(config)
        assert items == {"dispatch": [], "prep": []}

    def test_mixed_domain_types(self, config, db_path):
        with connect(db_path) as conn:
            _setup_classified_email(conn, "e1", "Email dispatch", "dispatch")
            _setup_classified_task(conn, "t1", "Task dispatch", "dispatch")
            _setup_classified_health(conn, "h1", "myapp", "warning", "prep")

        items = export_sweep_items(config)
        assert len(items["dispatch"]) == 2
        assert len(items["prep"]) == 1


# --- export_yours_items ---


class TestExportYoursItems:
    def test_exports_yours_only(self, config, db_path):
        with connect(db_path) as conn:
            _setup_classified_email(conn, "e1", "Dispatch item", "dispatch")
            _setup_classified_email(conn, "e2", "Yours item", "yours")

        items = export_yours_items(config)
        assert len(items) == 1
        assert items[0]["title"] == "Yours item"

    def test_empty(self, config, db_path):
        items = export_yours_items(config)
        assert items == []


# --- apply_actions ---


class TestApplyActions:
    def test_records_action(self, config, db_path):
        with connect(db_path) as conn:
            qid = _setup_classified_email(conn, "e1", "Test", "dispatch")

        actions = [
            {
                "queue_id": qid,
                "agent": "email",
                "action_type": "draft_created",
                "external_ref": "draft_123",
                "output_summary": "Reply draft created",
            }
        ]
        stats = apply_actions(config, actions)
        assert stats["recorded"] == 1
        assert stats["failed"] == 0

        with connect(db_path) as conn:
            action = conn.execute(
                "SELECT * FROM actions WHERE queue_id = ?", (qid,)
            ).fetchone()
            assert dict(action)["agent"] == "email"
            wq = conn.execute(
                "SELECT status FROM work_queue WHERE id = ?", (qid,)
            ).fetchone()
            assert dict(wq)["status"] == "dispatched"

    def test_missing_queue_id(self, config, db_path):
        stats = apply_actions(config, [{"agent": "email", "action_type": "test"}])
        assert stats["failed"] == 1
        assert stats["recorded"] == 0

    def test_invalid_queue_id(self, config, db_path):
        stats = apply_actions(
            config,
            [
                {
                    "queue_id": 99999,
                    "agent": "email",
                    "action_type": "test",
                }
            ],
        )
        assert stats["failed"] == 1

    def test_creates_run_record(self, config, db_path):
        with connect(db_path) as conn:
            qid = _setup_classified_email(conn, "e1", "Test", "dispatch")

        apply_actions(
            config,
            [
                {
                    "queue_id": qid,
                    "agent": "email",
                    "action_type": "draft_created",
                }
            ],
        )

        with connect(db_path) as conn:
            run = conn.execute("SELECT * FROM runs WHERE layer = 'sweep'").fetchone()
            assert run is not None
            assert dict(run)["status"] == "completed"

    def test_empty_actions(self, config, db_path):
        stats = apply_actions(config, [])
        assert stats["recorded"] == 0
        assert stats["failed"] == 0


# --- mark_done ---


class TestMarkDone:
    def test_marks_items_done(self, config, db_path):
        with connect(db_path) as conn:
            qid1 = _setup_classified_email(conn, "e1", "Test 1", "dispatch")
            qid2 = _setup_classified_email(conn, "e2", "Test 2", "dispatch")

        stats = mark_done(config, [qid1, qid2])
        assert stats["done"] == 2
        assert stats["failed"] == 0

        with connect(db_path) as conn:
            for qid in [qid1, qid2]:
                row = conn.execute(
                    "SELECT status FROM work_queue WHERE id = ?", (qid,)
                ).fetchone()
                assert dict(row)["status"] == "done"

    def test_nonexistent_id(self, config, db_path):
        # SQLite UPDATE on non-existent row doesn't raise, affects 0 rows
        stats = mark_done(config, [99999])
        assert stats["done"] == 0  # not counted as done

    def test_empty_ids(self, config, db_path):
        stats = mark_done(config, [])
        assert stats["done"] == 0
