# Cinnamon desktop tier: fixes needed in `src/fork_linux`

Found by the Cinnamon cell of the desktop E2E tier (`scripts/ci-e2e-de.sh cinnamon [--fork]`,
Ubuntu 26.04 with Ubuntu Cinnamon's packages: Cinnamon 6.4, muffin 6.4.1, cinnamon-settings-daemon
6.4.3 (csd-xsettings, csd-settings-remap), Nemo 6.4.5, gnome-terminal 3.58 with xdg-terminal-exec
0.14, Yaru-cinnamon themes; see [../DE-E2E.md](../DE-E2E.md)). `src/` is not changed by the
desktop tier's work: the patch below is a proposal for whoever owns `src/fork_linux`. Until it is
applied, the Cinnamon tiers fail exactly these tests (no `xfail`, no skip):

| Test (Cinnamon cell) | Fails with | Fixed by |
|---|---|---|
| `test_de_integration.py::test_file_manager_runs_open_in_fork` | Nemo's context menu of the folder it shows has no "Open in Fork"; the keys land on "Open in Terminal" (timeout waiting for fork-linux's license question) | 1 |
| `test_de_integration.py::test_theme_detection_follows_the_desktop[dark:prefer-dark-mode]` | `assert 'light' == 'dark'` | 2 |
| `test_de_integration.py::test_scale_detection_follows_the_desktop[ui-scale-200-text-scaling-125]` | `assert '125' == '250'` | 3 |
| `test_de_integration.py::test_terminal_opens_in_the_directory` | the terminal picked in Preferred Applications (Xfce Terminal) never opens, gnome-terminal does (timeout waiting for a new xfce4-terminal window) | 4 |
| `test_de_fork.py::test_05_open_in_shell_opens_the_desktops_terminal` (then 06-09 fail fast) | Fork's Repository > Open In Shell opens gnome-terminal in the repository, not Xfce Terminal (timeout) | 4 |
| `test_de_fork.py::test_06_open_in_file_explorer_opens_the_desktops_file_manager` (once 4 is fixed) | no Nemo window: Nemo started by xdg-open inside Wine's process tree aborts | 5 |

Without the patch the integration tier passes 11 of 15 and the real-Fork tier stops at step 05
(`scripts/ci-e2e-de.sh cinnamon [--fork]` from this branch, 2026-10-11). An earlier real-Fork run,
in which xdg-terminal-exec happened to pick Xfce Terminal (before the recipe's user had run
gnome-terminal, see 4), passed 05 and stopped at 06 with no Nemo window (5).

