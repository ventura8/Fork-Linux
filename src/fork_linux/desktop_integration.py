"""Per-user desktop integration: menu entry, icon, "Open in Fork" for file managers, CLI aliases.

Everything is written under the user's XDG directories (never system paths,
never a Wine prefix) and recorded in ``integrations.json`` with its sha256,
so :func:`remove` deletes only files we wrote and the user has not changed
since. The menu entry is skipped when a package already installed one in
``$XDG_DATA_DIRS``. The menu icon is Fork's own, extracted at runtime from
the user's ``Fork.exe`` (never shipped). Thunar keeps its custom actions in
one shared ``uca.xml``: only our ``<action>`` element is added or removed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from . import APP_ID, APP_NAME, credits, fsutil, icon_extract, resources, sandbox
from .errors import ForkLinuxError, UsageError
from .paths import Paths
from .procrun import Runner

log = logging.getLogger(__name__)

FILE_MANAGERS = ("nautilus", "nemo", "caja", "dolphin", "thunar", "fma")
LABEL = "Open in Fork"
TOOLTIP = f"Open the repository in {APP_NAME}"
UNIQUE_ID = "fork-linux-open"
FLATPAK_COMMAND = ("flatpak", "run", APP_ID)
APPIMAGE_ENV = "FORK_LINUX_APPIMAGE"
LAUNCHER = "fork-linux"
CLI_ALIASES = ("fork", "fork-linux")
TEMPLATE_DIR = "templates"
REGISTRY_SCHEMA = 1
THUNAR_BACKUP = "uca.xml.fork-linux-backup"
DEFAULT_DATA_DIRS = "/usr/local/share:/usr/share"
TOOL_TIMEOUT = 60.0

# Kinds of recorded integration entries.
MENU = "menu"
ICON = "icon"
NAUTILUS = "nautilus"
NAUTILUS_SCRIPT = "nautilus-script"
NEMO = "nemo"
FMA = "fma"
DOLPHIN = "dolphin"
THUNAR = "thunar"
CLI_ALIAS = "cli-alias"
FILE_KINDS = (MENU, ICON, NAUTILUS, NAUTILUS_SCRIPT, NEMO, FMA, DOLPHIN)

# File manager name -> integration kinds it needs.
_MANAGER_KINDS = {
    "nautilus": (NAUTILUS, NAUTILUS_SCRIPT),
    "nemo": (NEMO,),
    "caja": (FMA,),
    "fma": (FMA,),
    "dolphin": (DOLPHIN,),
    "thunar": (THUNAR,),
}
_PLACEHOLDER = re.compile(r"@([A-Z][A-Z0-9_]*)@")
# Characters that force quoting in a Desktop Entry Exec argument.
_EXEC_RESERVED = frozenset(" \t\n\"'\\><~|&;$*?#()`")
_EXEC_ESCAPED = frozenset('"`$\\')


@dataclass(frozen=True)
class _Dirs:
    """The user's XDG directories, resolved from an environment."""

    data: Path
    config: Path
    bin: Path
    data_dirs: tuple[Path, ...]

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> _Dirs:
        home = Path(env.get("HOME") or Path.home())

        def xdg(name: str, default: Path) -> Path:
            value = env.get(name, "")
            return Path(value) if os.path.isabs(value) else default

        raw_dirs = env.get("XDG_DATA_DIRS") or DEFAULT_DATA_DIRS
        return cls(
            data=xdg("XDG_DATA_HOME", home / ".local" / "share"),
            config=xdg("XDG_CONFIG_HOME", home / ".config"),
            bin=xdg("XDG_BIN_HOME", home / ".local" / "bin"),
            data_dirs=tuple(Path(item) for item in raw_dirs.split(":") if os.path.isabs(item)),
        )

    @property
    def applications(self) -> Path:
        return self.data / "applications"

    @property
    def menu_file(self) -> Path:
        return self.applications / f"{APP_ID}.desktop"

    @property
    def hicolor(self) -> Path:
        return self.data / "icons" / "hicolor"

    @property
    def thunar_uca(self) -> Path:
        return self.config / "Thunar" / "uca.xml"

    def kind_path(self, kind: str) -> Path:
        """Where the file of a file-manager ``kind`` goes."""
        return {
            NAUTILUS: self.data / "nautilus-python" / "extensions" / "fork_linux_nautilus.py",
            NAUTILUS_SCRIPT: self.data / "nautilus" / "scripts" / LABEL,
            NEMO: self.data / "nemo" / "actions" / "fork-linux-open.nemo_action",
            FMA: self.data / "file-manager" / "actions" / "fork-linux-open.desktop",
            DOLPHIN: self.data / "kio" / "servicemenus" / "fork-linux-open.desktop",
        }[kind]


