"""Helpers for the bootstrap / setup-step tests: a Ctx over the ``xdg`` fixture and a fake Wine.

:class:`FakeWine` answers a :class:`~fork_linux.procrun.RecordingRunner` like
Wine, wineserver and winetricks would, with the effects the steps verify:
``wineboot`` writes 64-bit hives and the drive links, ``regedit /S`` merges the
batch into ``user.reg`` / ``system.reg``, ``winetricks dotnet48`` records the
.NET release and ``corefonts`` drops ``arial.ttf``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fixtures.fork_tree import install_fork
from fork_linux import manifest as manifest_mod
from fork_linux.bootstrap import Ctx
from fork_linux.config import Config
from fork_linux.fork_layout import ForkLayout
from fork_linux.paths import Paths
from fork_linux.procrun import Completed, RecordingRunner
from fork_linux.state import State
from fork_linux.ui import UI, Progress
from fork_linux.winecmd import WineInfo

USER = "tester"
HIVE_HEADER = "WINE REGISTRY Version 2\n;; All keys relative to {root}\n\n#arch=win64\n"
HIVE_ROOTS = {"system.reg": "\\\\Machine", "user.reg": "\\\\User\\\\S-1-5-21-0-0-0-1000"}
REG_HIVES = {"HKEY_CURRENT_USER\\": "user.reg", "HKEY_LOCAL_MACHINE\\": "system.reg"}
NDP_KEY = "Software\\Microsoft\\NET Framework Setup\\NDP\\v4\\Full"


class RecordingProgress(Progress):
    """A progress display that remembers every update."""

    def __init__(self, title: str, log: list[tuple[str, Any]]) -> None:
        super().__init__(title)
        self.events = log

    def start(self) -> None:
        self.events.append(("start", self.title))

    def update(self, fraction: float, text: str = "") -> None:
        super().update(fraction, text)
        self.events.append(("update", (round(fraction, 4), text)))

    def finish(self, *, failed: bool) -> None:
        self.events.append(("finish", failed))


class FakeUI(UI):
    """Answers confirmations with ``answer`` and records everything."""

    def __init__(self, *, answer: bool = True, interactive: bool = True) -> None:
        self.answer = answer
        self.interactive = interactive
        self.events: list[tuple[str, Any]] = []

    def info(self, msg: str) -> None:
        self.events.append(("info", msg))

    def notify(self, msg: str) -> None:
        self.events.append(("notify", msg))

    def warn(self, msg: str) -> None:
        self.events.append(("warn", msg))

    def confirm(self, title: str, text: str, *, default: bool = False) -> bool:
        self.events.append(("confirm", (title, text)))
        return self.answer

    def progress(self, title: str) -> Progress:
        return RecordingProgress(title, self.events)

    def kinds(self, kind: str) -> list[Any]:
        return [value for name, value in self.events if name == kind]


def wine_info(root: Path) -> WineInfo:
    """A WineInfo for a fake Wine root (nothing is started)."""
    return WineInfo(
        provider="custom",
        build_id=None,
        root=root,
        wine=root / "bin" / "wine",
        wineserver=root / "bin" / "wineserver",
        version="11.0",
        staging=True,
        wow64=True,
    )


def make_ctx(
    runner: RecordingRunner | None = None,
    *,
    ui: UI | None = None,
    env: dict[str, str] | None = None,
    with_wine: bool = True,
    **flags: Any,
) -> Ctx:
    """A Ctx for the current (``xdg`` fixture) environment, user :data:`USER`."""
    environ = dict(os.environ if env is None else env)
    environ["USER"] = USER
    paths = Paths.from_env(environ)
    ctx = Ctx(
        paths=paths,
        config=Config.load(paths, environ),
        manifest=manifest_mod.load(),
        runner=RecordingRunner() if runner is None else runner,
        env=environ,
        ui=FakeUI() if ui is None else ui,
        state=State.load(paths.state_file),
        user=USER,
        **flags,
    )
    if with_wine:
        ctx.set_wine(wine_info(paths.data_dir / "fake-wine"))
    return ctx


# -- registry effects ----------------------------------------------------------------------------


def _escape_key(path: str) -> str:
    return path.replace("\\", "\\\\")


def add_values(prefix: Path, hive: str, key: str, lines: list[str]) -> None:
    """Append ``[key]`` with raw value ``lines`` to ``prefix/hive`` (created when missing)."""
    path = prefix / hive
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(HIVE_HEADER.format(root=HIVE_ROOTS[hive]), encoding="utf-8")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"\n[{_escape_key(key)}] 1700000000\n")
        handle.writelines(line + "\n" for line in lines)


def apply_reg(prefix: Path, text: str) -> None:
    """Merge a REGEDIT5 batch (as text) into the prefix's hives the way regedit would."""
    hive = key = None
    lines: list[str] = []

    def flush() -> None:
        if hive is not None and key is not None:
            add_values(prefix, hive, key, lines)

    for raw in text.lstrip("﻿").splitlines():
        line = raw.rstrip("\r")
        if line.startswith("["):
            flush()
            inner = line[1:-1]
            hive = key = None
            lines = []
            for root, name in REG_HIVES.items():
                if inner.startswith(root):
                    hive, key = name, inner[len(root):]
        elif line.startswith(('"', "@")):
            lines.append(line)
    flush()