Fixes 1 and 5 are the same as patches 1 and 2 of [budgie.md](budgie.md): Ubuntu Budgie's file
manager is Nemo too, and the two cells found them independently with the same design (the
`nemo-folder` kind and its `nemo_action_folder.in`, `Host._file_manager1` with `ShowFolders`).
Apply those once; the patch at the end of this file holds fixes 2, 3 and 4 and applies on top of
them (`git apply` budgie.md's patches 1 and 2, then this one).

Checked on a scratch copy of this branch with budgie.md's two patches and this one applied: the
release tarball built from it with `docker/Dockerfile.release`, installed by the same tiers through
`FL_E2E_DE_TARBALL`, passes the Cinnamon integration tier 15/15 and the real-Fork tier 9/9 (setup
264 s from the seed, the whole tier 319 s; with this cell's own, equivalent versions of fixes 1
and 5 it passed 15/15 and 9/9 twice too, setup 285 s and 789 s, the tier 340 s and 845 s on a
busy host). The whole unit
suite passes (3513 passed, 44 skipped for absent tools, Python 3.13 in
`fork-linux-ci-coverage:26.04` and Python 3.10 in `fork-linux-ci-jammy:22.04`) with 100 % line and
branch coverage of `src/fork_linux` (`--cov=fork_linux --cov-branch`); ruff,
`scripts/no-suppressions-lint.py` and `gen-data.py check` are clean (`fork-linux-ci-lint:26.04`).
Without the `src` part of the patch below, 16 of its new unit tests in `test_theme.py`,
`test_display.py` and `test_host_helper.py` fail.

## 1. Nemo has no "Open in Fork" for the folder it shows (budgie.md patch 1)

`nemo_action.in` declares `Selection=s`: Nemo lists such an action only when exactly one item is
selected. Right-clicking the empty space of a folder (the repository you are in) shows Create New
Folder, Create New Document, Open in Terminal, ... but no "Open in Fork", while Nautilus
(`get_background_items`), Thunar (`<directories/>` also matches the folder shown) and Caja /
PCManFM-Qt (FileManager-Actions `TargetLocation=true`) all offer it there. Checked in the
container with Nemo 6.4.5: with `repo-one` selected in its parent the menu has "Open in Fork";
inside `repo-one` (also after `nemo -q`) it does not. A `.nemo_action` cannot serve both: `%F` is
empty for a background click and `%P` is the parent for a selection. Fix: a second action,
`fork-linux-open-folder.nemo_action` (`Selection=none`, `Exec=... open --from-file-manager %P`),
as a new integration kind `nemo-folder` that `nemo` installs next to `nemo` (per user, and
system-wide through `gen-data.py fm-action nemo-folder` and `data/fm-actions/meson.build`). Nemo
watches its action folders, so a running Nemo shows it at once (no restart notice needed). With
it, the folder's menu reads Create New Folder, Create New Document, Open in Fork, Open in
Terminal, ...

## 2. Cinnamon's "Dark mode" preference is ignored

Cinnamon 6 keeps System Settings > Themes > Settings > "Dark mode" (Let applications decide /
Prefer dark mode / Prefer light mode), and the Light / Dark buttons of its simplified Themes page,
in `org.x.apps.portal color-scheme`: what xdg-desktop-portal-xapp hands applications as their
color scheme (`cs_themes.py`: `Gio.Settings.new("org.x.apps.portal")`, `set_enum("color-scheme",
...)`). `org.cinnamon.desktop.interface` has no `color-scheme` key, so `theme.detect` asked a key
that does not exist and then the theme name: with the light Yaru-cinnamon and "Prefer dark mode"
it answered light. Fix: on Cinnamon ask `org.x.apps.portal color-scheme` (dconf, then gsettings)
first; `default` falls through to the theme name as before.

## 3. Cinnamon's user interface scale with text scaling is read as the text scaling alone

System Settings > Display > User interface scale (Cinnamon 6.4's display panel) is a monitor
configuration of muffin's DisplayConfig (its global UI scale, `legacy-ui-scaling-factor`), not the
legacy `org.cinnamon.desktop.interface scaling-factor` key, which muffin does not apply live.
csd-xsettings publishes `Xft.dpi` as 96 x that scale x Fonts > Text scaling factor (checked in the
container: 96, 192 at scale 2, 144 at text 1.5, 240 at scale 2 with text 1.25), and
csd-settings-remap (a cinnamon-session autostart) copies the text scaling factor, but not the UI
scale, into `org.gnome.desktop.interface`. `display.scale_percent` asks GNOME's keys before
`Xft.dpi`, so at scale 2 with text 1.25 it answered 125 % while every X client (Wine included)
gets 240 DPI: Fork came out at half the size of everything else. Fix: Cinnamon's own probe is
`_from_xrdb` (`DESKTOP_PROBES["cinnamon"]`); GNOME's keys stay the fallback without `Xft.dpi`.

## 4. Fork's "Open In Shell" ignores the terminal picked in Cinnamon's Preferred Applications

The terminal a Cinnamon user picks is System Settings > Preferred Applications > Terminal:
`org.cinnamon.desktop.default-applications.terminal exec` (default `gnome-terminal`), which
Nemo's own "Open in Terminal" starts (Preferred Applications stores the chosen terminal's
desktop-entry `Exec`, e.g. `xfce4-terminal`, `tilix`, `ptyxis --new-window`).
`fork-linux-host terminal` never asked it: it took xdg-terminal-exec first, which gnome-terminal's
Recommends install on Ubuntu Cinnamon. Cinnamon does not configure xdg-terminal-exec. Until
gnome-terminal has run once, xdg-terminal-exec 0.14 picks the first terminal entry it finds,
`org.gnome.Terminal.Preferences.desktop`, i.e. `gnome-terminal --preferences`
(`xdg-terminal-exec --print-id` in the container): Fork's Repository > Open In Shell (and its
Shell button) opened gnome-terminal's Preferences window, no terminal. Once gnome-terminal has run,
it writes itself into `~/.config/x-cinnamon-xdg-terminals.list` and `xdg-terminals.list`, so the
user's own choice (Xfce Terminal, Tilix, ...) still never opens. Fix: on `X-Cinnamon`, after
`$FORK_LINUX_TERMINAL` / `[integration] terminal`, read that `exec` key (on the host inside
Flatpak), drop desktop-entry field codes, and use it when the program is installed: a known
terminal gets its own directory arguments, another one runs as stored, in the directory. Without
an answer the generic order follows, in which `X-CINNAMON` now prefers gnome-terminal like KDE
prefers konsole.