def launcher_command(env: Mapping[str, str] | None = None) -> list[str]:
    """The command that starts fork-linux from a menu or file manager.

    The AppImage itself (``$FORK_LINUX_APPIMAGE``), ``flatpak run <app-id>``
    inside Flatpak, else ``fork-linux`` from ``$PATH`` or this installation's
    ``bin/fork-linux`` (in a source checkout without a rendered launcher:
    Python running ``bin/fork-linux.in``).
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    appimage = environ.get(APPIMAGE_ENV, "")
    if appimage:
        return [appimage]
    if sandbox.detect(environ) == sandbox.FLATPAK:
        return list(FLATPAK_COMMAND)
    found = shutil.which(LAUNCHER, path=environ.get("PATH"))
    if found:
        return [found]
    launcher = resources.install_root() / "bin" / LAUNCHER
    template = launcher.with_name(LAUNCHER + ".in")
    if resources.is_source_tree() and not launcher.exists() and template.is_file():
        # A checkout has only the unrendered template; Python runs it as it is (it finds src/).
        return [sys.executable, "-I", str(template)]
    return [str(launcher)]


def _exec_arg(arg: str) -> str:
    """One Exec argument: quoted when needed, then ``%`` and backslashes escaped for the key file."""
    if any(ord(ch) < 0x20 and ch != "\t" for ch in arg):
        raise UsageError(f"cannot put {arg!r} in a desktop entry: it contains a control character")
    if arg and not any(ch in _EXEC_RESERVED for ch in arg):
        quoted = arg
    else:
        quoted = '"' + "".join("\\" + ch if ch in _EXEC_ESCAPED else ch for ch in arg) + '"'
    return quoted.replace("%", "%%").replace("\\", "\\\\")


def desktop_exec(cmd: Iterable[str]) -> str:
    """``cmd`` as a Desktop Entry ``Exec`` value (quoting and escaping per the specification)."""
    args = list(cmd)
    if not args:
        raise UsageError("empty command")
    return " ".join(_exec_arg(arg) for arg in args)


def _desktop_string(value: str) -> str:
    """Escape a Desktop Entry ``string`` / ``localestring`` value."""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")


def render_template(name: str, mapping: Mapping[str, str]) -> str:
    """Render ``data/templates/<name>[.in]``, replacing every ``@KEY@`` from ``mapping``.

    A placeholder missing from ``mapping`` is an error (:class:`ForkLinuxError`).
    """
    if not name or "/" in name or name.startswith("."):
        raise UsageError(f"invalid template name {name!r}")
    file_name = name if name.endswith(".in") else f"{name}.in"
    path = resources.data_path(TEMPLATE_DIR, file_name)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ForkLinuxError(f"cannot read the template {path}: {exc.strerror or exc}") from exc
    missing = sorted({key for key in _PLACEHOLDER.findall(text) if key not in mapping})
    if missing:
        raise ForkLinuxError(
            f"template {file_name} uses unknown placeholder(s): {', '.join('@' + key + '@' for key in missing)}"
        )
    return _PLACEHOLDER.sub(lambda match: str(mapping[match.group(1)]), text)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Registry:
    """``integrations.json``: what we installed, keyed by path."""

    def __init__(self, path: Path, entries: dict[str, dict[str, Any]]) -> None:
        self.path = path
        self.entries = entries

    @classmethod
    def load(cls, path: Path) -> _Registry:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(path, {})
        except (OSError, ValueError) as exc:
            log.warning("ignoring the unreadable integration registry %s: %s", path, exc)
            return cls(path, {})
        raw = data.get("entries") if isinstance(data, dict) else None
        entries: dict[str, dict[str, Any]] = {}
        for entry in raw if isinstance(raw, list) else []:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str) and isinstance(entry.get("kind"), str):
                entries[entry["path"]] = entry
        return cls(path, entries)

    def get(self, path: Path) -> dict[str, Any] | None:
        return self.entries.get(str(path))

    def record(self, path: Path, kind: str, **fields: Any) -> None:
        self.entries[str(path)] = {"path": str(path), "kind": kind, **fields}

    def forget(self, path: Path) -> None:
        self.entries.pop(str(path), None)

    def save(self) -> None:
        if not self.entries:
            if self.path.exists():
                self.path.unlink()
            return
        data = {"schema": REGISTRY_SCHEMA, "entries": sorted(self.entries.values(), key=lambda e: e["path"])}
        fsutil.atomic_write(self.path, json.dumps(data, indent=2) + "\n", mode=0o600)


def _file_state(path: Path, recorded_sha: str | None) -> str:
    """``missing``, ``ok`` (content matches ``recorded_sha``) or ``modified``."""
    if not os.path.lexists(path):
        return "missing"
    if path.is_symlink() or not path.is_file():
        return "modified"
    return "ok" if fsutil.sha256_file(path) == recorded_sha else "modified"


def _write_owned(path: Path, content: bytes, mode: int, kind: str, registry: _Registry) -> bool:
    """Write ``content`` unless a file we did not write (or the user changed) is in the way."""
    digest = _sha256(content)
    if os.path.lexists(path):
        current = None if path.is_symlink() or not path.is_file() else fsutil.sha256_file(path)
        entry = registry.get(path)
        recorded = entry.get("sha256") if entry is not None else None
        if current != digest and (current is None or current != recorded):
            log.warning("leaving %s alone: it was not written by fork-linux or has been changed", path)
            return False
        if current == digest:
            os.chmod(path, mode)
            registry.record(path, kind, sha256=digest)
            return True
    path.parent.mkdir(parents=True, exist_ok=True)
    fsutil.atomic_write(path, content, mode=mode)
    registry.record(path, kind, sha256=digest)
    return True


def _system_desktop(dirs: _Dirs) -> Path | None:
    """A package-installed copy of our desktop entry in ``$XDG_DATA_DIRS``, if any."""
    own = os.path.realpath(dirs.menu_file)
    for base in dirs.data_dirs:
        candidate = base / "applications" / f"{APP_ID}.desktop"
        if candidate.is_file() and os.path.realpath(candidate) != own:
            return candidate
    return None


def _render_menu(cmd: list[str]) -> str:
    """Our desktop entry; ``TryExec`` only when the command is an absolute path."""
    text = render_template(
        f"{APP_ID}.desktop",
        {
            "APP_ID": APP_ID,
            "APP_NAME": _desktop_string(APP_NAME),
            "COMMENT": _desktop_string(credits.render_desktop_comment()),
            "EXEC": desktop_exec(cmd),
            "TRYEXEC": _desktop_string(cmd[0]),
        },
    )
    if not os.path.isabs(cmd[0]):
        text = "".join(line for line in text.splitlines(keepends=True) if not line.startswith("TryExec="))
    return text


def _render_kind(kind: str, cmd: list[str]) -> tuple[str, int]:
    """``(content, mode)`` of a file-manager integration file."""
    common = {
        "APP_ID": APP_ID,
        "APP_NAME": APP_NAME,
        "LABEL": _desktop_string(LABEL),
        "TOOLTIP": _desktop_string(TOOLTIP),
        "EXEC": desktop_exec(cmd),
    }
    if kind == NAUTILUS:
        literals = {
            "COMMAND_LITERAL": repr(list(cmd)),
            "LABEL_LITERAL": repr(LABEL),
            "TOOLTIP_LITERAL": repr(TOOLTIP),
            "ICON_LITERAL": repr(APP_ID),
        }
        return render_template("fork_linux_nautilus.py", {**common, **literals}), 0o644
    if kind == NAUTILUS_SCRIPT:
        return render_template("nautilus-script", {**common, "EXEC_SH": shlex.join(cmd)}), 0o755
    if kind == NEMO:
        return render_template("nemo_action", common), 0o644
    if kind == FMA:
        return render_template("fma-action.desktop", common), 0o644
    return render_template("dolphin-servicemenu.desktop", common), 0o755


def _thunar_action(cmd: list[str]) -> ET.Element:
    """Our Thunar ``<action>`` element."""
    values = {
        "APP_ID": APP_ID,
        "LABEL": LABEL,
        "TOOLTIP": TOOLTIP,
        "UNIQUE_ID": UNIQUE_ID,
        "COMMAND": shlex.join(cmd),
    }
    return ET.fromstring(render_template("thunar-uca-action.xml", {k: xml_escape(v) for k, v in values.items()}))


def _read_uca(path: Path) -> ET.ElementTree | None:
    """Parse ``uca.xml`` keeping comments; ``None`` (with a warning) when it is not usable."""
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    try:
        tree = ET.parse(path, parser=parser)
    except (ET.ParseError, OSError) as exc:
        log.warning("leaving %s alone: cannot parse it (%s)", path, exc)
        return None
    if tree.getroot().tag != "actions":
        log.warning("leaving %s alone: it is not a Thunar custom actions file", path)
        return None
    return tree


def _ours(element: ET.Element) -> bool:
    """True for our ``<action>`` element."""
    return element.tag == "action" and (element.findtext("unique-id") or "").strip() == UNIQUE_ID


def _write_uca(path: Path, root: ET.Element) -> None:
    """Back up ``uca.xml``, then write ``root`` to it atomically (keeping its mode)."""
    mode = 0o644
    if path.exists():
        mode = path.stat().st_mode & 0o777
        fsutil.atomic_write(path.with_name(THUNAR_BACKUP), path.read_bytes(), mode=mode)
    ET.indent(root, space="\t")
    data = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fsutil.atomic_write(path, data, mode=mode)


def _install_thunar(dirs: _Dirs, cmd: list[str], registry: _Registry) -> bool:
    """Merge our action into ``uca.xml`` (created when missing); True on success."""
    path = dirs.thunar_uca
    entry = registry.get(path)
    created = bool(entry and entry.get("created"))
    if path.exists():
        tree = _read_uca(path)
        if tree is None:
            return False
        root = tree.getroot()
    else:
        root = ET.Element("actions")
        created = True
    for element in [child for child in root if _ours(child)]:
        root.remove(element)
    root.append(_thunar_action(cmd))
    _write_uca(path, root)
    registry.record(path, THUNAR, unique_id=UNIQUE_ID, created=created)
    return True


def _remove_thunar(path: Path, entry: Mapping[str, Any]) -> bool:
    """Remove our action from ``uca.xml`` (and the file if we created it and it is now empty)."""
    if not path.exists():
        return False
    tree = _read_uca(path)
    if tree is None:
        return False
    root = tree.getroot()
    ours = [child for child in root if _ours(child)]
    if not ours:
        return False
    for element in ours:
        root.remove(element)
    if entry.get("created") and not any(child.tag == "action" for child in root):
        path.unlink()
        backup = path.with_name(THUNAR_BACKUP)
        if backup.exists():
            backup.unlink()
        return True
    _write_uca(path, root)
    return True


def _alias_target(cmd: list[str], dirs: _Dirs) -> str | None:
    """What the ``fork`` / ``fork-linux`` links point to, or ``None`` when no file can be linked."""
    if len(cmd) != 1 or not os.path.isabs(cmd[0]):
        return None
    target = cmd[0]
    if Path(target).parent == dirs.bin:
        # The launcher was found through one of our own links: link to the real file.
        target = os.path.realpath(target)
    return target


def _link_is_ours(link: Path, target: str, recorded: str | None = None) -> bool:
    """True when the symbolic link ``link`` leads to fork-linux (``target``, what we recorded, or our install)."""
    if not link.is_symlink():
        return False
    if recorded is not None and os.readlink(link) == recorded:
        return True
    real = os.path.realpath(link)
    if real == os.path.realpath(target):
        return True
    root = os.path.realpath(resources.install_root())
    return real.startswith(root + os.sep)


def _install_aliases(dirs: _Dirs, cmd: list[str], registry: _Registry) -> list[Path]:
    """``fork`` and ``fork-linux`` links in ``$XDG_BIN_HOME`` (default ``~/.local/bin``)."""
    target = _alias_target(cmd, dirs)
    if target is None:
        log.warning("cannot create the fork / fork-linux commands for %s; use it directly", shlex.join(cmd))
        return []
    installed = []
    for name in CLI_ALIASES:
        link = dirs.bin / name
        entry = registry.get(link)
        recorded = entry.get("target") if entry is not None else None
        if os.path.lexists(link) and not _link_is_ours(link, target, recorded):
            log.warning("leaving %s alone: it is not a link to fork-linux", link)
            continue
        if not (link.is_symlink() and os.readlink(link) == target):
            # The user's own bin directory: create it if needed, never change its mode.
            dirs.bin.mkdir(parents=True, exist_ok=True)
            temp = link.with_name(f".{name}.fork-linux-tmp")
            if os.path.lexists(temp):
                temp.unlink()
            os.symlink(target, temp)
            os.replace(temp, link)
        registry.record(link, CLI_ALIAS, target=target)
        installed.append(link)
    return installed


def _kinds(file_managers: str | Iterable[str] | None) -> list[str]:
    """Integration kinds for ``all``, ``none``, a comma-separated list or an iterable of names."""
    if file_managers is None:
        return []
    if isinstance(file_managers, str):
        names = [item.strip().lower() for item in file_managers.split(",") if item.strip()]
    else:
        names = [str(item).strip().lower() for item in file_managers]
    if names == ["none"]:
        return []
    if "all" in names:
        names = list(FILE_MANAGERS)
    unknown = [name for name in names if name not in _MANAGER_KINDS]
    if unknown:
        raise UsageError(
            f"unknown file manager(s): {', '.join(unknown)}",
            hint=f"choose from: all, none, {', '.join(FILE_MANAGERS)}",
        )
    kinds: list[str] = []
    for name in names:
        kinds.extend(kind for kind in _MANAGER_KINDS[name] if kind not in kinds)
    return kinds


def _refresh(dirs: _Dirs, env: Mapping[str, str], runner: Runner, *, menu: bool, icons: bool) -> None:
    """Refresh desktop caches; every failure is ignored."""
    tool_env = sandbox.clean_env(env)
    path = tool_env.get("PATH")
    commands = []
    if menu and dirs.applications.is_dir():
        commands.append(["update-desktop-database", "-q", str(dirs.applications)])
    if icons and dirs.hicolor.is_dir():
        os.utime(dirs.hicolor)
        # Only refresh a cache that already exists: a new user cache would go stale.
        if (dirs.hicolor / "icon-theme.cache").exists():
            commands.append(["gtk-update-icon-cache", "-q", "-t", "-f", str(dirs.hicolor)])
    for argv in commands:
        if runner.which(argv[0], path) is None:
            continue
        try:
            result = runner.run(argv, env=tool_env, timeout=TOOL_TIMEOUT)
        except ForkLinuxError as exc:
            log.debug("%s: %s", argv[0], exc)
            continue
        if not result.ok:
            log.debug("%s exited with %d", argv[0], result.returncode)


def _install_menu(
    dirs: _Dirs, cmd: list[str], registry: _Registry, system_desktop_present: bool | None, installed: list[Path]
) -> bool:
    """Write the personal menu entry unless a packaged one exists; True if the menu changed."""
    system = _system_desktop(dirs) if system_desktop_present is None else None
    if system is not None or system_desktop_present:
        log.info("a packaged menu entry exists (%s); not adding a personal one", system)
        return _remove_owned(dirs.menu_file, registry)
    if _write_owned(dirs.menu_file, _render_menu(cmd).encode("utf-8"), 0o644, MENU, registry):
        installed.append(dirs.menu_file)
        return True
    return False


def _install_icons(fork_exe: Path, dirs: _Dirs, registry: _Registry) -> list[Path]:
    """Extract Fork's icon into the hicolor theme and record the PNGs written."""
    try:
        pngs = icon_extract.extract_icons(fork_exe, dirs.hicolor, APP_ID)
    except ForkLinuxError as exc:
        log.warning("cannot extract Fork's icon from %s: %s", fork_exe, exc)
        pngs = []
    for png in pngs:
        registry.record(png, ICON, sha256=fsutil.sha256_file(png))
    return pngs