def write_release(prefix: Path, release: int) -> None:
    """Record a .NET Framework 4.x release in ``system.reg``."""
    add_values(prefix, "system.reg", NDP_KEY, [f'"Release"=dword:{release:08x}'])


class FakeWine:
    """``RecordingRunner`` responses for wine / wineserver / winetricks with their effects."""

    def __init__(
        self,
        prefix: Path,
        *,
        release: int = 528049,
        fail: tuple[str, ...] = (),
        layout: ForkLayout | None = None,
    ) -> None:
        self.prefix = prefix
        self.release = release
        self.fail = set(fail)
        # Verbs that fail on their next call only (a transient winetricks failure).
        self.fail_once: set[str] = set()
        self.layout = layout
        self.verbs: list[str] = []

    def responses(self) -> dict[str, Any]:
        return {"wine": self.wine, "wineserver": self.wineserver, "winetricks": self.winetricks}

    def runner(self, extra: dict[str, Any] | None = None) -> RecordingRunner:
        table = self.responses()
        table.update(extra or {})
        return RecordingRunner(table)

    def wine(self, argv: list[str]) -> Completed | int | str:
        args = argv[1:]
        if args == ["--version"]:
            return "wine-11.0 (Staging)\n"
        if args[-1:] == ["--silent"] and self.layout is not None:
            version = args[0].rsplit("\\", 1)[-1][len("Fork-"):-len(".exe")]
            install_fork(self.layout, version)
            return 0
        if args[:1] == ["wineboot"]:
            if "wineboot" in self.fail:
                return 1
            for name in ("system.reg", "user.reg"):
                if not (self.prefix / name).exists():
                    add_values(self.prefix, name, "Software\\Wine", [])
            dosdevices = self.prefix / "dosdevices"
            dosdevices.mkdir(parents=True, exist_ok=True)
            (self.prefix / "drive_c" / "users" / USER).mkdir(parents=True, exist_ok=True)
            for drive, target in (("c:", "../drive_c"), ("z:", "/")):
                if not os.path.lexists(dosdevices / drive):
                    (dosdevices / drive).symlink_to(target)
            return 0
        if args[:1] == ["regedit"]:
            if "regedit" in self.fail:
                return Completed(argv, 1, "", "regedit failed")
            reg = self.prefix / "drive_c" / args[2][3:].replace("\\", "/")
            apply_reg(self.prefix, reg.read_bytes().decode("utf-16"))
            return 0
        return 0

    def wineserver(self, argv: list[str]) -> int:
        return 1 if "wineserver" in self.fail else 0

    def winetricks(self, argv: list[str]) -> int:
        verbs = [arg for arg in argv[1:] if not arg.startswith("-")]
        self.verbs.extend(verbs)
        failed = [verb for verb in verbs if verb in self.fail or verb in self.fail_once]
        self.fail_once.difference_update(verbs)
        if failed:
            return 1
        if self.prefix.is_dir():
            # The real winetricks records each finished verb in winetricks.log.
            with open(self.prefix / "winetricks.log", "a", encoding="utf-8") as handle:
                handle.writelines(verb + "\n" for verb in verbs)
        for verb in verbs:
            if verb.startswith("dotnet"):
                write_release(self.prefix, self.release)
            elif verb == "corefonts":
                fonts = self.prefix / "drive_c" / "windows" / "Fonts"
                fonts.mkdir(parents=True, exist_ok=True)
                (fonts / "arial.ttf").write_bytes(b"font")
            elif verb == "win10":
                add_values(self.prefix, "user.reg", "Software\\Wine", ['"Version"="win10"'])
        return 0