## 5. Fork's "Open In File Explorer" starts the file manager inside Wine's process tree (budgie.md patch 2)

Fork opens a folder with `explorer.exe FOLDER`, which fork-linux routes to `fork-linux-explorer`
-> `fork-linux-handoff open FOLDER` -> `fork-linux-host open`, i.e. `xdg-open FOLDER`. The
hand-off runs the helper as a transient systemd user unit when it can (outside Wine's process
tree); without a systemd user manager (this container, distrobox-like sessions, systems without
systemd) it runs it directly, and everything it starts inherits Wine's `NoNewPrivs=1` and its
seccomp filters (`/proc/self/status`: `NoNewPrivs: 1`, `Seccomp: 2`, `Seccomp_filters: 2`). On
Ubuntu 26.04 a GTK 3 program started that way aborts as soon as it loads a raster icon: gdk-pixbuf's
glycin loader exits early (`Gtk:ERROR ... ensure_surface_for_gicon: assertion failed ... Failed to
load /org/gtk/libgtk/icons/16x16/status/image-missing.png: Loader process exited early`; traced
in the tier with a logging hand-off: `nemo FOLDER` and Tilix started from there both exit with
status 134). Nemo does not keep a process running in a Cinnamon session (nemo-desktop draws the
desktop), so `xdg-open` -> `gio` started a new `nemo FOLDER` there and no window appeared (this is
the step-06 timeout the first Cinnamon attempt could not explain).
`reveal` already asks `org.freedesktop.FileManager1.ShowItems`, which the session bus answers by
starting the file manager as its own child. Fix: `open` of a directory asks
`FileManager1.ShowFolders` first (gdbus, else dbus-send, shared with `reveal`), then falls back to
`xdg-open`. This also keeps "Open In File Explorer" in the file manager when another program has
claimed `inode/directory` in `mimeapps.list` (an editor or a disk-usage tool), which `xdg-open`
would start instead.

## Merging with the other desktops' proposals

Besides building on budgie.md's patches, the hunks are independent of the GNOME, MATE, LXQt and
KDE proposals in this directory, but they touch the same places: `DESKTOP_PROBES` (GNOME adds
`"gnome": _from_xrdb`; together: `{"xfce": _from_xfce, "gnome": _from_xrdb, "cinnamon":
_from_xrdb}`), the module docstring and
the schema constants of `theme.py` (MATE, LXQt, KDE) and `test_host_helper.py`'s `_host` helper.
Apply them one after the other and resolve those lines by hand. budgie.md leaves reading the
desktops' own terminal setting to a maintainer decision; fix 4 is that decision for Cinnamon,
where a test fails without it (Budgie's `org.gnome.desktop.default-applications.terminal` would
be one more `DESKTOP_TERMINAL_SCHEMAS` entry).

## The patch: fixes 2, 3 and 4 (src + unit tests)

Apply from the repository root with `git apply`, after budgie.md's patches 1 and 2.