def _install_kind(kind: str, dirs: _Dirs, cmd: list[str], registry: _Registry) -> list[Path]:
    """Install one file-manager integration; the path written, if any."""
    if kind == THUNAR:
        return [dirs.thunar_uca] if _install_thunar(dirs, cmd, registry) else []
    content, mode = _render_kind(kind, cmd)
    path = dirs.kind_path(kind)
    return [path] if _write_owned(path, content.encode("utf-8"), mode, kind, registry) else []


def install(
    paths: Paths,
    env: Mapping[str, str] | None = None,
    *,
    exec_cmd: list[str] | None = None,
    fork_exe: Path | None = None,
    menu: bool = True,
    icons: bool = True,
    file_managers: str | Iterable[str] | None = "all",
    cli_alias: bool = False,
    system_desktop_present: bool | None = None,
    runner: Runner | None = None,
) -> list[Path]:
    """Install the per-user integration; return every path that is now ours.

    ``file_managers`` is ``all``, ``none``, or names from
    :data:`FILE_MANAGERS` (comma separated or a list). Files that exist but
    were not written by us (or were changed by the user) are left alone with
    a warning.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    dirs = _Dirs.from_env(environ)
    cmd = list(exec_cmd) if exec_cmd else launcher_command(environ)
    kinds = _kinds(file_managers)
    registry = _Registry.load(paths.integrations_file)
    installed: list[Path] = []
    menu_changed = menu and _install_menu(dirs, cmd, registry, system_desktop_present, installed)
    icons_changed = False
    if icons and fork_exe is not None and Path(fork_exe).is_file():
        pngs = _install_icons(Path(fork_exe), dirs, registry)
        installed.extend(pngs)
        icons_changed = bool(pngs)
    for kind in kinds:
        installed.extend(_install_kind(kind, dirs, cmd, registry))
    if cli_alias:
        installed.extend(_install_aliases(dirs, cmd, registry))
    registry.save()
    _refresh(dirs, environ, Runner() if runner is None else runner, menu=menu_changed, icons=icons_changed)
    return installed


def _inside_user_dirs(path: Path, dirs: _Dirs) -> bool:
    """True for a path under the user directories we install into (data, config, bin)."""
    absolute = os.path.abspath(path)
    return any(absolute.startswith(os.path.abspath(root) + os.sep) for root in (dirs.data, dirs.config, dirs.bin))


def _remove_owned(path: Path, registry: _Registry) -> bool:
    """Delete a recorded file that is still exactly as we wrote it; True if deleted."""
    entry = registry.get(path)
    if entry is None:
        return False
    registry.forget(path)
    state = _file_state(path, entry.get("sha256"))
    if state == "ok":
        path.unlink()
        return True
    if state == "modified":
        log.warning("leaving %s in place: it was changed after fork-linux installed it", path)
    return False


def remove(paths: Paths, env: Mapping[str, str] | None = None, *, runner: Runner | None = None) -> list[Path]:
    """Remove everything :func:`install` recorded that is still ours; return the paths removed or edited."""
    environ: Mapping[str, str] = os.environ if env is None else env
    dirs = _Dirs.from_env(environ)
    registry = _Registry.load(paths.integrations_file)
    removed: list[Path] = []
    kinds_removed: set[str] = set()
    for key, entry in sorted(registry.entries.items()):
        path = Path(key)
        kind = entry["kind"]
        if not _inside_user_dirs(path, dirs):
            log.warning("not removing %s: it is outside the directories fork-linux installs into", path)
            done = False
        elif kind == THUNAR:
            registry.forget(path)
            done = _remove_thunar(path, entry)
        elif kind == CLI_ALIAS:
            registry.forget(path)
            target = str(entry.get("target", ""))
            done = bool(target) and _link_is_ours(path, target, target)
            if done:
                path.unlink()
        else:
            done = _remove_owned(path, registry)
        if done:
            removed.append(path)
            kinds_removed.add(kind)
    registry.entries.clear()
    registry.save()
    _refresh(
        dirs,
        environ,
        Runner() if runner is None else runner,
        menu=MENU in kinds_removed,
        icons=ICON in kinds_removed,
    )
    return removed


def _entry_state(path: Path, entry: Mapping[str, Any]) -> str:
    """``ok``, ``missing``, ``modified`` or ``foreign`` for one recorded entry."""
    kind = entry["kind"]
    if kind == THUNAR:
        tree = _read_uca(path) if path.exists() else None
        if tree is None:
            return "missing"
        return "ok" if any(_ours(child) for child in tree.getroot()) else "missing"
    if kind == CLI_ALIAS:
        if not os.path.lexists(path):
            return "missing"
        return "ok" if path.is_symlink() and os.readlink(path) == entry.get("target") else "foreign"
    return _file_state(path, entry.get("sha256"))


def status(paths: Paths, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """What is installed and whether each recorded file is still intact."""
    environ: Mapping[str, str] = os.environ if env is None else env
    dirs = _Dirs.from_env(environ)
    registry = _Registry.load(paths.integrations_file)
    system = _system_desktop(dirs)
    entries = [
        {"path": key, "kind": entry["kind"], "state": _entry_state(Path(key), entry)}
        for key, entry in sorted(registry.entries.items())
    ]
    return {
        "registry": str(paths.integrations_file),
        "installed": bool(entries),
        "launcher": launcher_command(environ),
        "system_desktop": None if system is None else str(system),
        "menu_file": str(dirs.menu_file),
        "entries": entries,
        "ok": all(entry["state"] == "ok" for entry in entries),
    }
