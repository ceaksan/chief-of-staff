"""Tests for collectors.classifier module."""

import json
import sqlite3
from pathlib import Path

import pytest

from cos.db import (
    connect,
    content_hash,
    init_db,
    insert_email,
    insert_task,
    start_run,
    finish_run,
    classify_item,
)
from collectors.classifier import (
    apply_classifications,
    apply_force_rules,
    export_pending,
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "cos.db"
    init_db(path)
    return path


@pytest.fixture
def conn(db_path):
    with connect(db_path) as c:
        yield c


@pytest.fixture
def config(tmp_path):
    return {
        "paths": {"cos_dir": str(tmp_path)},
        "classification": {
            "force_yours": ["dentist", "personal"],
            "force_dispatch": ["zoho", "newsletter"],
        },
    }


def _insert_sample_email(conn, email_id="msg1", subject="Test Email", sender="a@b.com"):
    insert_email(
        conn,
        {
            "id": email_id,
            "thread_id": f"thread_{email_id}",
            "subject": subject,
            "sender": sender,
            "snippet": "snippet text",
            "labels": ["INBOX"],
            "received_at": "2026-03-08T10:00:00Z",
        },
    )


def _insert_sample_task(conn, task_id="task1", content="Fix the bug", project="myproj"):
    insert_task(
        conn,
        {
            "id": task_id,
            "file_path": "/vault/tasks.md",
            "content": content,
            "project": project,
        },
    )


# --- export_pending ---


class TestExportPending:
    def test_exports_pending_items(self, config, db_path):
        with connect(db_path) as conn:
            _insert_sample_email(conn, "e1", "Hello World")
            _insert_sample_task(conn, "t1", "Do stuff")

        items = export_pending(config)
        assert len(items) == 2
        types = {i["domain_type"] for i in items}
        assert types == {"email", "task"}

    def test_skips_already_classified(self, config, db_path):
        with connect(db_path) as conn:
            _insert_sample_email(conn, "e1", "Classified Email")
            queue_id = conn.execute(
                "SELECT id FROM work_queue WHERE domain_id = 'e1'"
            ).fetchone()["id"]
            classify_item(conn, queue_id, "dispatch", "test reason")

        items = export_pending(config)
        assert len(items) == 0

    def test_includes_title_and_context(self, config, db_path):
        with connect(db_path) as conn:
            _insert_sample_email(conn, "e1", "Important Subject", "boss@co.com")

        items = export_pending(config)
        assert len(items) == 1
        assert items[0]["title"] == "Important Subject"
        assert items[0]["context"] == "boss@co.com"

    def test_empty_db_returns_empty(self, config, db_path):
        items = export_pending(config)
        assert items == []


# --- apply_classifications ---


class TestApplyClassifications:
    def test_classifies_items(self, config, db_path):
        with connect(db_path) as conn:
            _insert_sample_email(conn, "e1")
            _insert_sample_task(conn, "t1")
            q1 = conn.execute(
                "SELECT id FROM work_queue WHERE domain_id = 'e1'"
            ).fetchone()["id"]
            q2 = conn.execute(
                "SELECT id FROM work_queue WHERE domain_id = 't1'"
            ).fetchone()["id"]

        classifications = [
            {"queue_id": q1, "category": "dispatch", "reason": "AI can handle"},
            {"queue_id": q2, "category": "yours", "reason": "Needs your brain"},
        ]
        stats = apply_classifications(config, classifications, model="test-model")
        assert stats["classified"] == 2
        assert stats["failed"] == 0

        with connect(db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM classifications ORDER BY queue_id"
            ).fetchall()
            assert len(rows) == 2
            assert dict(rows[0])["category"] == "dispatch"
            assert dict(rows[1])["category"] == "yours"

    def test_invalid_category_fails(self, config, db_path):
        with connect(db_path) as conn:
            _insert_sample_email(conn, "e1")
            q1 = conn.execute(
                "SELECT id FROM work_queue WHERE domain_id = 'e1'"
            ).fetchone()["id"]

        stats = apply_classifications(
            config,
            [
                {"queue_id": q1, "category": "invalid", "reason": "bad"},
            ],
        )
        assert stats["failed"] == 1
        assert stats["classified"] == 0

    def test_valid_categories(self, config, db_path):
        with connect(db_path) as conn:
            for i, cat in enumerate(["dispatch", "prep", "yours", "skip"]):
                _insert_sample_email(conn, f"e{i}", f"Email {i}")

            ids = []
            for i in range(4):
                row = conn.execute(
                    f"SELECT id FROM work_queue WHERE domain_id = 'e{i}'"
                ).fetchone()
                ids.append(row["id"])

        classifications = [
            {"queue_id": ids[i], "category": cat, "reason": f"test {cat}"}
            for i, cat in enumerate(["dispatch", "prep", "yours", "skip"])
        ]
        stats = apply_classifications(config, classifications)
        assert stats["classified"] == 4

    def test_records_model_and_prompt_version(self, config, db_path):
        with connect(db_path) as conn:
            _insert_sample_email(conn, "e1")
            q1 = conn.execute(
                "SELECT id FROM work_queue WHERE domain_id = 'e1'"
            ).fetchone()["id"]

        apply_classifications(
            config,
            [{"queue_id": q1, "category": "dispatch", "reason": "test"}],
            model="claude-sonnet",
            prompt_version="abc123",
        )

        with connect(db_path) as conn:
            row = conn.execute(
                "SELECT model, prompt_version FROM classifications"
            ).fetchone()
            assert dict(row)["model"] == "claude-sonnet"
            assert dict(row)["prompt_version"] == "abc123"

    def test_creates_run_record(self, config, db_path):
        with connect(db_path) as conn:
            _insert_sample_email(conn, "e1")
            q1 = conn.execute(
                "SELECT id FROM work_queue WHERE domain_id = 'e1'"
            ).fetchone()["id"]

        apply_classifications(
            config,
            [
                {"queue_id": q1, "category": "yours", "reason": "test"},
            ],
        )

        with connect(db_path) as conn:
            run = conn.execute(
                "SELECT * FROM runs WHERE layer = 'classifier'"
            ).fetchone()
            assert run is not None
            assert dict(run)["status"] == "completed"
            assert dict(run)["items_processed"] == 1

    def test_partial_status_on_failures(self, config, db_path):
        with connect(db_path) as conn:
            _insert_sample_email(conn, "e1")
            q1 = conn.execute(
                "SELECT id FROM work_queue WHERE domain_id = 'e1'"
            ).fetchone()["id"]

        stats = apply_classifications(
            config,
            [
                {"queue_id": q1, "category": "yours", "reason": "ok"},
                {"queue_id": q1 + 999, "category": "dispatch", "reason": "bad id"},
            ],
        )
        assert stats["classified"] == 1
        assert stats["failed"] == 1

        with connect(db_path) as conn:
            run = conn.execute(
                "SELECT status FROM runs WHERE layer = 'classifier'"
            ).fetchone()
            assert dict(run)["status"] == "partial"

    def test_empty_classifications(self, config, db_path):
        stats = apply_classifications(config, [])
        assert stats["classified"] == 0
        assert stats["failed"] == 0


# --- apply_force_rules ---


class TestApplyForceRules:
    def test_force_yours_matches_title(self):
        config = {"classification": {"force_yours": ["dentist"], "force_dispatch": []}}
        items = [{"queue_id": 1, "title": "Dentist appointment", "detail": None}]

        auto, remaining = apply_force_rules(config, items)
        assert len(auto) == 1
        assert auto[0]["category"] == "yours"
        assert remaining == []

    def test_force_dispatch_matches_detail(self):
        config = {"classification": {"force_yours": [], "force_dispatch": ["zoho"]}}
        items = [{"queue_id": 1, "title": "Support ticket", "detail": "Zoho desk #123"}]

        auto, remaining = apply_force_rules(config, items)
        assert len(auto) == 1
        assert auto[0]["category"] == "dispatch"

    def test_force_yours_takes_precedence(self):
        config = {
            "classification": {
                "force_yours": ["meeting"],
                "force_dispatch": ["meeting"],
            }
        }
        items = [{"queue_id": 1, "title": "Meeting prep", "detail": None}]

        auto, remaining = apply_force_rules(config, items)
        assert len(auto) == 1
        assert auto[0]["category"] == "yours"

    def test_no_match_goes_to_remaining(self):
        config = {
            "classification": {"force_yours": ["dentist"], "force_dispatch": ["zoho"]}
        }
        items = [{"queue_id": 1, "title": "Random email", "detail": "Some text"}]

        auto, remaining = apply_force_rules(config, items)
        assert auto == []
        assert len(remaining) == 1

    def test_case_insensitive(self):
        config = {"classification": {"force_yours": ["DENTIST"], "force_dispatch": []}}
        items = [{"queue_id": 1, "title": "dentist visit", "detail": None}]

        auto, remaining = apply_force_rules(config, items)
        assert len(auto) == 1

    def test_matches_context_field(self):
        config = {
            "classification": {
                "force_yours": ["boss@company.com"],
                "force_dispatch": [],
            }
        }
        items = [
            {
                "queue_id": 1,
                "title": "Generic subject",
                "detail": None,
                "context": "boss@company.com",
            }
        ]

        auto, remaining = apply_force_rules(config, items)
        assert len(auto) == 1
        assert auto[0]["category"] == "yours"

    def test_empty_config(self):
        config = {"classification": {}}
        items = [{"queue_id": 1, "title": "Anything", "detail": None}]

        auto, remaining = apply_force_rules(config, items)
        assert auto == []
        assert len(remaining) == 1

    def test_none_title_and_detail(self):
        config = {"classification": {"force_yours": ["test"], "force_dispatch": []}}
        items = [{"queue_id": 1, "title": None, "detail": None}]

        auto, remaining = apply_force_rules(config, items)
        assert auto == []
        assert len(remaining) == 1

    def test_multiple_items_mixed(self):
        config = {
            "classification": {
                "force_yours": ["personal"],
                "force_dispatch": ["newsletter"],
            }
        }
        items = [
            {"queue_id": 1, "title": "Personal task", "detail": None},
            {"queue_id": 2, "title": "Weekly newsletter", "detail": None},
            {"queue_id": 3, "title": "Important project", "detail": None},
        ]

        auto, remaining = apply_force_rules(config, items)
        assert len(auto) == 2
        assert len(remaining) == 1
        assert auto[0]["category"] == "yours"
        assert auto[1]["category"] == "dispatch"
        assert remaining[0]["queue_id"] == 3
