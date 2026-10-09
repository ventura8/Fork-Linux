"""fork-linux settings: ``~/.config/fork-linux/config.ini`` over built-in defaults.

Every setting has a default in :data:`SCHEMA`; ``config.ini`` overrides it, and
an environment variable ``FORK_LINUX_<SECTION>_<KEY>`` (for example
``FORK_LINUX_WINE_DEBUG``) overrides both. Changes go through
:mod:`config_edit`, so the user's comments survive. ``data/defaults.ini`` is
the commented template written when the file is first created; it is
generated from :data:`SCHEMA` by :func:`render_defaults` (a test keeps the two
identical).
"""

from __future__ import annotations

import configparser
import logging
import os
import re
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import config_edit, fsutil, resources
from .errors import ForkLinuxError, UsageError
from .paths import Paths

log = logging.getLogger(__name__)

ENV_PREFIX = "FORK_LINUX_"
DEFAULTS_FILE = "defaults.ini"
FILE_MODE = 0o600

SOURCE_DEFAULT = "default"
SOURCE_FILE = "file"
SOURCE_ENV = "env"

BOOLEANS: Mapping[str, bool] = configparser.ConfigParser.BOOLEAN_STATES

# An ``id`` value: empty, or a single safe path component (no '/', no leading dot).
_ID_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._+\-]*)?$")

# configparser copies a [DEFAULT] section into every section; ours means nothing special.
_NO_DEFAULT_SECTION = "fork-linux:no-default-section"


@dataclass(frozen=True)
class KeySpec:
    """One setting: its default, how values are checked, and its documentation.

    ``kind`` is ``str``, ``list`` (comma separated), ``bool``, ``int``,
    ``choice`` or ``id`` (empty, or a name safe to use as a file name: letters,
    digits and ``._+-``, not starting with a dot). A ``choice`` may also accept
    an absolute path (``path_ok``) or a whole number in ``minimum..maximum``
    (when ``minimum`` is set).
    """

    default: str
    doc: str
    kind: str = "str"
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    path_ok: bool = False


