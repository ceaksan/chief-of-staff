"""Tests for task_collector."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.task_collector import collect, parse_task_line, scan_vault
from cos.db import connect, get_pending_items, init_db


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "cos.db"
    init_db(path)
    return path


@pytest.fixture
def mock_vault(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()

    # Project file with tasks
    projects = vault / "Projects"
    projects.mkdir()
    (projects / "validough.md").write_text(
        "# Validough\n\n"
        "## Tasks\n"
        "- [ ] [P1] Fix checkout flow #validough @due(2026-03-10)\n"
        "- [ ] Add onboarding wizard #validough\n"
        "- [x] Setup CI pipeline\n"  # completed, should be skipped
        "- [ ] [P2] Update pricing page #validough @due(2026-03-15)\n"
    )

    # Daily note with tasks
    daily = vault / "Daily"
    daily.mkdir()
    (daily / "2026-03-07.md").write_text(
        "# 2026-03-07\n\n"
        "- [ ] Review PR from client\n"
        "- [ ] [P3] Write blog post #content\n"
        "- Some regular note text\n"
    )

    # File that should be excluded
    obsidian = vault / ".obsidian"
    obsidian.mkdir()
    (obsidian / "config.md").write_text("- [ ] Should be excluded\n")

    # Nested project
    sub = vault / "Projects" / "SubProject"
    sub.mkdir()
    (sub / "tasks.md").write_text("- [ ] Nested task #subproject\n")

    return vault


class TestParseTaskLine:
    def test_basic_task(self):
        result = parse_task_line("- [ ] Fix the bug", "test.md", 1)
        assert result is not None
        assert result["content"] == "Fix the bug"
        assert result["priority"] is None
        assert result["due_date"] is None
        assert result["project"] is None

    def test_task_with_priority(self):
        result = parse_task_line("- [ ] [P1] Critical fix #myapp", "test.md", 5)
        assert result["priority"] == "P1"
        assert result["content"] == "Critical fix #myapp"
        assert result["project"] == "myapp"

    def test_task_with_due_date(self):
        result = parse_task_line("- [ ] Deploy update @due(2026-03-10)", "test.md", 1)
        assert result["due_date"] == "2026-03-10"
        assert result["content"] == "Deploy update"

    def test_task_with_everything(self):
        result = parse_task_line(
            "- [ ] [P2] Fix checkout #validough @due(2026-03-15)", "proj.md", 10
        )
        assert result["priority"] == "P2"
        assert result["due_date"] == "2026-03-15"
        assert result["project"] == "validough"
        assert result["content"] == "Fix checkout #validough"
        assert result["file_path"] == "proj.md"
        assert result["line_number"] == 10

    def test_completed_task_ignored(self):
        result = parse_task_line("- [x] Done task", "test.md", 1)
        assert result is None

    def test_regular_text_ignored(self):
        result = parse_task_line("Some regular text", "test.md", 1)
        assert result is None

    def test_bullet_without_checkbox_ignored(self):
        result = parse_task_line("- Regular bullet point", "test.md", 1)
        assert result is None

    def test_indented_task(self):
        result = parse_task_line("  - [ ] Sub-task", "test.md", 1)
        assert result is not None
        assert result["content"] == "Sub-task"

    def test_empty_task_ignored(self):
        result = parse_task_line("- [ ] ", "test.md", 1)
        assert result is None

    def test_multiple_tags(self):
        result = parse_task_line("- [ ] Fix bug #validough #backend", "test.md", 1)
        assert result["project"] == "validough"  # first tag = project

    def test_deterministic_id(self):
        r1 = parse_task_line("- [ ] Same task", "same.md", 1)
        r2 = parse_task_line("- [ ] Same task", "same.md", 5)
        assert r1["id"] == r2["id"]  # same file+content = same id

        r3 = parse_task_line("- [ ] Same task", "different.md", 1)
        assert r1["id"] != r3["id"]  # different file = different id


class TestScanVault:
    def test_finds_all_tasks(self, mock_vault):
        tasks = scan_vault(mock_vault)
        contents = [t["content"] for t in tasks]

        assert "Fix checkout flow #validough" in contents
        assert "Add onboarding wizard #validough" in contents
        assert "Update pricing page #validough" in contents
        assert "Review PR from client" in contents
        assert "Write blog post #content" in contents
        assert "Nested task #subproject" in contents

    def test_skips_completed_tasks(self, mock_vault):
        tasks = scan_vault(mock_vault)
        contents = [t["content"] for t in tasks]
        assert "Setup CI pipeline" not in contents

    def test_skips_excluded_dirs(self, mock_vault):
        tasks = scan_vault(mock_vault)
        contents = [t["content"] for t in tasks]
        assert "Should be excluded" not in contents

    def test_task_count(self, mock_vault):
        tasks = scan_vault(mock_vault)
        assert len(tasks) == 6


class TestCollect:
    def test_collect_writes_to_db(self, db_path, mock_vault):
        config = {
            "paths": {
                "obsidian_vault": str(mock_vault),
                "cos_dir": str(db_path.parent),
            }
        }
        stats = collect(config)
        assert stats["processed"] == 6

        with connect(db_path) as conn:
            pending = get_pending_items(conn, domain_type="task")
            assert len(pending) == 6

    def test_collect_idempotent(self, db_path, mock_vault):
        config = {
            "paths": {
                "obsidian_vault": str(mock_vault),
                "cos_dir": str(db_path.parent),
            }
        }
        collect(config)
        stats2 = collect(config)
        assert stats2["processed"] == 0
        assert stats2["skipped"] == 6

    def test_collect_marks_removed_tasks_done(self, db_path, mock_vault):
        config = {
            "paths": {
                "obsidian_vault": str(mock_vault),
                "cos_dir": str(db_path.parent),
            }
        }
        collect(config)

        # Remove a task file
        (mock_vault / "Projects" / "SubProject" / "tasks.md").unlink()

        collect(config)

        with connect(db_path) as conn:
            pending = get_pending_items(conn, domain_type="task")
            assert len(pending) == 5  # 6 - 1 removed

            done = conn.execute(
                "SELECT COUNT(*) as c FROM work_queue WHERE domain_type = 'task' AND status = 'done'"
            ).fetchone()
            assert done["c"] == 1

    def test_collect_missing_vault(self, db_path, tmp_path):
        config = {
            "paths": {
                "obsidian_vault": str(tmp_path / "nonexistent"),
                "cos_dir": str(db_path.parent),
            }
        }
        stats = collect(config)
        assert stats["processed"] == 0