```diff
diff --git a/src/fork_linux/display.py b/src/fork_linux/display.py
index 85ad055..5ce4b9f 100644
--- a/src/fork_linux/display.py
+++ b/src/fork_linux/display.py
@@ -12,7 +12,11 @@ Scale sources, first match wins:
 
 The desktop's own probe (:data:`DESKTOP_PROBES`, by :func:`desktop`) runs right after
 the environment: XFCE's window scaling (Settings > Appearance > Window Scaling) leaves
-``Xft.dpi`` at 96, so asking ``xrdb`` first would miss it.
+``Xft.dpi`` at 96, so asking ``xrdb`` first would miss it. Cinnamon's csd-xsettings
+publishes ``Xft.dpi`` as 96 x the user interface scale (System Settings > Display, kept
+in muffin's monitor configuration, not in GSettings) x the text scaling factor, while
+csd-settings-remap copies only the text scaling factor into GNOME's keys: there ``xrdb``
+is asked before GNOME's keys.
 
 The result is rounded to the nearest 25 % and clamped to 100..300; with no
 source it is 100. Every probe is a short command run through the runner
@@ -202,7 +206,7 @@ PROBES: tuple[Probe, ...] = (
     _from_xfce,
 )
 # Desktop (:func:`desktop`) -> its own probe, asked right after the environment.
-DESKTOP_PROBES: dict[str, Probe] = {"xfce": _from_xfce}
+DESKTOP_PROBES: dict[str, Probe] = {"xfce": _from_xfce, "cinnamon": _from_xrdb}
 
 
 def normalize(percent: float) -> int:
diff --git a/src/fork_linux/host_helper.py b/src/fork_linux/host_helper.py
index 9a60018..e55695b 100644
--- a/src/fork_linux/host_helper.py
+++ b/src/fork_linux/host_helper.py
@@ -55,7 +55,8 @@ verbs:
   edit PATH [LINE]                 open PATH in its default editor (waits)
 
 terminal: $FORK_LINUX_TERMINAL or [integration] terminal (a command, {{dir}} is
-replaced by the directory), else xdg-terminal-exec, $TERMINAL, then known terminals.
+replaced by the directory), else the desktop's preferred terminal (Cinnamon),
+xdg-terminal-exec, $TERMINAL, then known terminals.
 """
 
 # Placeholders filled by :func:`fill`.
@@ -93,7 +94,13 @@ DESKTOP_TERMINALS = {
     "XFCE": "xfce4-terminal",
     "MATE": "mate-terminal",
     "LXDE": "lxterminal",
+    "X-CINNAMON": "gnome-terminal",
 }
+# $XDG_CURRENT_DESKTOP entry -> the GSettings schema whose ``exec`` key names the terminal the
+# user picked in the desktop's own settings (Cinnamon: System Settings > Preferred Applications).
+# It is asked before xdg-terminal-exec, which these desktops do not configure.
+DESKTOP_TERMINAL_SCHEMAS = {"X-CINNAMON": "org.cinnamon.desktop.default-applications.terminal"}
+SETTINGS_TIMEOUT = 2.0
 # Diff/merge tool -> (diff arguments, merge arguments or None).
 DIFF_TOOLS: tuple[tuple[str, tuple[str, ...], tuple[str, ...] | None], ...] = (
     ("meld", (_LEFT, _RIGHT), ("--output={merged}", _LOCAL, _BASE, _REMOTE)),
@@ -113,6 +120,8 @@ _WINDOWS_PATH = re.compile(r"^(?:[A-Za-z]:(?:[\\/]|$)|\\\\[?.]\\)")
 # A URL scheme has at least two characters, so a drive path such as C:\x never looks like one.
 _URL = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]+):")
 _FIELD = re.compile(r"\{(\w+)\}")
+# Desktop Entry field codes (%f, %U, ...): Cinnamon's Preferred Applications stores a terminal's Exec.
+_FIELD_CODE = re.compile(r"%[fFuUdDnNickvm]")
 # A constant script: the name to look up is passed as "$1", never spliced into the code.
 _HOST_LOOKUP = ("sh", "-c", 'command -v -- "$1"', "sh")
 
@@ -223,17 +232,49 @@ class Host:
             return [words[0], *fill(_TERMINAL_ARGS[os.path.basename(words[0])], {"dir": directory})]
         return list(words)
 
+    def _desktops(self) -> list[str]:
+        """The ``$XDG_CURRENT_DESKTOP`` entries, upper-cased."""
+        return [item.strip().upper() for item in self.env.get("XDG_CURRENT_DESKTOP", "").split(":")]
+
     def _desktop_order(self) -> list[str]:
         """Known terminals, the desktop's own first."""
-        desktops = [item.strip().upper() for item in self.env.get("XDG_CURRENT_DESKTOP", "").split(":")]
-        preferred = [DESKTOP_TERMINALS[name] for name in desktops if name in DESKTOP_TERMINALS]
+        preferred = [DESKTOP_TERMINALS[name] for name in self._desktops() if name in DESKTOP_TERMINALS]
         return [*preferred, *(name for name, _args in TERMINALS if name not in preferred)]
 
+    def _desktop_terminal(self) -> list[str]:
+        """The installed terminal the desktop's own settings name ([] when they name none).
+
+        Cinnamon stores the chosen terminal's desktop-entry ``Exec`` (``ptyxis --new-window``):
+        field codes are dropped.
+        """
+        schemas = [DESKTOP_TERMINAL_SCHEMAS[name] for name in self._desktops() if name in DESKTOP_TERMINAL_SCHEMAS]
+        if not schemas or not self.which("gsettings"):
+            return []
+        argv = sandbox.host_argv(["gsettings", "get", schemas[0], "exec"], self.kind)
+        try:
+            result = self.runner.run(argv, env=self.tool_env, timeout=SETTINGS_TIMEOUT)
+        except ForkLinuxError:
+            return []
+        words = _split(result.stdout.strip().strip("'\"")) if result.ok else []
+        words = [word for word in words if not _FIELD_CODE.fullmatch(word)]
+        return words if words and self.which(words[0]) else []
+
+    def _preferred_command(self, words: list[str], directory: str) -> list[str]:
+        """The desktop's terminal opened in ``directory``: a known one gets its own directory
+        arguments (the desktop entry's options are dropped), another one runs as named, in it."""
+        name = os.path.basename(words[0])
+        if name in _TERMINAL_ARGS:
+            return [words[0], *fill(_TERMINAL_ARGS[name], {"dir": directory})]
+        return list(words)
+
     def terminal_argv(self, directory: str) -> list[str]:
         """The terminal command for ``directory``; :class:`ToolNotFound` when there is none."""
         configured = self._configured_terminal()
         if configured and configured != "auto":
             return self._command(_split(configured), directory)
+        preferred = self._desktop_terminal()
+        if preferred:
+            return self._preferred_command(preferred, directory)
         if self.which("xdg-terminal-exec"):
             return ["xdg-terminal-exec", f"--dir={directory}"]
         words = _split(self.env.get("TERMINAL", ""))
diff --git a/src/fork_linux/theme.py b/src/fork_linux/theme.py
index 34640dd..d2bf1ab 100644
--- a/src/fork_linux/theme.py
+++ b/src/fork_linux/theme.py
@@ -5,7 +5,9 @@ and asked in its own terms:
 
 * GNOME, Budgie (and unknown desktops): ``color-scheme``, then the
   ``gtk-theme`` name, of ``org.gnome.desktop.interface`` (dconf, then gsettings);
-* Cinnamon: the same keys of ``org.cinnamon.desktop.interface``, then GNOME's;
+* Cinnamon: the "Dark mode" preference of Cinnamon 6 (``color-scheme`` of
+  ``org.x.apps.portal``, what the XApp portal hands applications), then the
+  ``gtk-theme`` name of ``org.cinnamon.desktop.interface``, then GNOME's keys;
 * KDE Plasma: ``kreadconfig6``/``kreadconfig5`` ``ColorScheme`` and
   ``LookAndFeelPackage`` of ``kdeglobals``, else the ``kdeglobals`` file;
 * XFCE: ``xfconf-query`` ``xsettings`` ``/Net/ThemeName``;
@@ -33,6 +35,8 @@ DARK = "dark"
 LIGHT = "light"
 SCHEMA_GNOME = "org.gnome.desktop.interface"
 SCHEMA_CINNAMON = "org.cinnamon.desktop.interface"
+# Cinnamon 6's Themes > Settings > Dark mode (and the Light / Dark buttons of Themes).
+SCHEMA_XAPP_PORTAL = "org.x.apps.portal"
 SCHEMA_MATE = "org.mate.interface"
 GTK_SETTINGS_DIRS = ("gtk-4.0", "gtk-3.0")
 LXQT_FILES = ("lxqt.conf", "session.conf")
@@ -71,8 +75,8 @@ def _ini_values(content: str) -> dict[str, str]:
     return values
 
 
-def _gnome_family(runner: Runner, env: Mapping[str, str], schema: str) -> str | None:
-    """``color-scheme`` (GNOME 42+), then the ``gtk-theme`` name, of ``schema``."""
+def _color_scheme(runner: Runner, env: Mapping[str, str], schema: str) -> str | None:
+    """The ``color-scheme`` key of ``schema`` (dconf, then gsettings): None for ``default`` or unset."""
     dconf_dir = "/" + schema.replace(".", "/")
     for argv in (["dconf", "read", f"{dconf_dir}/color-scheme"], ["gsettings", "get", schema, "color-scheme"]):
         value = _query(runner, env, argv)
@@ -80,6 +84,15 @@ def _gnome_family(runner: Runner, env: Mapping[str, str], schema: str) -> str |
             return DARK
         if value == "prefer-light":
             return LIGHT
+    return None
+
+
+def _gnome_family(runner: Runner, env: Mapping[str, str], schema: str) -> str | None:
+    """``color-scheme`` (GNOME 42+), then the ``gtk-theme`` name, of ``schema``."""
+    result = _color_scheme(runner, env, schema)
+    if result is not None:
+        return result
+    dconf_dir = "/" + schema.replace(".", "/")
     for argv in (["dconf", "read", f"{dconf_dir}/gtk-theme"], ["gsettings", "get", schema, "gtk-theme"]):
         result = _by_name(_query(runner, env, argv))
         if result is not None:
@@ -133,7 +146,14 @@ def _lxqt(_runner: Runner, _env: Mapping[str, str], config_dir: Path) -> str | N
 
 
 def _cinnamon(runner: Runner, env: Mapping[str, str], _config_dir: Path) -> str | None:
-    """Cinnamon's own interface schema (GNOME's is the generic fallback)."""
+    """Cinnamon's dark-mode preference, then its own interface schema (GNOME's is the generic fallback).
+
+    Cinnamon 6 keeps "Dark mode" in ``org.x.apps.portal``; ``org.cinnamon.desktop.interface``
+    has no ``color-scheme`` key, so a light theme with "Prefer dark mode" is dark only there.
+    """
+    result = _color_scheme(runner, env, SCHEMA_XAPP_PORTAL)
+    if result is not None:
+        return result
     return _gnome_family(runner, env, SCHEMA_CINNAMON)
 
 
diff --git a/tests/test_display.py b/tests/test_display.py
index 61b5162..85e4321 100644
--- a/tests/test_display.py
+++ b/tests/test_display.py
@@ -148,6 +148,27 @@ def test_xfce_asks_its_own_settings_before_xrdb(outputs: dict[tuple[str, ...], s
     assert runner.argvs[0] == list(XFCE_FACTOR)
 
 
+CINNAMON_SESSION = {"XDG_CURRENT_DESKTOP": "X-Cinnamon"}
+
+
+@pytest.mark.parametrize(
+    ("outputs", "expected"),
+    [
+        # Display > User interface scale 2 with Fonts > Text scaling factor 1.25: csd-xsettings
+        # publishes 96 x 2 x 1.25; csd-settings-remap copies only the text factor to GNOME's keys.
+        ({GNOME_SCALE: "uint32 0\n", GNOME_TEXT: "1.25\n", XRDB: "Xft.dpi:\t240\n"}, 250),
+        ({GNOME_SCALE: "uint32 0\n", GNOME_TEXT: "1.0\n", XRDB: "Xft.dpi:\t192\n"}, 200),
+        # No Xft.dpi (no X resource database): GNOME's keys are still asked.
+        ({GNOME_SCALE: "uint32 0\n", GNOME_TEXT: "1.5\n", XRDB: "Xft.antialias:\t1\n"}, 150),
+    ],
+    ids=["ui-scale-2-text-1.25", "ui-scale-2", "no-xft-dpi"],
+)
+def test_cinnamon_asks_xft_dpi_before_gnome_keys(outputs: dict[tuple[str, ...], str], expected: int) -> None:
+    runner = _runner(outputs)
+    assert display.scale_percent(CINNAMON_SESSION, runner) == expected
+    assert runner.argvs[0] == list(XRDB)
+
+
 def test_xfce_probe_is_last_on_other_desktops() -> None:
     runner = _runner({XFCE_FACTOR: "2\n", XFCE: "96\n", XRDB: "Xft.dpi:\t96\n"})
     assert display.scale_percent({**BUS, "XDG_CURRENT_DESKTOP": "GNOME"}, runner) == 100
diff --git a/tests/test_host_helper.py b/tests/test_host_helper.py
index 6affc48..020bef3 100644
--- a/tests/test_host_helper.py
+++ b/tests/test_host_helper.py
@@ -53,7 +53,7 @@ def _host(
     popen: _Popen | None = None,
 ) -> tuple[hh.Host, RecordingRunner, _Popen]:
     names = {name for name, _args in hh.TERMINALS} | {name for name, _d, _m in hh.DIFF_TOOLS}
-    names |= {"xdg-terminal-exec", "gdbus", "dbus-send", "xdg-open", "my-term", "weird-term"}
+    names |= {"xdg-terminal-exec", "gdbus", "dbus-send", "xdg-open", "my-term", "weird-term", "gsettings"}
     which = {name: (f"/usr/bin/{name}" if name in installed else None) for name in names}
     runner = RecordingRunner(responses=responses or {}, which_map=which)
     spawner = popen or _Popen()
@@ -250,6 +250,71 @@ def test_terminal_detection_order() -> None:
         host.terminal_argv("/r")
 
 
+CINNAMON = {"XDG_CURRENT_DESKTOP": "X-Cinnamon"}
+CINNAMON_TERMINAL = ["gsettings", "get", "org.cinnamon.desktop.default-applications.terminal", "exec"]
+# Ubuntu Cinnamon: gnome-terminal, whose Recommends bring xdg-terminal-exec (which picks
+# org.gnome.Terminal.Preferences.desktop there: gnome-terminal --preferences, no terminal).
+UBUNTU_CINNAMON = ("gsettings", "gnome-terminal", "xdg-terminal-exec")
+
+
+@pytest.mark.parametrize(
+    ("exec_key", "installed", "expected"),
+    [
+        ("'tilix'\n", (*UBUNTU_CINNAMON, "tilix"), ["tilix", "--working-directory=/r"]),
+        ("'gnome-terminal'\n", UBUNTU_CINNAMON, ["gnome-terminal", "--working-directory=/r"]),
+        ("'x-terminal-emulator'\n", (*UBUNTU_CINNAMON, "x-terminal-emulator"), ["x-terminal-emulator"]),
+        # Preferred Applications stores the desktop entry's Exec: its options and field codes.
+        ("'ptyxis --new-window'\n", (*UBUNTU_CINNAMON, "ptyxis"), ["ptyxis", "--new-window", "-d", "/r"]),
+        ("'my-term --class=x %F'\n", (*UBUNTU_CINNAMON, "my-term"), ["my-term", "--class=x"]),
+        # Not installed, or no answer: the generic order (xdg-terminal-exec first).
+        ("'terminator'\n", UBUNTU_CINNAMON, ["xdg-terminal-exec", "--dir=/r"]),
+        ("''\n", UBUNTU_CINNAMON, ["xdg-terminal-exec", "--dir=/r"]),
+        ("'%U'\n", UBUNTU_CINNAMON, ["xdg-terminal-exec", "--dir=/r"]),
+        (Completed([], 1, "", "No such schema\n"), UBUNTU_CINNAMON, ["xdg-terminal-exec", "--dir=/r"]),
+    ],
+    ids=["tilix", "gnome-terminal", "x-terminal-emulator", "ptyxis-exec", "unknown-with-field-code", "not-installed",
+         "empty", "only-a-field-code", "no-schema"],
+)
+def test_terminal_follows_cinnamons_preferred_application(
+    exec_key: Any, installed: tuple[str, ...], expected: list[str]
+) -> None:
+    host, runner, _popen = _host(CINNAMON, installed=installed, responses={"gsettings": exec_key})
+    assert host.terminal_argv("/r") == expected
+    assert runner.argvs == [CINNAMON_TERMINAL]
+
+
+def test_terminal_cinnamon_setting_after_the_configured_one_and_without_gsettings() -> None:
+    host, runner, _popen = _host({**CINNAMON, FORK_TERMINAL_CONFIG: "konsole"}, installed=("gsettings", "tilix"))
+    assert host.terminal_argv("/r") == ["konsole", "--workdir", "/r"]
+    assert runner.argvs == []
+    host, runner, _popen = _host(CINNAMON, installed=("tilix", "gnome-terminal"))
+    assert host.terminal_argv("/r") == ["gnome-terminal", "--working-directory=/r"]
+    assert runner.argvs == []
+    host, runner, _popen = _host({"XDG_CURRENT_DESKTOP": "GNOME"}, installed=("gsettings", "gnome-terminal"))
+    assert host.terminal_argv("/r") == ["gnome-terminal", "--working-directory=/r"]
+    assert runner.argvs == []
+
+
+def test_terminal_cinnamon_setting_that_cannot_be_read() -> None:
+    def fails(argv: list[str]) -> Completed:
+        raise ForkLinuxError(f"cannot run {argv[0]}")
+
+    host, _runner, _popen = _host(CINNAMON, installed=UBUNTU_CINNAMON, responses={"gsettings": fails})
+    assert host.terminal_argv("/r") == ["xdg-terminal-exec", "--dir=/r"]
+
+
+def test_terminal_cinnamon_setting_is_read_on_the_host_inside_flatpak(
+    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
+) -> None:
+    info = tmp_path / ".flatpak-info"
+    info.write_text("[Application]\nname=x\n", encoding="utf-8")
+    monkeypatch.setattr(sandbox, "FLATPAK_INFO", info)
+    answers = ["/usr/bin/gsettings\n", "'tilix'\n", "/usr/bin/tilix\n"]
+    host, runner, _popen = _host(CINNAMON, responses={"flatpak-spawn": answers})
+    assert host.terminal_argv("/r") == ["tilix", "--working-directory=/r"]
+    assert runner.argvs[1] == [*sandbox.HOST_SPAWN, *CINNAMON_TERMINAL]
+
+
 @pytest.mark.parametrize(
     ("name", "args"),
     [
diff --git a/tests/test_theme.py b/tests/test_theme.py
index 4db2c39..59034c7 100644
--- a/tests/test_theme.py
+++ b/tests/test_theme.py
@@ -131,6 +131,28 @@ def test_cinnamon_own_schema(home: Path) -> None:
     assert theme.detect({"XDG_CURRENT_DESKTOP": "X-Cinnamon"}, runner, home) == "dark"
 
 
+XAPP_PORTAL = "org.x.apps.portal"
+DARK_MODE = _gsettings(XAPP_PORTAL, "color-scheme")
+CINNAMON_THEME = _gsettings(CINNAMON, "gtk-theme")
+
+
+@pytest.mark.parametrize(
+    ("outputs", "expected"),
+    [
+        # Themes > Settings > Dark mode "Prefer dark mode" with the light Yaru-cinnamon kept.
+        ({_dconf(XAPP_PORTAL, "color-scheme"): "'prefer-dark'\n", CINNAMON_THEME: "'Yaru-cinnamon'\n"}, "dark"),
+        # The same through gsettings, and "Prefer light mode" over a dark theme name.
+        ({DARK_MODE: "'prefer-dark'\n", CINNAMON_THEME: "'Yaru-cinnamon'\n"}, "dark"),
+        ({DARK_MODE: "'prefer-light'\n", CINNAMON_THEME: "'Mint-Y-Dark'\n"}, "light"),
+        # "Let applications decide": the theme name decides.
+        ({DARK_MODE: "'default'\n", CINNAMON_THEME: "'Yaru-cinnamon-dark'\n"}, "dark"),
+    ],
+    ids=["dconf-prefer-dark", "gsettings-prefer-dark", "prefer-light", "default-uses-theme"],
+)
+def test_cinnamon_dark_mode_preference(home: Path, outputs: dict[tuple[str, ...], str], expected: str) -> None:
+    assert theme.detect({"XDG_CURRENT_DESKTOP": "X-Cinnamon"}, _runner(outputs), home) == expected
+
+
 def test_cinnamon_falls_back_to_gnome(home: Path) -> None:
     runner = _runner({_gsettings(GNOME, "color-scheme"): "'prefer-light'\n"})
     assert theme.detect({"XDG_CURRENT_DESKTOP": "X-Cinnamon"}, runner, home) == "light"
```
