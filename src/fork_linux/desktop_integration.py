"""Per-user desktop integration: menu entry, icon, "Open in Fork" for file managers, CLI aliases.

Everything is written under the user's XDG directories (never system paths,
never a Wine prefix) and recorded in ``integrations.json`` with its sha256,
so :func:`remove` deletes only files we wrote and the user has not changed
since. The menu entry is skipped when a package already installed one in
``$XDG_DATA_DIRS``. The menu icon is Fork's own, extracted at runtime from
the user's ``Fork.exe`` (never shipped). Thunar keeps its custom actions in
one shared ``uca.xml``: only our ``<action>`` element is added or removed.
Thunar reads only the first ``Thunar/uca.xml`` of ``$XDG_CONFIG_HOME`` and
``$XDG_CONFIG_DIRS``, so a missing user file is first seeded from the
desktop's own file (Xubuntu's "Open Terminal Here", ...), never created empty.
``uca.xml`` is written only when its content changes; a running Thunar reads it only
when it starts, so a change then comes with a warning to restart Thunar. Nautilus
likewise loads its extensions only when it starts: installing a changed extension or
removing it comes with a warning to restart a running Nautilus. Cinnamon's and MATE's
menus watch only the application folders that existed when the session started: a menu
entry that had to create ``$XDG_DATA_HOME/applications`` there comes with a warning
that it shows up after the next login.
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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from . import APP_ID, APP_NAME, credits, fsutil, icon_extract, procs, resources, sandbox
from .display import desktop as current_desktop
from .errors import ForkLinuxError, UsageError
from .paths import Paths
from .procrun import Runner

log = logging.getLogger(__name__)

FILE_MANAGERS = ("nautilus", "nemo", "caja", "dolphin", "thunar", "pcmanfm", "fma")
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
THUNAR_UCA = ("Thunar", "uca.xml")
THUNAR_PROGRAMS = ("thunar",)
THUNAR_RESTART = (
    "Thunar is running and reads its custom actions only when it starts: run 'thunar -q' "
    "(it starts again when needed) to see the change to 'Open in Fork'"
)
NAUTILUS_PROGRAMS = ("nautilus",)
NAUTILUS_RESTART = (
    "Nautilus is running and loads its extensions only when it starts: run 'nautilus -q' "
    "(it starts again when needed) to see the change to 'Open in Fork'"
)
# Desktop (display.desktop) -> its name, for desktops whose menu library watches only the
# application folders that existed when the session started (Cinnamon's CMenu, MATE's
# libmate-menu; GNOME Shell, Budgie (GIO), XFCE (garcon) and KDE (KSycoca) notice a new one).
MENU_AT_LOGIN_DESKTOPS = {"cinnamon": "Cinnamon", "mate": "MATE"}
MENU_RELOGIN = (
    "{desktop}'s menu lists the applications of {folder} only if that folder existed when you "
    "logged in, and fork-linux has just created it: log out and back in to see {app} in the menu"
)
# Our neutral placeholder icon, relative to a data directory (share/).
SCALABLE_ICON = ("icons", "hicolor", "scalable", "apps", f"{APP_ID}.svg")
DEFAULT_DATA_DIRS = "/usr/local/share:/usr/share"
DEFAULT_CONFIG_DIRS = "/etc/xdg"
TOOL_TIMEOUT = 60.0

# Kinds of recorded integration entries.
MENU = "menu"
ICON = "icon"
NAUTILUS = "nautilus"
NAUTILUS_SCRIPT = "nautilus-script"
NEMO = "nemo"
NEMO_FOLDER = "nemo-folder"
FMA = "fma"
DOLPHIN = "dolphin"
THUNAR = "thunar"
CLI_ALIAS = "cli-alias"
FILE_KINDS = (MENU, ICON, NAUTILUS, NAUTILUS_SCRIPT, NEMO, NEMO_FOLDER, FMA, DOLPHIN)
# Indexes the desktop cache tools write next to our files (update-desktop-database, gtk-update-icon-cache).
CACHE_INDEXES = frozenset({"mimeinfo.cache", "icon-theme.cache"})

AUTO = "auto"
# File manager name -> integration kinds it needs.
_MANAGER_KINDS = {
    "nautilus": (NAUTILUS, NAUTILUS_SCRIPT),
    # Nemo shows an action either on selected items or on the folder it shows (its background).
    "nemo": (NEMO, NEMO_FOLDER),
    "caja": (FMA,),
    "fma": (FMA,),
    "dolphin": (DOLPHIN,),
    "thunar": (THUNAR,),
    # PCManFM-Qt (LXQt, Lubuntu) and PCManFM (LXDE) read FileManager-Actions entries (libfm).
    "pcmanfm": (FMA,),
}
_FILE_MANAGER_KINDS = frozenset(kind for kinds in _MANAGER_KINDS.values() for kind in kinds)
# File manager -> (programs on PATH, menu entries in */applications) that show it is installed.
_MANAGER_PROBES = {
    "nautilus": (("nautilus",), ("org.gnome.Nautilus.desktop", "nautilus.desktop")),
    "nemo": (("nemo",), ("nemo.desktop",)),
    "caja": (("caja",), ("caja.desktop", "caja-browser.desktop")),
    "dolphin": (("dolphin",), ("org.kde.dolphin.desktop", "dolphin.desktop")),
    "thunar": (("thunar", "Thunar"), ("thunar.desktop", "Thunar.desktop")),
    "pcmanfm": (("pcmanfm-qt", "pcmanfm"), ("pcmanfm-qt.desktop", "pcmanfm.desktop")),
    "fma": (("fma-config-tool",), ("fma-config-tool.desktop",)),
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
    config_dirs: tuple[Path, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> _Dirs:
        home = Path(env.get("HOME") or Path.home())

        def xdg(name: str, default: Path) -> Path:
            value = env.get(name, "")
            return Path(value) if os.path.isabs(value) else default

        def search(name: str, default: str) -> tuple[Path, ...]:
            raw = env.get(name) or default
            return tuple(Path(item) for item in raw.split(":") if os.path.isabs(item))

        return cls(
            data=xdg("XDG_DATA_HOME", home / ".local" / "share"),
            config=xdg("XDG_CONFIG_HOME", home / ".config"),
            bin=xdg("XDG_BIN_HOME", home / ".local" / "bin"),
            data_dirs=search("XDG_DATA_DIRS", DEFAULT_DATA_DIRS),
            config_dirs=search("XDG_CONFIG_DIRS", DEFAULT_CONFIG_DIRS),
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
    def scalable_icon(self) -> Path:
        return self.data.joinpath(*SCALABLE_ICON)

    @property
    def thunar_uca(self) -> Path:
        return self.config.joinpath(*THUNAR_UCA)

    def system_thunar_uca(self) -> Path | None:
        """The ``Thunar/uca.xml`` Thunar reads when the user has none: the first in ``$XDG_CONFIG_DIRS``."""
        for base in self.config_dirs:
            candidate = base.joinpath(*THUNAR_UCA)
            if candidate.is_file():
                return candidate
        return None

    def kind_path(self, kind: str) -> Path:
        """Where the file of a file-manager ``kind`` goes."""
        return {
            NAUTILUS: self.data / "nautilus-python" / "extensions" / "fork_linux_nautilus.py",
            NAUTILUS_SCRIPT: self.data / "nautilus" / "scripts" / LABEL,
            NEMO: self.data / "nemo" / "actions" / "fork-linux-open.nemo_action",
            NEMO_FOLDER: self.data / "nemo" / "actions" / "fork-linux-open-folder.nemo_action",
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
    """``integrations.json``: what we installed, keyed by path, and the directories we had to create."""

    def __init__(self, path: Path, entries: dict[str, dict[str, Any]], dirs: set[str] | None = None) -> None:
        self.path = path
        self.entries = entries
        self.dirs = set() if dirs is None else dirs

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
        raw_dirs = data.get("dirs") if isinstance(data, dict) else None
        dirs = {item for item in raw_dirs if isinstance(item, str)} if isinstance(raw_dirs, list) else set()
        return cls(path, entries, dirs)

    def get(self, path: Path) -> dict[str, Any] | None:
        return self.entries.get(str(path))

    def record(self, path: Path, kind: str, **fields: Any) -> None:
        self.entries[str(path)] = {"path": str(path), "kind": kind, **fields}

    def forget(self, path: Path) -> None:
        self.entries.pop(str(path), None)

    def save(self) -> None:
        """Write the registry; without entries it is deleted (created directories still in use stay)."""
        if not self.entries:
            if self.path.exists():
                self.path.unlink()
            return
        data: dict[str, Any] = {
            "schema": REGISTRY_SCHEMA,
            "entries": sorted(self.entries.values(), key=lambda e: e["path"]),
        }
        if self.dirs:
            data["dirs"] = sorted(self.dirs)
        fsutil.atomic_write(self.path, json.dumps(data, indent=2) + "\n", mode=0o600)


def _missing_dirs(directory: Path) -> list[Path]:
    """``directory`` and its ancestors that do not exist yet, deepest first."""
    missing = []
    while not os.path.lexists(directory):
        missing.append(directory)
        directory = directory.parent
    return missing


def _make_parents(path: Path, registry: _Registry) -> None:
    """Create the missing directories above ``path``, recording each one so :func:`remove` can drop it again."""
    missing = _missing_dirs(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    registry.dirs.update(str(directory) for directory in missing)


def _prune_dirs(registry: _Registry, dirs: _Dirs) -> None:
    """Remove the directories :func:`install` created that are empty again (deepest first) and forget them."""
    for key in sorted(registry.dirs, key=len, reverse=True):
        if _drop_if_empty(Path(key), dirs):
            registry.dirs.discard(key)


def _drop_if_empty(directory: Path, dirs: _Dirs) -> bool:
    """Remove ``directory`` when nothing but desktop cache indexes is left in it; True when it is gone.

    A cache index (``mimeinfo.cache``...) alone does not keep the directory: it only indexed our files.
    """
    ours = _inside_user_dirs(directory, dirs) or os.path.abspath(directory) == os.path.abspath(dirs.bin)
    if not ours or directory.is_symlink():
        return False
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not set(names) <= CACHE_INDEXES:
        return False
    try:
        for name in names:
            (directory / name).unlink()
        directory.rmdir()
    except OSError as exc:
        log.debug("keeping %s: %s", directory, exc)
        return False
    return True


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
    _make_parents(path, registry)
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
    if kind == NEMO_FOLDER:
        return render_template("nemo_action_folder", common), 0o644
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


def _uca_bytes(root: ET.Element) -> bytes:
    """``root`` as fork-linux writes ``uca.xml`` (tab indented, with an XML declaration)."""
    if not len(root) and not (root.text or "").strip():
        root.text = None  # ET.indent leaves an element without children alone
    ET.indent(root, space="\t")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n").encode("utf-8")


def _tell_restart(programs: tuple[str, ...], message: str) -> None:
    """Warn ``message`` when one of ``programs`` runs: it would keep showing the old actions."""
    if procs.running(programs):
        log.warning(message)


def _thunar_changed() -> None:
    """Tell the user to restart a running Thunar: it reads ``uca.xml`` only when it starts."""
    _tell_restart(THUNAR_PROGRAMS, THUNAR_RESTART)


def _nautilus_changed() -> None:
    """Tell the user to restart a running Nautilus: it loads extensions only when it starts."""
    _tell_restart(NAUTILUS_PROGRAMS, NAUTILUS_RESTART)


def _write_uca(path: Path, root: ET.Element) -> None:
    """Back up ``uca.xml``, then write ``root`` to it atomically (keeping its mode); nothing when unchanged."""
    content = _uca_bytes(root)
    mode = 0o644
    if path.exists():
        current = path.read_bytes()
        if current == content:
            return
        mode = path.stat().st_mode & 0o777
        fsutil.atomic_write(path.with_name(THUNAR_BACKUP), current, mode=mode)
    fsutil.atomic_write(path, content, mode=mode)
    _thunar_changed()


def _drop_ours(root: ET.Element) -> int:
    """Remove our ``<action>`` elements from ``root``; how many there were."""
    ours = [child for child in root if _ours(child)]
    for element in ours:
        root.remove(element)
    return len(ours)


def _thunar_seed(dirs: _Dirs) -> tuple[ET.Element, dict[str, Any]]:
    """What a new user ``uca.xml`` starts from, and the registry fields that describe it.

    Thunar reads the desktop's own file (e.g. ``/etc/xdg/xdg-xubuntu/Thunar/uca.xml``)
    only while the user has none, so that file's actions are copied first; without
    one (or when it cannot be parsed) the file starts empty. ``seed_sha256`` is the
    seed as fork-linux writes it, without our action: :func:`_remove_thunar` deletes
    the file only when it is back to exactly that.
    """
    fields: dict[str, Any] = {}
    system = dirs.system_thunar_uca()
    tree = _read_uca(system) if system is not None else None
    root = ET.Element("actions") if tree is None else tree.getroot()
    if tree is not None:
        fields["seed"] = str(system)
    _drop_ours(root)
    fields["seed_sha256"] = _sha256(_uca_bytes(root))
    return root, fields


def _install_thunar(dirs: _Dirs, cmd: list[str], registry: _Registry) -> bool:
    """Merge our action into ``uca.xml`` (seeded from the desktop's own file when missing); True on success."""
    path = dirs.thunar_uca
    entry = registry.get(path) or {}
    created = bool(entry.get("created"))
    fields = {key: entry[key] for key in ("seed", "seed_sha256") if key in entry}
    root: ET.Element | None = None
    if path.exists():
        tree = _read_uca(path)
        if tree is None:
            return False
        root = tree.getroot()
        if created and "seed_sha256" not in entry and all(_ours(child) for child in root if child.tag == "action"):
            # fork-linux 1.0.0 created the file with only our action, which hid the desktop's own actions.
            root = None
    if root is None:
        root, fields = _thunar_seed(dirs)
        created = True
    _drop_ours(root)
    if created and "seed_sha256" not in fields:
        # Recorded by a build without seed fields: adopt the seed when the file is still exactly it.
        seed_fields = _thunar_seed(dirs)[1]
        if _sha256(_uca_bytes(root)) == seed_fields["seed_sha256"]:
            fields = seed_fields
    root.append(_thunar_action(cmd))
    _make_parents(path, registry)
    _write_uca(path, root)
    registry.record(path, THUNAR, unique_id=UNIQUE_ID, created=created, **fields)
    return True


def _back_to_seed(root: ET.Element, entry: Mapping[str, Any]) -> bool:
    """True when ``root`` (our action already removed) is what fork-linux created the file from."""
    seed_sha = entry.get("seed_sha256")
    if isinstance(seed_sha, str):
        return _sha256(_uca_bytes(root)) == seed_sha
    # Created before seeding existed: from an empty <actions>.
    return not any(child.tag == "action" for child in root)


def _remove_thunar(path: Path, entry: Mapping[str, Any]) -> bool:
    """Remove our action from ``uca.xml``; delete the file when we created it and it is back to its seed."""
    if not path.exists():
        return False
    tree = _read_uca(path)
    if tree is None:
        return False
    root = tree.getroot()
    if not _drop_ours(root):
        return False
    if entry.get("created") and _back_to_seed(root, entry):
        path.unlink()
        backup = path.with_name(THUNAR_BACKUP)
        if backup.exists():
            backup.unlink()
        _thunar_changed()
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
            _make_parents(link, registry)
            temp = link.with_name(f".{name}.fork-linux-tmp")
            if os.path.lexists(temp):
                temp.unlink()
            os.symlink(target, temp)
            os.replace(temp, link)
        registry.record(link, CLI_ALIAS, target=target)
        installed.append(link)
    return installed


def installed_file_managers(env: Mapping[str, str] | None = None) -> list[str]:
    """The file managers of :data:`FILE_MANAGERS` installed here (program on ``PATH`` or menu entry)."""
    environ: Mapping[str, str] = os.environ if env is None else env
    dirs = _Dirs.from_env(environ)
    search = sandbox.clean_env(environ).get("PATH")
    menus = [dirs.applications, *(base / "applications" for base in dirs.data_dirs)]
    found = []
    for name in FILE_MANAGERS:
        programs, entries = _MANAGER_PROBES[name]
        if any(shutil.which(program, path=search) for program in programs) or any(
            (menu / entry).is_file() for menu in menus for entry in entries
        ):
            found.append(name)
    return found


def _names(file_managers: str | Iterable[str] | None) -> list[str]:
    """The lower-case names in ``file_managers`` (a comma-separated string or an iterable)."""
    if file_managers is None:
        return []
    if isinstance(file_managers, str):
        return [item.strip().lower() for item in file_managers.split(",") if item.strip()]
    return [str(item).strip().lower() for item in file_managers]


def _kinds(names: list[str], detect: Callable[[], list[str]]) -> list[str]:
    """Integration kinds for ``auto`` (``detect()``), ``all``, ``none`` or file manager names."""
    if names == ["none"]:
        return []
    if "all" in names:
        names = list(FILE_MANAGERS)
    elif AUTO in names:
        names = [*detect(), *(name for name in names if name != AUTO)]
    unknown = [name for name in names if name not in _MANAGER_KINDS]
    if unknown:
        raise UsageError(
            f"unknown file manager(s): {', '.join(unknown)}",
            hint=f"choose from: {AUTO}, all, none, {', '.join(FILE_MANAGERS)}",
        )
    kinds: list[str] = []
    for name in names:
        kinds.extend(kind for kind in _MANAGER_KINDS[name] if kind not in kinds)
    return kinds


def _drop_unused_kinds(dirs: _Dirs, registry: _Registry, kinds: list[str]) -> list[Path]:
    """Remove recorded file-manager actions that are not in ``kinds`` (``auto``: that manager is gone)."""
    removed = []
    for key, entry in sorted(registry.entries.items()):
        kind = entry["kind"]
        path = Path(key)
        if kind not in _FILE_MANAGER_KINDS or kind in kinds or not _inside_user_dirs(path, dirs):
            continue
        if kind == THUNAR:
            registry.forget(path)
            done = _remove_thunar(path, entry)
        else:
            done = _remove_owned(path, registry)
        if done:
            log.info("removed %s: its file manager is not installed", path)
            removed.append(path)
    return removed


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
    dirs: _Dirs,
    cmd: list[str],
    registry: _Registry,
    system_desktop_present: bool | None,
    installed: list[Path],
    desktop: str,
) -> bool:
    """Write the personal menu entry unless a packaged one exists; True if the menu changed.

    ``desktop`` is the session's desktop (:func:`display.desktop`), for :func:`_menu_folder_created`.
    """
    system = _system_desktop(dirs) if system_desktop_present is None else None
    if system is not None or system_desktop_present:
        log.info("a packaged menu entry exists (%s); not adding a personal one", system)
        return _remove_owned(dirs.menu_file, registry)
    new_folder = not dirs.applications.is_dir()
    if _write_owned(dirs.menu_file, _render_menu(cmd).encode("utf-8"), 0o644, MENU, registry):
        installed.append(dirs.menu_file)
        if new_folder:
            _menu_folder_created(dirs, desktop)
        return True
    return False


