"""Tests for renderer."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from cos.db import (
    classify_item,
    connect,
    finish_run,
    init_db,
    insert_email,
    insert_event,
    insert_feed,
    insert_health_check,
    insert_task,
    record_action,
    start_run,
)
from renderer import render, write_daily_note

TARGET_DATE = datetime.now(timezone.utc).strftime("%Y-%m-%d")


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    return path


@pytest.fixture
def conn(db_path):
    with connect(db_path) as c:
        yield c


@pytest.fixture
def populated_db(conn):
    """Insert sample data for rendering tests."""
    # Events
    insert_event(
        conn,
        {
            "id": "evt_1",
            "calendar_id": "primary",
            "summary": "Client X meeting",
            "start_time": f"{TARGET_DATE}T10:00:00+03:00",
            "end_time": f"{TARGET_DATE}T11:00:00+03:00",
            "location": "Google Meet",
            "is_calendly": True,
            "prep_needed": True,
        },
    )
    insert_event(
        conn,
        {
            "id": "evt_2",
            "calendar_id": "primary",
            "summary": "Deploy review",
            "start_time": f"{TARGET_DATE}T14:00:00+03:00",
            "end_time": f"{TARGET_DATE}T14:30:00+03:00",
        },
    )

    # Health
    insert_health_check(
        conn,
        {
            "id": f"leetty_{TARGET_DATE}",
            "project": "leetty",
            "status": "ok",
            "uptime": 99.9,
            "errors_24h": 0,
            "checked_at": f"{TARGET_DATE}T06:00:00Z",
        },
    )
    insert_health_check(
        conn,
        {
            "id": f"validough_{TARGET_DATE}",
            "project": "validough",
            "status": "warning",
            "uptime": 95.0,
            "errors_24h": 3,
            "last_error": "Neon connection timeout",
            "checked_at": f"{TARGET_DATE}T06:00:00Z",
        },
    )

    # Email (classified)
    q1 = insert_email(
        conn,
        {
            "id": "msg_1",
            "subject": "Meeting confirmation",
            "sender": "client@example.com",
            "received_at": f"{TARGET_DATE}T05:00:00Z",
        },
    )
    classify_item(conn, q1, "dispatch", reason="Simple confirmation reply")

    # Task (classified)
    q2 = insert_task(
        conn,
        {
            "id": "task_1",
            "file_path": "Projects/validough.md",
            "content": "Fix checkout flow",
            "project": "validough",
        },
    )
    classify_item(conn, q2, "yours", reason="Needs active development")

    return conn


class TestRender:
    def test_empty_db(self, conn):
        content = render(conn, TARGET_DATE)
        assert f"# {TARGET_DATE}" in content
        assert "## Calendar" in content
        assert "No events" in content
        assert "No health data" in content
        assert "Not yet classified" in content

    def test_with_data(self, populated_db):
        content = render(populated_db, TARGET_DATE)

        # Calendar section
        assert "Client X meeting" in content
        assert "Google Meet" in content
        assert "Calendly" in content
        assert "**prep needed**" in content
        assert "Deploy review" in content

        # Health section
        assert "OK: leetty" in content
        assert "validough: warning" in content
        assert "Neon connection timeout" in content

        # Classified section
        assert "DISPATCH" in content
        assert "Meeting confirmation" in content
        assert "YOURS" in content
        assert "Fix checkout flow" in content

    def test_run_warnings(self, conn):
        run_id = start_run(conn, "collector", source="gmail")
        finish_run(conn, run_id, status="failed", error="MCP auth expired")
        content = render(conn, datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        assert "Collection Issues" in content
        assert "MCP auth expired" in content

    def test_feed_in_classified(self, conn):
        """Feed items show real titles in classified section, not 'Untitled'."""
        q = insert_feed(
            conn,
            {
                "id": "feed_1",
                "feed_id": 10,
                "feed_title": "TechCrunch",
                "title": "AI startup raises $50M",
                "url": "https://example.com/article",
                "published_at": f"{TARGET_DATE}T08:00:00Z",
                "reading_time": 3,
                "priority": "P3",
            },
        )
        classify_item(conn, q, "skip", reason="Industry news, not actionable")
        content = render(conn, TARGET_DATE)
        assert "AI startup raises $50M" in content
        assert "Untitled" not in content

    def test_feed_in_carried_over(self, db_path):
        """Feed items carried over show real titles."""
        from datetime import timedelta

        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
        with connect(db_path) as conn:
            insert_feed(
                conn,
                {
                    "id": "feed_old",
                    "feed_id": 10,
                    "feed_title": "HackerNews",
                    "title": "Show HN: My side project",
                    "url": "https://example.com/hn",
                    "published_at": f"{yesterday}T12:00:00Z",
                    "reading_time": 2,
                    "priority": "P3",
                },
            )
            # Backdate the work_queue entry
            conn.execute(
                "UPDATE work_queue SET collected_at = ? WHERE domain_type = 'feed' AND domain_id = 'feed_old'",
                (f"{yesterday}T12:00:00",),
            )

            content = render(conn, TARGET_DATE)
            assert "Show HN: My side project" in content
            assert "#feed" in content

    def test_feed_highlights_section(self, conn):
        """Feed highlights section shows feed titles and reading time."""
        insert_feed(
            conn,
            {
                "id": "feed_h1",
                "feed_id": 10,
                "feed_title": "Ars Technica",
                "title": "New chip breakthrough",
                "url": "https://example.com/chip",
                "published_at": f"{TARGET_DATE}T09:00:00Z",
                "reading_time": 5,
                "priority": "P4",
            },
        )
        content = render(conn, TARGET_DATE)
        assert "Feed Highlights" in content
        assert "New chip breakthrough" in content
        assert "Ars Technica" in content
        assert "~5min" in content

    def test_actions_section(self, populated_db):
        record_action(
            populated_db,
            queue_id=1,
            agent="email",
            action_type="draft_created",
            output_summary="Reply draft for meeting confirmation",
        )
        content = render(populated_db, TARGET_DATE)
        assert "Agent Actions" in content
        assert "email" in content
        assert "Reply draft" in content


class TestWriteDailyNote:
    def test_creates_new_note(self, tmp_path):
        config = {
            "paths": {
                "obsidian_vault": str(tmp_path / "vault"),
                "daily_notes_dir": "Daily",
            }
        }
        content = "# 2026-03-08\n\n## Calendar\n- No events"
        note_path = write_daily_note(config, content, "2026-03-08")

        assert note_path.exists()
        text = note_path.read_text()
        assert "<!-- cos:start -->" in text
        assert "<!-- cos:end -->" in text
        assert "# 2026-03-08" in text

    def test_updates_existing_note_with_markers(self, tmp_path):
        config = {
            "paths": {
                "obsidian_vault": str(tmp_path / "vault"),
                "daily_notes_dir": "Daily",
            }
        }
        daily_dir = tmp_path / "vault" / "Daily"
        daily_dir.mkdir(parents=True)
        note = daily_dir / "2026-03-08.md"
        note.write_text(
            "# My Notes\n\n<!-- cos:start -->\nOLD DATA\n<!-- cos:end -->\n\n## My Stuff\n"
        )

        write_daily_note(config, "NEW DATA", "2026-03-08")

        text = note.read_text()
        assert "NEW DATA" in text
        assert "OLD DATA" not in text
        assert "# My Notes" in text
        assert "## My Stuff" in text

    def test_appends_to_existing_note_without_markers(self, tmp_path):
        config = {
            "paths": {
                "obsidian_vault": str(tmp_path / "vault"),
                "daily_notes_dir": "Daily",
            }
        }
        daily_dir = tmp_path / "vault" / "Daily"
        daily_dir.mkdir(parents=True)
        note = daily_dir / "2026-03-08.md"
        note.write_text("# My existing notes\n\nSome content\n")

        write_daily_note(config, "COS DATA", "2026-03-08")

        text = note.read_text()
        assert "# My existing notes" in text
        assert "COS DATA" in text
        assert "<!-- cos:start -->" in text
