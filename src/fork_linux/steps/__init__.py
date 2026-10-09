"""The setup steps, in the order :mod:`fork_linux.bootstrap` runs them.

preflight -> consent -> wine_runtime -> winetricks -> prefix_init ->
shell_folders -> registry -> dotnet -> winver -> fonts -> font_replacements ->
display_dpi -> fork_download -> fork_install -> fork_settings -> host_shims ->
host_integration -> git_overlay -> ssh_sync -> icon -> desktop_entry -> finalize
"""

from __future__ import annotations

from ..bootstrap import Step
from .consent import CONSENT
from .display import DISPLAY_DPI
from .dotnet import DOTNET
from .finalize import FINALIZE
from .fonts import FONT_REPLACEMENTS, FONTS
from .fork import FORK_DOWNLOAD, FORK_INSTALL, FORK_SETTINGS
from .integration import DESKTOP_ENTRY, GIT_OVERLAY, HOST_INTEGRATION, HOST_SHIMS, ICON, SSH_SYNC
from .prefix import PREFIX_INIT, REGISTRY, SHELL_FOLDERS, WINVER
from .preflight import PREFLIGHT
from .runtime import WINE_RUNTIME, WINETRICKS

STEPS: tuple[Step, ...] = (
    PREFLIGHT,
    CONSENT,
    WINE_RUNTIME,
    WINETRICKS,
    PREFIX_INIT,
    SHELL_FOLDERS,
    REGISTRY,
    DOTNET,
    WINVER,
    FONTS,
    FONT_REPLACEMENTS,
    DISPLAY_DPI,
    FORK_DOWNLOAD,
    FORK_INSTALL,
    FORK_SETTINGS,
    HOST_SHIMS,
    HOST_INTEGRATION,
    GIT_OVERLAY,
    SSH_SYNC,
    ICON,
    DESKTOP_ENTRY,
    FINALIZE,
)

STEP_IDS: tuple[str, ...] = tuple(step.id for step in STEPS)


def get(step_id: str) -> Step:
    """The step called ``step_id`` (``KeyError`` if there is none)."""
    for step in STEPS:
        if step.id == step_id:
            return step
    raise KeyError(step_id)