SCHEMA: dict[str, dict[str, KeySpec]] = {
    "wine": {
        "provider": KeySpec(
            "managed",
            "Which Wine runs Fork: managed (a pinned, sha256-verified Wine build downloaded for you), "
            "system (your distribution's wine, 9.0 or newer), flatpak (Flatpak builds only) "
            "or an absolute path to a wine binary.",
            kind="choice",
            choices=("managed", "system", "flatpak"),
            path_ok=True,
        ),
        "build": KeySpec(
            "", "Managed Wine build id from the runtime manifest; empty means the tested default.", kind="id"
        ),
        "debug": KeySpec("-all", "WINEDEBUG channels while Fork runs (-all keeps Wine quiet)."),
        "driver": KeySpec(
            "auto",
            "Graphics driver: auto (X11 or XWayland), x11, or wayland (Wine's experimental Wayland driver).",
            kind="choice",
            choices=("auto", "x11", "wayland"),
        ),
        "renderer": KeySpec(
            "gdi",
            "Direct3D renderer used by Fork's WPF interface: gdi (most reliable), gl or vulkan.",
            kind="choice",
            choices=("gdi", "gl", "vulkan"),
        ),
        "extra_dll_overrides": KeySpec(
            "", "Extra WINEDLLOVERRIDES entries for Fork, e.g. dwrite=n,b;d3d9=b (empty: none)."
        ),
    },
    "fork": {
        "channel": KeySpec(
            "known-good",
            "Fork version that setup installs: known-good (tested with this fork-linux release) "
            "or latest (newest from Fork's release feed, verified after install).",
            kind="choice",
            choices=("known-good", "latest"),
        ),
        "update_policy": KeySpec(
            "auto",
            "Fork's own updater: auto (allowed; a snapshot is taken before each launch) "
            "or pinned (stay on the installed version; set by 'fork-linux rollback').",
            kind="choice",
            choices=("auto", "pinned"),
        ),
        "enforce_settings": KeySpec(
            "UpdateSubmodulesOnCheckout, DisableHardwareAcceleration",
            "Fork settings kept at Wine-safe values before every launch (comma separated; empty: none).",
            kind="list",
        ),
    },
    "display": {
        "dpi": KeySpec(
            "auto",
            "Fork's DPI: auto (from your desktop's scaling) or a number such as 96, 120, 144 or 192.",
            kind="choice",
            choices=("auto",),
            minimum=72,
            maximum=480,
        ),
        "theme": KeySpec(
            "follow",
            "Light or dark Windows theme: follow (your desktop's preference), light or dark.",
            kind="choice",
            choices=("follow", "light", "dark"),
        ),
    },
    "ssh": {
        "sync": KeySpec(
            "auto",
            "Share ~/.ssh keys and config with Fork's bundled ssh: auto (when ~/.ssh exists), on or off.",
            kind="choice",
            choices=("auto", "on", "off"),
        ),
        "mode": KeySpec(
            "link",
            "How private keys are shared: link (symbolic links) or copy (private 0600 copies).",
            kind="choice",
            choices=("link", "copy"),
        ),
        "agent_bridge": KeySpec(
            "off", "Experimental: let Fork's ssh use your ssh-agent ($SSH_AUTH_SOCK).", kind="bool"
        ),
    },
    "git": {
        "config_overlay": KeySpec(
            "translate",
            "Give Fork's bundled git a translated copy of ~/.gitconfig: translate or off.",
            kind="choice",
            choices=("translate", "off"),
        ),
        "env_overrides": KeySpec(
            "core.filemode=false, core.autocrlf=false",
            "git settings passed to Fork's git through GIT_CONFIG_COUNT (comma separated key=value).",
            kind="list",
        ),
        "safe_directory_all": KeySpec(
            "false",
            "Trust every repository (safe.directory=*) to silence 'dubious ownership' errors.",
            kind="bool",
        ),
        "bridge": KeySpec(
            "off",
            "Experimental native-git bridge; manage it with 'fork-linux git-bridge enable|disable'.",
            kind="bool",
        ),
    },
    "integration": {
        "terminal": KeySpec(
            "auto", "Terminal for Fork's 'Open in Terminal': auto or a command such as gnome-terminal or konsole."
        ),
        "open_files_natively": KeySpec(
            "true", "Open files and folders from Fork with your Linux applications instead of Wine's.", kind="bool"
        ),
    },
    "snapshots": {
        "keep": KeySpec(
            "2",
            "Snapshots of Fork's install directory kept for rollback (0 disables snapshots).",
            kind="int",
            minimum=0,
            maximum=100,
        ),
        "method": KeySpec(
            "auto",
            "How snapshots are made: auto (hard links when possible), hardlink or copy.",
            kind="choice",
            choices=fsutil.CLONE_METHODS,
        ),
    },
    "ui": {
        "progress": KeySpec(
            "auto",
            "How setup shows progress: auto (a dialog on a desktop, else the terminal), zenity, kdialog or terminal.",
            kind="choice",
            choices=("auto", "zenity", "kdialog", "terminal"),
        ),
    },
}


def env_var(section: str, key: str) -> str:
    """The environment variable that overrides ``[section] key``."""
    return f"{ENV_PREFIX}{section.upper()}_{key.upper()}"


def spec(section: str, key: str) -> KeySpec:
    """The :class:`KeySpec` of ``[section] key``; :class:`UsageError` if unknown."""
    keys = SCHEMA.get(section)
    if keys is None or key not in keys:
        raise UsageError(
            f"unknown setting: {section}.{key}",
            hint="'fork-linux config list' shows every setting",
        )
    return keys[key]


def parse_key(dotted: str) -> tuple[str, str]:
    """``"wine.provider"`` -> ``("wine", "provider")``; :class:`UsageError` if unknown."""
    section, sep, key = dotted.strip().lower().partition(".")
    if not sep:
        raise UsageError(
            f"settings are named SECTION.KEY, not {dotted!r}",
            hint="for example: fork-linux config get wine.provider",
        )
    spec(section, key)
    return section, key


def _int_problem(key_spec: KeySpec, value: str) -> str | None:
    """Why ``value`` is not a whole number within the spec's range, or ``None``."""
    try:
        number = int(value)
    except ValueError:
        return "expected a whole number"
    low, high = key_spec.minimum, key_spec.maximum
    if not ((low is not None and number < low) or (high is not None and number > high)):
        return None
    if low is None:
        return f"expected a number up to {high}"
    if high is None:
        return f"expected a number of at least {low}"
    return f"expected a number from {low} to {high}"


