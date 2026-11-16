"""Boot strips volume_control from a persisted Control-UI tool override."""

from __future__ import annotations

from pathlib import Path

from reachy_mini_conversation_app.profile_toolsets import (
    read_profile_tool_names,
    write_profile_tool_override,
)

from medicine_buddy.main import PROFILE, _sync_profile_tools_without_volume


def test_sync_removes_volume_control(tmp_path: Path) -> None:
    instance = str(tmp_path)
    write_profile_tool_override(
        PROFILE,
        ["list_medicines", "volume_control", "dispense_medicine"],
        instance,
    )
    assert "volume_control" in read_profile_tool_names(PROFILE, instance)

    _sync_profile_tools_without_volume(instance)

    tools = read_profile_tool_names(PROFILE, instance)
    assert "volume_control" not in tools
    assert tools == ["list_medicines", "dispense_medicine"]


def test_sync_noop_when_already_clean(tmp_path: Path) -> None:
    instance = str(tmp_path)
    write_profile_tool_override(PROFILE, ["list_medicines", "dispense_medicine"], instance)
    before = read_profile_tool_names(PROFILE, instance)

    _sync_profile_tools_without_volume(instance)

    assert read_profile_tool_names(PROFILE, instance) == before
