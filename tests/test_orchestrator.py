"""Tests for collectors.orchestrator module."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.orchestrator import (
    _extract_json_from_output,
    get_agent_config,
    group_items_by_agent,
)


# --- group_items_by_agent ---


def test_group_items_by_agent():
    items = {
        "dispatch": [
            {"queue_id": 1, "domain_type": "email", "title": "Email 1"},
            {"queue_id": 2, "domain_type": "health", "title": "Health 1"},
        ],
        "prep": [
            {"queue_id": 3, "domain_type": "event", "title": "Event 1"},
            {"queue_id": 4, "domain_type": "task", "title": "Task 1"},
        ],
    }
    result = group_items_by_agent(items)

    # email excluded from agent dispatch (classification only)
    assert "email" not in result
    assert "health" in result
    assert "calendar" in result
    assert "task" in result
    assert len(result["health"]) == 1
    assert len(result["calendar"]) == 1
    assert len(result["task"]) == 1


def test_group_items_empty():
    result = group_items_by_agent({"dispatch": [], "prep": []})
    assert result == {}


def test_group_items_radar_maps_to_feed():
    items = {
        "dispatch": [{"queue_id": 1, "domain_type": "radar", "title": "Radar 1"}],
        "prep": [],
    }
    result = group_items_by_agent(items)
    assert "feed" in result
    assert len(result["feed"]) == 1
    assert result["feed"][0]["queue_id"] == 1


def test_group_items_feed_and_radar_merge():
    items = {
        "dispatch": [
            {"queue_id": 1, "domain_type": "feed", "title": "Feed 1"},
            {"queue_id": 2, "domain_type": "radar", "title": "Radar 1"},
        ],
        "prep": [],
    }
    result = group_items_by_agent(items)
    assert "feed" in result
    assert len(result["feed"]) == 2


def test_group_items_unknown_domain_skipped():
    items = {
        "dispatch": [{"queue_id": 1, "domain_type": "unknown_xyz", "title": "?"}],
        "prep": [],
    }
    result = group_items_by_agent(items)
    assert result == {}


def test_group_items_task_domain():
    items = {
        "dispatch": [],
        "prep": [{"queue_id": 5, "domain_type": "task", "title": "Task 1"}],
    }
    result = group_items_by_agent(items)
    assert "task" in result
    assert len(result["task"]) == 1
    assert result["task"][0]["category"] == "prep"


# --- get_agent_config ---


def test_get_agent_config_with_defaults():
    config = {}
    result = get_agent_config(config, "email")
    assert result["budget"] == 0.50
    assert result["model"] == "sonnet"
    assert result["timeout"] == 180


def test_get_agent_config_with_overrides():
    config = {
        "agents": {
            "email": {
                "budget": 1.00,
                "model": "opus",
                "timeout": 300,
            }
        }
    }
    result = get_agent_config(config, "email")
    assert result["budget"] == 1.00
    assert result["model"] == "opus"
    assert result["timeout"] == 300


def test_get_agent_config_partial_overrides():
    config = {
        "agents": {
            "calendar": {
                "budget": 0.75,
            }
        }
    }
    result = get_agent_config(config, "calendar")
    assert result["budget"] == 0.75
    assert result["model"] == "sonnet"
    assert result["timeout"] == 180


def test_get_agent_config_missing_agent_section():
    config = {"agents": {"email": {"budget": 1.00}}}
    result = get_agent_config(config, "health")
    assert result["budget"] == 0.50
    assert result["model"] == "sonnet"
    assert result["timeout"] == 180


# --- _extract_json_from_output ---


def test_extract_json_clean():
    output = '[{"queue_id": 1, "action_type": "draft_created"}]'
    result = _extract_json_from_output(output)
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["queue_id"] == 1
    assert result[0]["action_type"] == "draft_created"


def test_extract_json_embedded_in_text():
    output = """
I processed the items and here are the actions:
[{"queue_id": 2, "agent": "email", "action_type": "replied"}, {"queue_id": 3, "agent": "email", "action_type": "skipped"}]
Done processing.
"""
    result = _extract_json_from_output(output)
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["queue_id"] == 2
    assert result[1]["queue_id"] == 3


def test_extract_json_no_json():
    output = "No actions were taken. Everything looked fine."
    result = _extract_json_from_output(output)
    assert result == []


def test_extract_json_empty_string():
    result = _extract_json_from_output("")
    assert result == []


def test_extract_json_empty_array():
    result = _extract_json_from_output("[]")
    assert result == []


def test_extract_json_nested_objects():
    output = '[{"queue_id": 1, "meta": {"key": "value"}}]'
    result = _extract_json_from_output(output)
    assert len(result) == 1
    assert result[0]["meta"]["key"] == "value"


def test_extract_json_malformed():
    output = "[not valid json {"
    result = _extract_json_from_output(output)
    assert result == []
