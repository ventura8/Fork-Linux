"""Step ``consent``: show Fork's license, what will be downloaded, and ask before anything is.

AGENTS.md hard rules 11 and 13: the not-affiliated disclaimer, the developers'
names, the EULA link and the buy link are shown, and nothing is downloaded
until the user agreed (in a dialog, or with ``--accept-fork-eula``).
"""

from __future__ import annotations

from .. import APP_NAME, credits
from ..bootstrap import Ctx, Step, now
from ..errors import Declined
from . import fork as fork_steps

CONSENT_KEY = "consent.fork_eula"
METHOD_KEY = "consent.method"
TITLE = "Fork license agreement"
# Sizes winetricks downloads from Microsoft (measured: ~124 MB cache in total).
DOTNET_SIZE = "about 120 MB"
COREFONTS_SIZE = "about 5 MB"
DISK = "about 3 GB"
DURATION = "about 5-20 minutes, depending on your network and computer"


def _mb(size: int | None) -> str:
    """``73 MB`` (decimal megabytes), or a note when the size is not pinned."""
    if size is None:
        return "size not pinned (verified after download)"
    return f"{size / 1_000_000:.0f} MB" if size >= 1_000_000 else f"{size / 1_000_000:.1f} MB"


def _wine_line(ctx: Ctx) -> str:
    """The Wine runtime download (only the managed provider downloads one)."""
    provider = ctx.wine_provider_choice()
    if provider != "managed":
        return f"Wine: your {provider} Wine is used, nothing to download"
    build = ctx.manifest.wine_build(ctx.config.get("wine", "build") or ctx.manifest.wine_default.id)
    return f"Wine {build.version} ({build.flavor}) runtime from GitHub (Kron4ek Wine-Builds): {_mb(build.size)}"


def downloads(ctx: Ctx) -> list[str]:
    """One line per download setup will make."""
    winetricks = ctx.manifest.winetricks
    font = ctx.manifest.ui_font
    version, size = fork_steps.describe_request(ctx)
    return [
        _wine_line(ctx),
        f"winetricks {winetricks.version} from GitHub: {_mb(winetricks.size)}",
        f"Microsoft .NET Framework 4.8 from Microsoft, via winetricks: {DOTNET_SIZE}",
        f"Microsoft core fonts (Arial, Verdana, ...) via winetricks: {COREFONTS_SIZE}",
        f"{font.family} {font.version} interface font ({font.license}) from GitHub (Microsoft): {_mb(font.size)}",
        f"Fork {version} installer from {credits.INSTALLER_HOST}: {_mb(size)}",
    ]


def text(ctx: Ctx) -> str:
    """The consent message."""
    listed = "\n".join(f"  - {line}" for line in downloads(ctx))
    return (
        f"{APP_NAME} will download and install the official Fork for Windows git client, "
        f"made by {credits.developers_text()}, and run it under Wine.\n\n"
        f"{credits.disclaimer()}\n\n"
        "Fork is commercial software. By continuing you accept Fork's license agreement:\n"
        f"  {credits.LINKS['license']}\n"
        f"Fork needs a license for continued use ({credits.PRICE}, {credits.LICENSE_SCOPE}):\n"
        f"  {credits.LINKS['buy']}\n\n"
        f"Downloads:\n{listed}\n\n"
        f"Disk space: {DISK}. Time: {DURATION}.\n\n"
        "Accept Fork's license agreement and continue?"
    )


def run(ctx: Ctx) -> None:
    """Ask (unless accepted before, or ``--accept-fork-eula``); :class:`Declined` when the answer is no."""
    if verify(ctx):
        return
    if ctx.accept_eula:
        method = "flag"
    elif ctx.ui.confirm(TITLE, text(ctx), default=False):
        method = "prompt"
    elif ctx.ui.interactive:
        raise Declined(
            "Fork's license agreement was declined; nothing was downloaded",
            hint="run 'fork-linux setup' again when you want to continue",
        )
    else:
        raise Declined(
            "Fork's license agreement has not been accepted and nobody could be asked",
            hint=f"read {credits.LINKS['license']}, then run 'fork-linux setup --accept-fork-eula'",
        )
    ctx.state.set(CONSENT_KEY, now())
    ctx.state.set(METHOD_KEY, method)


def verify(ctx: Ctx) -> bool:
    """True once the agreement was accepted."""
    value = ctx.state.get(CONSENT_KEY)
    return isinstance(value, str) and bool(value)


CONSENT = Step(
    id="consent",
    title="Fork license agreement",
    rev=1,
    weight=1,
    run=run,
    verify=verify,
    requires_fork_closed=False,
)

STEPS = (CONSENT,)
