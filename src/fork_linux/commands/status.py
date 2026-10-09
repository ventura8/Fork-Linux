"""``fork-linux status``: is setup complete, which Fork and Wine, where everything lives."""

from __future__ import annotations

import argparse
from typing import Any

from .. import sandbox
from ..cli import AppContext
from ..errors import UsageError
from ..state import FORK_VERSION, SETUP_COMPLETE, WINE_BUILD, WINE_PROVIDER, State
from ..version import get_flavor, get_version

SCHEMA = 1


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add the ``status`` command."""
    parser = subparsers.add_parser(
        "status",
        help="show setup, Fork and Wine status",
        description="Show whether setup is complete, the installed Fork version and the Wine in use.",
    )
    parser.set_defaults(func=run)


def _configured(ctx: AppContext, key: str) -> tuple[str | None, str | None]:
    """``(value, error)`` of ``[wine] key`` from the configuration."""
    try:
        return ctx.config.raw("wine", key), None
    except UsageError as exc:
        return None, exc.message


def collect(ctx: AppContext) -> dict[str, Any]:
    """The status report as a JSON-ready dict (schema :data:`SCHEMA`)."""
    paths = ctx.paths
    state = State.load(paths.state_file)
    provider, config_error = _configured(ctx, "provider")
    build, _error = _configured(ctx, "build")
    recorded = state.get(WINE_PROVIDER)
    return {
        "schema": SCHEMA,
        "fork_linux": get_version(),
        "flavor": get_flavor(),
        "sandbox": sandbox.detect(ctx.env),
        "prefix": str(paths.prefix),
        "prefix_exists": paths.prefix.is_dir(),
        "state_file": str(paths.state_file),
        "setup_complete": state.get(SETUP_COMPLETE) is True,
        "steps_done": sorted(state.data["steps"]),
        "fork_version": state.get(FORK_VERSION),
        "wine_provider": recorded or provider,
        "wine_provider_source": "setup" if recorded else "config",
        "wine_build": state.get(WINE_BUILD) or build or None,
        "config_file": str(paths.config_file),
        "config_exists": paths.config_file.is_file(),
        "config_error": config_error,
    }


def _setup_line(info: dict[str, Any]) -> str:
    """Human summary of the setup progress."""
    if info["setup_complete"]:
        return "complete"
    done = len(info["steps_done"])
    if done:
        return f"incomplete ({done} steps done) - run 'fork-linux setup' to finish"
    return "not set up - run 'fork-linux setup'"


def run(args: argparse.Namespace, ctx: AppContext) -> int:
    """Print the status report (``--json`` for machine output)."""
    info = collect(ctx)
    if ctx.json:
        ctx.print_json(info)
        return 0
    provider = info["wine_provider"] or "unknown"
    if info["wine_build"]:
        provider += f" ({info['wine_build']})"
    if info["wine_provider_source"] == "config":
        provider += " - configured, not set up yet"
    config_note = "exists" if info["config_exists"] else "not created, defaults in use"
    if info["config_error"]:
        config_note = f"error: {info['config_error']}"
    rows = [
        ("Prefix", f"{info['prefix']}" + ("" if info["prefix_exists"] else " (not created yet)")),
        ("Setup", _setup_line(info)),
        ("Fork", info["fork_version"] or "not installed"),
        ("Wine", provider),
        ("Config", f"{info['config_file']} ({config_note})"),
    ]
    if info["sandbox"] != sandbox.NONE:
        rows.append(("Packaging", info["sandbox"]))
    print(f"fork-linux {info['fork_linux']} ({info['flavor']})")
    for label, value in rows:
        print(f"{label + ':':<10} {value}")
    return 0
