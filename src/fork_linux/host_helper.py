"""``fork-linux-host``: run Linux desktop tools on behalf of Fork.

Fork (through ``fl-launch.exe`` and the bridge) asks for a terminal in a
repository, to reveal a file in the file manager, to open a file or web link,
or to run a diff / merge tool. Arguments may be Windows paths (``C:\\…``,
``Z:\\…``), which are mapped back through the prefix's drive letters
(``$WINEPREFIX``). Tools run with the user's environment minus Wine's
variables and sandbox injections, through ``flatpak-spawn --host`` inside
Flatpak.

Exit status: the tool's own status (``diff``, ``merge``, ``edit`` wait for
it), 0 once a detached tool started (``terminal``, ``open``, ``reveal``), 127
when no suitable tool is installed, 2 for bad arguments.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import sandbox
from .config import Config
from .errors import ForkLinuxError, NotSetUpError, UsageError
from .pathmap import PathMap
from .paths import Paths
from .procrun import Runner

PROG = "fork-linux-host"
EXIT_USAGE = 2
EXIT_NOT_FOUND = 127
EXIT_CANNOT_EXEC = 126
EXIT_INTERRUPTED = 130
TERMINAL_ENV = "FORK_LINUX_TERMINAL"
DBUS_TIMEOUT = 10.0
URL_SCHEMES = ("http", "https", "mailto")

USAGE = f"""usage: {PROG} VERB ARGUMENTS

Runs Linux desktop tools for Fork for Linux (unofficial). Paths may be Linux
or Windows paths (mapped through $WINEPREFIX).

verbs:
  terminal [DIR]                   open a terminal in DIR (default: the working directory)
  reveal PATH                      show PATH in the file manager
  open PATH|URL                    open a file or an http(s)/mailto link
  diff LEFT RIGHT                  compare two files (waits)
  merge BASE LOCAL REMOTE MERGED   three-way merge into MERGED (waits)
  edit PATH [LINE]                 open PATH in its default editor (waits)

terminal: $FORK_LINUX_TERMINAL or [integration] terminal (a command, {{dir}} is
replaced by the directory), else xdg-terminal-exec, $TERMINAL, then known terminals.
"""

# Terminal -> arguments that open it in {dir}; an empty tuple relies on the working directory.
TERMINALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ptyxis", ("--new-window", "-d", "{dir}")),
    ("kgx", ("--working-directory={dir}",)),
    ("gnome-terminal", ("--working-directory={dir}",)),
    ("konsole", ("--workdir", "{dir}")),
    ("xfce4-terminal", ("--working-directory={dir}",)),
    ("tilix", ("--working-directory={dir}",)),
    ("terminator", ("--working-directory={dir}",)),
    ("mate-terminal", ("--working-directory={dir}",)),
    ("lxterminal", ("--working-directory={dir}",)),
    ("alacritty", ("--working-directory", "{dir}")),
    ("kitty", ("--directory", "{dir}")),
    ("foot", ("--working-directory={dir}",)),
    ("wezterm", ("start", "--cwd", "{dir}")),
    ("ghostty", ("--working-directory={dir}",)),
    ("x-terminal-emulator", ()),
    ("xterm", ()),
)
_TERMINAL_ARGS = dict(TERMINALS)
# $XDG_CURRENT_DESKTOP entry -> the terminal tried first on that desktop.
DESKTOP_TERMINALS = {
    "KDE": "konsole",
    "XFCE": "xfce4-terminal",
    "MATE": "mate-terminal",
    "LXDE": "lxterminal",
}
# Diff/merge tool -> (diff arguments, merge arguments or None).
DIFF_TOOLS: tuple[tuple[str, tuple[str, ...], tuple[str, ...] | None], ...] = (
    ("meld", ("{left}", "{right}"), ("--output={merged}", "{local}", "{base}", "{remote}")),
    ("kdiff3", ("{left}", "{right}"), ("--auto", "{base}", "{local}", "{remote}", "-o", "{merged}")),
    ("bcompare", ("{left}", "{right}"), ("{local}", "{remote}", "{base}", "-mergeoutput={merged}")),
    (
        "code",
        ("--wait", "--diff", "{left}", "{right}"),
        ("--wait", "--merge", "{remote}", "{local}", "{base}", "{merged}"),
    ),
    ("kompare", ("{left}", "{right}"), None),
)
FILE_MANAGER1 = "org.freedesktop.FileManager1"
FILE_MANAGER1_PATH = "/org/freedesktop/FileManager1"

_WINDOWS_PATH = re.compile(r"^(?:[A-Za-z]:(?:[\\/]|$)|\\\\[?.]\\)")
# A URL scheme has at least two characters, so a drive path such as C:\x never looks like one.
_URL = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]+):")
_FIELD = re.compile(r"\{(\w+)\}")
# A constant script: the name to look up is passed as "$1", never spliced into the code.
_HOST_LOOKUP = ("sh", "-c", 'command -v -- "$1"', "sh")


class ToolNotFound(ForkLinuxError):
    """No suitable host tool is installed (exit status 127)."""


def fill(template: Sequence[str], values: Mapping[str, str]) -> list[str]:
    """Replace ``{name}`` fields in each argument (one pass; values are never re-scanned)."""
    return [_FIELD.sub(lambda m: values.get(m.group(1), m.group(0)), arg) for arg in template]


def file_uri(path: Path | str) -> str:
    """``file:///…`` with every unsafe byte percent-encoded (quotes and commas included)."""
    return "file://" + quote(os.fsencode(os.path.abspath(path)), safe="/")