def _menu_folder_created(dirs: _Dirs, desktop: str) -> None:
    """Tell a Cinnamon or MATE user that the menu shows the new folder's entries after the next login."""
    name = MENU_AT_LOGIN_DESKTOPS.get(desktop)
    if name is not None:
        log.warning(MENU_RELOGIN.format(desktop=name, folder=dirs.applications, app=APP_NAME))


def _install_icons(fork_exe: Path, dirs: _Dirs, registry: _Registry) -> list[Path]:
    """Extract Fork's icon into the hicolor theme and record the files written.

    Besides the PNGs, the per-user scalable icon becomes an SVG showing Fork's
    largest image, so no size falls back to our placeholder (GTK takes the
    scalable directory for every size without its own PNG).
    """
    missing = _missing_dirs(dirs.hicolor)
    existing = {path for pattern in ("*", "*/*") for path in dirs.hicolor.glob(pattern)}
    try:
        pngs = icon_extract.extract_icons(fork_exe, dirs.hicolor, APP_ID)
    except ForkLinuxError as exc:
        log.warning("cannot extract Fork's icon from %s: %s", fork_exe, exc)
        pngs = []
    for png in pngs:
        registry.record(png, ICON, sha256=fsutil.sha256_file(png))
        # <N>x<N>/apps and <N>x<N> that the extraction created.
        registry.dirs.update(str(path) for path in (png.parent, png.parent.parent) if path not in existing)
    if pngs:
        registry.dirs.update(str(path) for path in missing)
        svg = icon_extract.scalable_svg(pngs[-1].read_bytes())
        if _write_owned(dirs.scalable_icon, svg, 0o644, ICON, registry):
            pngs.append(dirs.scalable_icon)
    return pngs


