"""Tests for health_collector."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.health_collector import collect, run_health_script
from cos.db import connect, get_pending_items, init_db


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "cos.db"
    init_db(path)
    return path


@pytest.fixture
def mock_health_script(tmp_path):
    """Create a mock health script that outputs JSON."""
    script = tmp_path / "health.py"
    script.write_text(
        'import json; print(json.dumps({"status": "ok", "uptime": 99.9, "errors_24h": 0, "last_deploy": "2026-03-07T14:30:00Z"}))'
    )
    return script


@pytest.fixture
def mock_warning_script(tmp_path):
    script = tmp_path / "health_warn.py"
    script.write_text(
        'import json; print(json.dumps({"status": "warning", "uptime": 95.0, "errors_24h": 3, "last_error": "Connection timeout"}))'
    )
    return script


@pytest.fixture
def mock_failing_script(tmp_path):
    script = tmp_path / "health_fail.py"
    script.write_text('import sys; print("error", file=sys.stderr); sys.exit(1)')
    return script


@pytest.fixture
def mock_bad_json_script(tmp_path):
    script = tmp_path / "health_bad.py"
    script.write_text('print("not json at all")')
    return script


class TestRunHealthScript:
    def test_ok_script(self, mock_health_script):
        result = run_health_script("testproject", str(mock_health_script))
        assert result is not None
        assert result["status"] == "ok"
        assert result["uptime"] == 99.9
        assert result["project"] == "testproject"
        assert result["priority"] is None

    def test_warning_script(self, mock_warning_script):
        result = run_health_script("testproject", str(mock_warning_script))
        assert result["status"] == "warning"
        assert result["errors_24h"] == 3

    def test_failing_script(self, mock_failing_script):
        result = run_health_script("testproject", str(mock_failing_script))
        assert result is not None
        assert result["status"] == "error"
        assert "code 1" in result["last_error"]

    def test_bad_json_script(self, mock_bad_json_script):
        result = run_health_script("testproject", str(mock_bad_json_script))
        assert result is not None
        assert result["status"] == "error"
        assert "invalid JSON" in result["last_error"]

    def test_missing_script(self):
        result = run_health_script("testproject", "/nonexistent/health.py")
        assert result is None

    def test_down_status_gets_p1(self, tmp_path):
        script = tmp_path / "down.py"
        script.write_text('import json; print(json.dumps({"status": "down"}))')
        result = run_health_script("testproject", str(script))
        assert result["status"] == "down"
        assert result["priority"] == "P1"


class TestCollect:
    def test_no_projects_configured(self, db_path):
        config = {"paths": {"cos_dir": str(db_path.parent)}, "health": {}}
        stats = collect(config)
        assert stats["processed"] == 0
        assert stats["skipped"] == 0

    def test_collect_writes_to_db(
        self, db_path, mock_health_script, mock_warning_script
    ):
        config = {
            "paths": {"cos_dir": str(db_path.parent)},
            "health": {
                "projects": {
                    "project_a": str(mock_health_script),
                    "project_b": str(mock_warning_script),
                }
            },
        }
        stats = collect(config)
        assert stats["processed"] == 2

        with connect(db_path) as conn:
            pending = get_pending_items(conn, domain_type="health")
            assert len(pending) == 2

    def test_collect_handles_mixed_results(self, db_path, mock_health_script):
        config = {
            "paths": {"cos_dir": str(db_path.parent)},
            "health": {
                "projects": {
                    "good": str(mock_health_script),
                    "missing": "/nonexistent/health.py",
                }
            },
        }
        stats = collect(config)
        assert stats["processed"] == 1
        assert stats["skipped"] == 1