def problem(key_spec: KeySpec, value: str) -> str | None:
    """Why ``value`` is not acceptable for ``key_spec``, or ``None`` if it is."""
    if key_spec.kind == "id":
        return None if _ID_RE.match(value) else "expected an id made of letters, digits and . _ + -"
    if key_spec.kind == "bool":
        return None if value.lower() in BOOLEANS else "expected true or false (also on/off, yes/no, 1/0)"
    if key_spec.kind == "int":
        return _int_problem(key_spec, value)
    if key_spec.kind != "choice" or value in key_spec.choices:
        return None
    if key_spec.path_ok and os.path.isabs(value):
        return None
    if key_spec.minimum is not None and _int_problem(key_spec, value) is None:
        return None
    expected = "expected " + ", ".join(key_spec.choices)
    if key_spec.path_ok:
        expected += " or an absolute path"
    if key_spec.minimum is not None:
        expected += f" or a number from {key_spec.minimum} to {key_spec.maximum}"
    return expected


def render_defaults() -> str:
    """The commented template written to a new ``config.ini`` (``data/defaults.ini``)."""
    out = [
        "# Fork for Linux (unofficial) settings: ~/.config/fork-linux/config.ini",
        "#",
        "# Every setting is listed below, commented out, at its default value.",
        "# Uncomment a line to change it, or run: fork-linux config set SECTION.KEY VALUE",
        "# Environment variables FORK_LINUX_<SECTION>_<KEY> (e.g. FORK_LINUX_WINE_DEBUG)",
        "# override this file. 'fork-linux config list' shows the values in effect.",
    ]
    for section, keys in SCHEMA.items():
        out.extend(["", f"[{section}]"])
        for index, (key, key_spec) in enumerate(keys.items()):
            if index:
                out.append("")
            out.extend(textwrap.wrap(key_spec.doc, width=88, initial_indent="# ", subsequent_indent="# "))
            out.append(f"# {key} = {key_spec.default}".rstrip())
    return "\n".join(out) + "\n"


def template() -> str:
    """The packaged ``defaults.ini``, or the same text rendered from :data:`SCHEMA`."""
    path = resources.data_path(DEFAULTS_FILE)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return render_defaults()