class Host:
    """Runs host tools for one request; seams (runner, popen) make it testable."""

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        runner: Runner | None = None,
        popen: Callable[..., Any] | None = None,
    ) -> None:
        self.env: dict[str, str] = dict(os.environ if env is None else env)
        self.tool_env = {k: v for k, v in sandbox.clean_env(self.env).items() if not k.startswith("WINE")}
        self.runner = Runner() if runner is None else runner
        self.popen = subprocess.Popen if popen is None else popen
        self.kind = sandbox.detect(self.env)

    # --- lookup and spawning -------------------------------------------------

    def which(self, name: str) -> str | None:
        """Where the host has ``name`` (inside Flatpak: asked on the host), or ``None``."""
        if self.kind == sandbox.FLATPAK:
            result = self.runner.run([*sandbox.HOST_SPAWN, *_HOST_LOOKUP, name], env=self.tool_env)
            found = result.stdout.strip()
            return found if result.ok and found else None
        return self.runner.which(name, self.tool_env.get("PATH"))

    def spawn(self, argv: list[str], cwd: str | None = None) -> int:
        """Start ``argv`` detached (own session, no stdio); 0 once started."""
        try:
            self.popen(
                sandbox.host_argv(argv, self.kind),
                cwd=cwd,
                env=self.tool_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except FileNotFoundError:
            raise ToolNotFound(f"{argv[0]} is not installed") from None
        except OSError as exc:
            raise ForkLinuxError(f"cannot start {argv[0]}: {exc.strerror or exc}") from exc
        return 0

    def wait(self, argv: list[str]) -> int:
        """Run ``argv`` and return its exit status."""
        result = self.runner.run(sandbox.host_argv(argv, self.kind), env=self.tool_env)
        if result.returncode == EXIT_NOT_FOUND and result.stderr.startswith("fork-linux: cannot execute"):
            raise ToolNotFound(f"{argv[0]} is not installed")
        return result.returncode

    # --- arguments -------------------------------------------------------------

    def path(self, arg: str) -> str:
        """``arg`` as a Linux path: Windows paths are mapped through ``$WINEPREFIX``."""
        prefix = self.env.get("WINEPREFIX", "")
        if not _WINDOWS_PATH.match(arg) or not prefix:
            return arg
        try:
            pathmap = PathMap.from_prefix(prefix)
        except NotSetUpError:
            return arg
        return str(pathmap.win_to_unix(arg))

    def existing(self, arg: str) -> str:
        """``path(arg)``, which must exist (:class:`UsageError` otherwise)."""
        path = self.path(arg)
        if not os.path.lexists(path):
            raise UsageError(f"no such file or directory: {path}")
        return os.path.abspath(path)

    # --- terminal --------------------------------------------------------------

    def _configured_terminal(self) -> str:
        """``$FORK_LINUX_TERMINAL``, else ``[integration] terminal`` (``auto`` when unreadable)."""
        explicit = self.env.get(TERMINAL_ENV, "").strip()
        if explicit:
            return explicit
        try:
            return Config.load(Paths.from_env(self.env), self.env).get("integration", "terminal").strip()
        except ForkLinuxError:
            return "auto"

    def _command(self, words: list[str], directory: str) -> list[str]:
        """A terminal command: ``{dir}`` fields filled, or a known terminal's own arguments."""
        if any("{dir}" in word for word in words):
            return fill(words, {"dir": directory})
        if len(words) == 1 and os.path.basename(words[0]) in _TERMINAL_ARGS:
            return [words[0], *fill(_TERMINAL_ARGS[os.path.basename(words[0])], {"dir": directory})]
        return list(words)

    def _desktop_order(self) -> list[str]:
        """Known terminals, the desktop's own first."""
        desktops = [item.strip().upper() for item in self.env.get("XDG_CURRENT_DESKTOP", "").split(":")]
        preferred = [DESKTOP_TERMINALS[name] for name in desktops if name in DESKTOP_TERMINALS]
        return [*preferred, *(name for name, _args in TERMINALS if name not in preferred)]

    def terminal_argv(self, directory: str) -> list[str]:
        """The terminal command for ``directory``; :class:`ToolNotFound` when there is none."""
        configured = self._configured_terminal()
        if configured and configured != "auto":
            return self._command(_split(configured), directory)
        if self.which("xdg-terminal-exec"):
            return ["xdg-terminal-exec", f"--dir={directory}"]
        words = _split(self.env.get("TERMINAL", ""))
        if words and self.which(words[0]):
            return self._command(words, directory)
        for name in self._desktop_order():
            if self.which(name):
                return [name, *fill(_TERMINAL_ARGS[name], {"dir": directory})]
        raise ToolNotFound(
            "no terminal emulator found",
            hint=f"install one, or set {TERMINAL_ENV} (e.g. 'gnome-terminal --working-directory={{dir}}')",
        )

    def terminal(self, directory: str = "") -> int:
        """Open a terminal in ``directory`` (a file means its folder; empty: the working directory).

        Fork's Console button runs ``fl-launch.exe terminal`` with the
        repository as its working directory and no argument; the bridge daemon
        starts this helper in that (translated) directory.
        """
        path = self.existing(directory or os.getcwd())
        if not os.path.isdir(path):
            path = os.path.dirname(path)
        return self.spawn(self.terminal_argv(path), cwd=path)

    # --- file manager and default applications ----------------------------------

    def _try(self, argv: list[str]) -> bool:
        """Run a quick D-Bus call; True when it succeeded."""
        if not self.which(argv[0]):
            return False
        try:
            result = self.runner.run(sandbox.host_argv(argv, self.kind), env=self.tool_env, timeout=DBUS_TIMEOUT)
        except ForkLinuxError:
            return False
        return result.ok

    def reveal(self, target: str) -> int:
        """Select ``target`` in the file manager (FileManager1), else open its folder."""
        path = self.existing(target)
        uri = file_uri(path)
        gdbus = [
            "gdbus", "call", "--session",
            "--dest", FILE_MANAGER1,
            "--object-path", FILE_MANAGER1_PATH,
            "--method", f"{FILE_MANAGER1}.ShowItems",
            f"['{uri}']", "",
        ]
        if self._try(gdbus):
            return 0
        dbus_send = [
            "dbus-send", "--session", "--print-reply", f"--dest={FILE_MANAGER1}",
            FILE_MANAGER1_PATH, f"{FILE_MANAGER1}.ShowItems",
            f"array:string:{uri}", "string:",
        ]
        if self._try(dbus_send):
            return 0
        return self.spawn(["xdg-open", os.path.dirname(path)])

    def open(self, target: str) -> int:
        """Open a file with its default application, or an http(s) / mailto link."""
        url = _URL.match(target)
        if url is not None:
            if url.group(1).lower() not in URL_SCHEMES:
                raise UsageError(f"refusing to open a {url.group(1)!r} link", hint="only http, https and mailto links")
            return self.spawn(["xdg-open", target])
        return self.spawn(["xdg-open", self.existing(target)])

    def edit(self, target: str, line: str | None = None) -> int:
        """Open ``target`` in its default editor (the line number is accepted and ignored)."""
        if line is not None and not line.isdigit():
            raise UsageError(f"not a line number: {line!r}")
        return self.wait(["xdg-open", self.existing(target)])

    # --- diff and merge -----------------------------------------------------------

    def _tool(self, merge: bool) -> tuple[str, tuple[str, ...]]:
        """The first installed diff (or merge-capable) tool and its arguments."""
        for name, diff_args, merge_args in DIFF_TOOLS:
            args = merge_args if merge else diff_args
            if args is not None and self.which(name):
                return name, args
        kind = "merge" if merge else "diff"
        raise ToolNotFound(
            f"no {kind} tool found",
            hint="install meld, kdiff3, Beyond Compare (bcompare) or Visual Studio Code (code)",
        )

    def diff(self, left: str, right: str) -> int:
        """Compare two files with the first installed tool; wait for it."""
        name, args = self._tool(merge=False)
        values = {"left": self.existing(left), "right": self.existing(right)}
        return self.wait([name, *fill(args, values)])

    def merge(self, base: str, local: str, remote: str, merged: str) -> int:
        """Three-way merge into ``merged``; wait for the tool and return its status."""
        name, args = self._tool(merge=True)
        values = {
            "base": self.existing(base),
            "local": self.existing(local),
            "remote": self.existing(remote),
            "merged": os.path.abspath(self.path(merged)),
        }
        return self.wait([name, *fill(args, values)])


def _split(text: str) -> list[str]:
    """Shell-like word splitting; unbalanced quotes fall back to whitespace."""
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


# verb -> (method name, allowed argument counts)
VERBS: dict[str, tuple[str, tuple[int, ...]]] = {
    "terminal": ("terminal", (0, 1)),
    "reveal": ("reveal", (1,)),
    "open": ("open", (1,)),
    "diff": ("diff", (2,)),
    "merge": ("merge", (4,)),
    "edit": ("edit", (1, 2)),
}


def _error(message: str, hint: str = "") -> None:
    print(f"{PROG}: error: {message}", file=sys.stderr)
    if hint:
        print(f"hint: {hint}", file=sys.stderr)


def main(argv: Sequence[str] | None = None, *, host: Host | None = None) -> int:
    """Entry point of ``fork-linux-host``; returns the exit status."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help", "help"):
        print(USAGE, end="")
        return 0
    if not args or args[0] not in VERBS or len(args) - 1 not in VERBS[args[0]][1]:
        _error("expected a verb and its arguments" if not args else f"bad usage of {args[0]!r}")
        print(USAGE, end="", file=sys.stderr)
        return EXIT_USAGE
    method, _counts = VERBS[args[0]]
    try:
        handler = getattr(Host() if host is None else host, method)
        return int(handler(*args[1:]))
    except ToolNotFound as exc:
        _error(exc.message, exc.hint)
        return EXIT_NOT_FOUND
    except UsageError as exc:
        _error(exc.message, exc.hint)
        return EXIT_USAGE
    except ForkLinuxError as exc:
        _error(exc.message, exc.hint)
        return int(exc.exit_code)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
