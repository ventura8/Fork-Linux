"""Contract: every settings.json key fork-linux writes exists in a real Fork 2.23.2 settings.json.

The key list was read from the settings.json of a real Fork 2.23.2 install
under Wine (QA audit, docs/qa/FUNCTIONALITY-REPORT.md). A key Fork does not
know is silently ignored by Fork (``ExternalMergeTool`` was such a bug), so
any new key must be checked against a real file and added here.
"""

from __future__ import annotations

from fork_linux import fork_settings, fork_tools

# Top-level keys of a real Fork 2.23.2 settings.json that fork-linux may write or read.
FORK_2_23_2_KEYS = frozenset(
    {
        "Guid",
        "Theme",
        "FollowSystemTheme",
        "LayoutScaling",
        "UpdateSubmodulesOnCheckout",
        "DisableHardwareAcceleration",
        "ShellTool",
        "ExternalDiffTool",
        "MergeTool",
        "ExternalDiffTools",
        "ExternalMergeTools",
        "GitInstancePath",
        "ApplicationUpdateType",  # 0 Develop (Fork's default), 1 Stable, 2 Off
        "RepositoryManager",
        "Workspaces",
        "MainWindowLocationState",
    }
)

WRITTEN = (
    fork_settings.GUID,
    fork_settings.THEME,
    fork_settings.FOLLOW_SYSTEM_THEME,
    fork_settings.LAYOUT_SCALING,
    fork_settings.UPDATE_SUBMODULES_ON_CHECKOUT,
    fork_settings.DISABLE_HARDWARE_ACCELERATION,
    fork_settings.SHELL_TOOL,
    fork_settings.EXTERNAL_DIFF_TOOL,
    fork_settings.EXTERNAL_MERGE_TOOL,
    fork_settings.EXTERNAL_DIFF_TOOLS,
    fork_settings.EXTERNAL_MERGE_TOOLS,
    fork_settings.GIT_INSTANCE_PATH,
    fork_settings.APPLICATION_UPDATE_TYPE,
    fork_settings.REPOSITORY_MANAGER,
    fork_settings.SOURCE_DIRECTORIES,
    fork_settings.WORKSPACES,
    fork_settings.MAIN_WINDOW_LOCATION_STATE,
    *fork_settings.ENFORCED_DEFAULTS,
    *fork_tools.TOOL_KEYS,
)


def test_every_key_we_write_is_a_real_fork_key() -> None:
    unknown = sorted({key.split(".")[0] for key in WRITTEN} - FORK_2_23_2_KEYS)
    assert unknown == []


def test_the_merge_tool_key_is_fork_2_23s() -> None:
    assert fork_settings.EXTERNAL_MERGE_TOOL == "MergeTool"
    assert fork_settings.EXTERNAL_DIFF_TOOL == "ExternalDiffTool"