def _read_file(path: Path) -> tuple[dict[tuple[str, str], str], list[str]]:
    """Known values and unknown setting names found in ``path`` (missing file: none)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, []
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError(f"cannot read {path}: {exc}", hint="check the file's permissions and encoding") from exc
    parser = configparser.ConfigParser(interpolation=None, default_section=_NO_DEFAULT_SECTION)
    try:
        parser.read_string(text, source=str(path))
    except configparser.Error as exc:
        raise UsageError(
            f"cannot parse {path}: {exc}",
            hint="fix it with 'fork-linux config edit', or move it aside to start again from the defaults",
        ) from exc
    values: dict[tuple[str, str], str] = {}
    unknown: list[str] = []
    for section in parser.sections():
        items = parser.items(section, raw=True)
        if not items and section not in SCHEMA:
            unknown.append(f"[{section}]")
        for key, value in items:
            if key in SCHEMA.get(section, {}):
                values[(section, key)] = value
            else:
                unknown.append(f"{section}.{key}")
    return values, unknown


class Config:
    """Effective settings: environment over ``config.ini`` over :data:`SCHEMA` defaults."""

    def __init__(
        self,
        path: Path,
        file_values: Mapping[tuple[str, str], str] | None = None,
        env: Mapping[str, str] | None = None,
        unknown: Sequence[str] = (),
    ) -> None:
        self.path = Path(path)
        self._file: dict[tuple[str, str], str] = dict(file_values or {})
        environ = {} if env is None else env
        self._env: dict[tuple[str, str], str] = {
            (section, key): environ[env_var(section, key)].strip()
            for section, keys in SCHEMA.items()
            for key in keys
            if environ.get(env_var(section, key), "").strip()
        }
        self.unknown: list[str] = list(unknown)

    @classmethod
    def load(cls, paths: Paths, env: Mapping[str, str] | None = None) -> Config:
        """Read ``paths.config_file`` (if any) and the ``FORK_LINUX_*`` overrides in ``env``."""
        path = paths.config_file
        file_values, unknown = _read_file(path)
        for name in unknown:
            log.warning("%s: ignoring unknown setting %s", path, name)
        return cls(path, file_values, os.environ if env is None else env, unknown)

    def source(self, section: str, key: str) -> str:
        """Where the value comes from: ``default``, ``file`` or ``env``."""
        spec(section, key)
        if (section, key) in self._env:
            return SOURCE_ENV
        return SOURCE_FILE if (section, key) in self._file else SOURCE_DEFAULT

    def raw(self, section: str, key: str) -> str:
        """The effective value of ``[section] key``, unchecked."""
        default = spec(section, key).default
        return self._env.get((section, key), self._file.get((section, key), default))

    def _origin(self, section: str, key: str) -> tuple[str, str]:
        """``(where, hint)`` describing how to fix the value of ``[section] key``."""
        if self.source(section, key) == SOURCE_ENV:
            name = env_var(section, key)
            return f"from ${name}", f"unset {name} or give it a valid value"
        return (
            f"in {self.path}",
            f"run 'fork-linux config set {section}.{key} VALUE' or 'fork-linux config unset {section}.{key}'",
        )

    def get(self, section: str, key: str) -> str:
        """The effective value; :class:`UsageError` if it is not valid for the setting."""
        value = self.raw(section, key)
        why = problem(spec(section, key), value)
        if why is not None:
            where, hint = self._origin(section, key)
            raise UsageError(f"invalid value {value!r} for {section}.{key} {where}: {why}", hint=hint)
        return value

    def getbool(self, section: str, key: str) -> bool:
        """The value as a boolean (true/false, on/off, yes/no, 1/0)."""
        value = self.get(section, key)
        if value.lower() not in BOOLEANS:
            where, hint = self._origin(section, key)
            raise UsageError(f"{section}.{key} {where} is {value!r}, not true or false", hint=hint)
        return BOOLEANS[value.lower()]

    def getint(self, section: str, key: str) -> int:
        """The value as an integer."""
        value = self.get(section, key)
        try:
            return int(value)
        except ValueError:
            where, hint = self._origin(section, key)
            raise UsageError(f"{section}.{key} {where} is {value!r}, not a number", hint=hint) from None

    def getlist(self, section: str, key: str) -> list[str]:
        """The value split on commas and newlines, blanks dropped."""
        value = self.get(section, key).replace("\n", ",")
        return [item.strip() for item in value.split(",") if item.strip()]

    def items(self) -> list[tuple[str, str, str, str]]:
        """``(section, key, value, source)`` for every setting, in schema order."""
        return [
            (section, key, self.raw(section, key), self.source(section, key))
            for section, keys in SCHEMA.items()
            for key in keys
        ]

    def problems(self, *, include_unknown: bool = True) -> list[str]:
        """Human-readable problems: invalid values and (optionally) unknown settings.

        :meth:`load` already logs a warning for each unknown setting, so
        callers that print problems right after loading pass
        ``include_unknown=False``.
        """
        found = []
        for section, key, value, _source in self.items():
            why = problem(SCHEMA[section][key], value)
            if why is not None:
                where, _hint = self._origin(section, key)
                found.append(f"{section}.{key} {where}: invalid value {value!r} ({why})")
        if include_unknown:
            found.extend(f"unknown setting {name} in {self.path}" for name in self.unknown)
        return found

    def set(self, section: str, key: str, value: str) -> None:
        """Validate and write ``[section] key = value`` to ``config.ini``, keeping comments."""
        key_spec = spec(section, key)
        value = str(value).strip()
        if "\n" in value or "\r" in value:
            raise UsageError(f"the value of {section}.{key} must be a single line")
        why = problem(key_spec, value)
        if why is not None:
            raise UsageError(f"invalid value {value!r} for {section}.{key}: {why}")
        try:
            if not os.path.lexists(self.path):
                fsutil.atomic_write(self.path, template(), mode=FILE_MODE)
            config_edit.set_option(self.path, section, key, value)
        except OSError as exc:
            raise ForkLinuxError(f"cannot write {self.path}: {exc}") from exc
        self._file[(section, key)] = value

    def unset(self, section: str, key: str) -> bool:
        """Remove ``[section] key`` from ``config.ini`` (back to the default); True if it was set."""
        spec(section, key)
        try:
            removed = config_edit.unset_option(self.path, section, key)
        except OSError as exc:
            raise ForkLinuxError(f"cannot write {self.path}: {exc}") from exc
        self._file.pop((section, key), None)
        return removed