def _shipped_placeholder() -> Path | None:
    """Our placeholder SVG in this installation (``share/``) or checkout (``data/``)."""
    root = resources.install_root()
    for candidate in (root.joinpath("share", *SCALABLE_ICON), root.joinpath("data", *SCALABLE_ICON)):
        if candidate.is_file():
            return candidate
    return None


def _install_placeholder(dirs: _Dirs, registry: _Registry) -> list[Path]:
    """Our placeholder as the user's scalable icon until Fork's is extracted; the paths written.

    Skipped when the file is already ours (the placeholder, or Fork's icon that
    replaced it) and when a package installed the placeholder in ``$XDG_DATA_DIRS``.
    """
    path = dirs.scalable_icon
    if registry.get(path) is not None and os.path.lexists(path):
        return []
    own = os.path.realpath(path)
    for base in dirs.data_dirs:
        candidate = base.joinpath(*SCALABLE_ICON)
        if candidate.is_file() and os.path.realpath(candidate) != own:
            return []
    source = _shipped_placeholder()
    if source is None:
        return []
    return [path] if _write_owned(path, source.read_bytes(), 0o644, ICON, registry) else []


def _install_kind(kind: str, dirs: _Dirs, cmd: list[str], registry: _Registry) -> list[Path]:
    """Install one file-manager integration; the path written, if any."""
    if kind == THUNAR:
        return [dirs.thunar_uca] if _install_thunar(dirs, cmd, registry) else []
    content, mode = _render_kind(kind, cmd)
    path = dirs.kind_path(kind)
    data = content.encode("utf-8")
    unchanged = path.is_file() and not path.is_symlink() and fsutil.sha256_file(path) == _sha256(data)
    if not _write_owned(path, data, mode, kind, registry):
        return []
    if kind == NAUTILUS and not unchanged:
        _nautilus_changed()
    return [path]


def install(
    paths: Paths,
    env: Mapping[str, str] | None = None,
    *,
    exec_cmd: list[str] | None = None,
    fork_exe: Path | None = None,
    menu: bool = True,
    icons: bool = True,
    file_managers: str | Iterable[str] | None = AUTO,
    cli_alias: bool = False,
    system_desktop_present: bool | None = None,
    runner: Runner | None = None,
    refresh_recorded: bool = False,
) -> list[Path]:
    """Install the per-user integration; return every path that is now ours.

    ``file_managers`` is ``auto`` (the installed ones, see
    :func:`installed_file_managers`; actions installed earlier for a file
    manager that is gone are removed), ``all``, ``none``, or names from
    :data:`FILE_MANAGERS` (comma separated or a list). Files that exist but
    were not written by us (or were changed by the user) are left alone with
    a warning. ``refresh_recorded`` also rewrites every file-manager action
    and ``fork`` / ``fork-linux`` link installed earlier, for the current
    command (an AppImage that was moved, scenario A9).
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    dirs = _Dirs.from_env(environ)
    cmd = list(exec_cmd) if exec_cmd else launcher_command(environ)
    names = _names(file_managers)
    kinds = _kinds(names, lambda: installed_file_managers(environ))
    registry = _Registry.load(paths.integrations_file)
    if refresh_recorded:
        recorded = {entry["kind"] for entry in registry.entries.values()}
        kinds.extend(sorted((recorded & _FILE_MANAGER_KINDS) - set(kinds)))
        cli_alias = cli_alias or CLI_ALIAS in recorded
    installed: list[Path] = []
    menu_changed = menu and _install_menu(
        dirs, cmd, registry, system_desktop_present, installed, current_desktop(environ)
    )
    icons_changed = False
    if icons:
        written = _install_icons(Path(fork_exe), dirs, registry) if fork_exe and Path(fork_exe).is_file() else []
        written = written or _install_placeholder(dirs, registry)
        installed.extend(written)
        icons_changed = bool(written)
    if AUTO in names:
        _drop_unused_kinds(dirs, registry, kinds)
    for kind in kinds:
        installed.extend(_install_kind(kind, dirs, cmd, registry))
    if cli_alias:
        installed.extend(_install_aliases(dirs, cmd, registry))
    _refresh(dirs, environ, Runner() if runner is None else runner, menu=menu_changed, icons=icons_changed)
    _prune_dirs(registry, dirs)
    registry.save()
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


def _remove_entry(path: Path, entry: Mapping[str, Any], dirs: _Dirs, registry: _Registry) -> bool:
    """Remove one recorded file (or take our action out of ``uca.xml``); True when something was removed."""
    kind = entry["kind"]
    if not _inside_user_dirs(path, dirs):
        log.warning("not removing %s: it is outside the directories fork-linux installs into", path)
        return False
    if kind == THUNAR:
        registry.forget(path)
        return _remove_thunar(path, entry)
    if kind == CLI_ALIAS:
        registry.forget(path)
        target = str(entry.get("target", ""))
        if not (target and _link_is_ours(path, target, target)):
            return False
        path.unlink()
        return True
    return _remove_owned(path, registry)


def remove(paths: Paths, env: Mapping[str, str] | None = None, *, runner: Runner | None = None) -> list[Path]:
    """Remove everything :func:`install` recorded that is still ours; return the paths removed or edited."""
    environ: Mapping[str, str] = os.environ if env is None else env
    dirs = _Dirs.from_env(environ)
    registry = _Registry.load(paths.integrations_file)
    removed: list[Path] = []
    kinds_removed: set[str] = set()
    for key, entry in sorted(registry.entries.items()):
        path = Path(key)
        if _remove_entry(path, entry, dirs, registry):
            removed.append(path)
            kinds_removed.add(entry["kind"])
    registry.entries.clear()
    if NAUTILUS in kinds_removed:
        _nautilus_changed()
    _refresh(
        dirs,
        environ,
        Runner() if runner is None else runner,
        menu=MENU in kinds_removed,
        icons=ICON in kinds_removed,
    )
    _prune_dirs(registry, dirs)
    registry.save()
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
