# LXQt desktop tier: fixes needed in `src/fork_linux`

Found by the LXQt cell of the desktop E2E tier (`scripts/ci-e2e-de.sh lxqt [--fork]`, Ubuntu
26.04 with Lubuntu's LXQt 2.3: lxqt-session, openbox, pcmanfm-qt 2.3, qterminal 2.3, Kvantum;
see [../DE-E2E.md](../DE-E2E.md)). The desktop tier's work does not change `src/`. The patch
below is a proposal for whoever owns `src/fork_linux`. Until it is applied, the LXQt
integration tier fails exactly these tests, with no `xfail` and no skip:

| Test (LXQt cell) | Fails with | Fixed by |
|---|---|---|
| `test_de_integration.py::test_theme_detection_follows_the_desktop[light]` | `assert 'dark' == 'light'` (Lubuntu as installed) | 1 |
| `test_de_integration.py::test_theme_detection_follows_the_desktop[light:dark-panel-theme]` | `assert 'dark' == 'light'` | 1 |
| `test_de_integration.py::test_file_manager_runs_open_in_fork` | `fork-linux added no 'Open in Fork' action for pcmanfm-qt` | 2 |
| `test_de_integration.py::test_terminal_opens_in_the_directory` | `qterminal is not the terminal that opened` (it ran as `x-terminal-emulator`) | 3 |
| `test_de_integration.py::test_menu_entry_is_valid_listed_and_starts_fork_linux` | none: passes (see the note under 2) | - |
| `test_de_fork.py::test_03_fork_runs_under_the_desktops_window_manager` (then 04-09 fail fast) | `the desktop is light`: Fork starts dark on the light Lubuntu desktop (window mean 0.18 < 0.65) | 1 |

The cases `dark:kvantum-dark-theme` and `dark:fusion-dark-palette` pass today only by chance,
for the wrong reason (problem 1): today's code answers "dark" for every Lubuntu desktop that
sets no LXQt `theme=` of its own.

With the patch applied, the checks pass. They were run on a scratch copy of this branch, with
the release tarball built from the patched tree and the same tests and images. The LXQt
integration tier passes 17/17 (`FL_E2E_DE_TARBALL=<patched tarball> scripts/ci-e2e-de.sh
--name de-lxqt-patched lxqt`). The real-Fork tier passes 9/9 (`FL_E2E_DE_TARBALL=<patched tarball> scripts/ci-e2e-de.sh --name
de-lxqt-fork-patched lxqt --fork`: setup 294 s from the seed, 351 s in all). Without the patch, it
stops at step 03. The
whole unit suite passes (3466 passed) with 100 % line and branch coverage of `theme.py`,
`host_helper.py` and `desktop_integration.py` (Python 3.14 in `fork-linux-ci-coverage:26.04`).
`test_theme.py`, `test_host_helper.py`, `test_desktop_integration.py`, `test_completions.py` and
`test_manpages.py` pass on Python 3.10 (`fork-linux-ci-jammy:22.04`). `ruff check --no-cache .`
and `scripts/no-suppressions-lint.py` are clean. Without the patch, 14 of the new or changed
unit tests fail: 9 in `test_theme.py`, 2 in `test_host_helper.py` and 3 in
`test_desktop_integration.py`.

The two other failures of that unit run, `test_docker.py::test_no_dockerfiles_outside_docker_dir`
and `test_packaging_release.py::test_source_tarball_contains_only_what_git_would_commit`, happen
because the scratch copy is not a git checkout. Both pass in the repository.

The patch applies with `git apply` to this branch and touches different hunks than the GNOME
proposal ([gnome.md](gnome.md)) in `desktop_integration.py`. The `data/` files are regenerated
with `python3 scripts/gen-data.py write`.

## 1. LXQt: Lubuntu is detected as dark, and the colour of LXQt's applications is never read

`theme._lxqt` reads only `theme=` from the user's `~/.config/lxqt/lxqt.conf` or
`session.conf`. That is LXQt's own theme, which styles only the panel, the desktop and LXQt's
dialogs. Several things checked in the container (screenshots of a pcmanfm-qt window, mean
brightness) show why that is not enough:

- **Lubuntu as installed** has no user `theme=`, because Lubuntu's defaults live in
  `/etc/xdg/xdg-Lubuntu/lxqt/lxqt.conf`, which LXQt merges in key by key. The probe falls back
  to GNOME's `org.gnome.desktop.interface gtk-theme`, which is **`Arc-Darker`** on Lubuntu
  26.04 (dark header bars, light content). So fork-linux answers *dark* and Fork starts dark on
  a light desktop (window mean brightness 0.92).
- **Lubuntu's Qt style is `kvantum`.** The applications' colours come from the Kvantum theme
  (`~/.config/Kvantum/kvantum.kvconfig` `theme=`, falling back to
  `/etc/xdg/xdg-Lubuntu/Kvantum/kvantum.kvconfig`: `Lubuntu`), and that theme's own
  `[GeneralColors] window.color` (`/usr/share/Kvantum/<theme>/<theme>.kvconfig`). Choosing
  `KvArcDark` in Kvantum Manager makes every Qt application dark (0.27). Under Kvantum, LXQt's
  widget palette changes nothing (0.92 with the Dark palette).
- **Under any other Qt style** (LXQt Appearance > Widget Style, for example Fusion), the widget
  palette `[Palette] window_color` of `lxqt.conf` colours the applications (Fusion with the
  Dark palette: 0.16). `palette_override` makes no difference to that: 0.16 both with
  `true` and with `false`.
- **LXQt's own theme `dark`** alone leaves every application light (0.92), but today's code
  answers *dark*.

Fix: the LXQt probe merges `lxqt.conf` the way LXQt does, from `$XDG_CONFIG_HOME` and then
each of `$XDG_CONFIG_DIRS`, key by key. Under the `kvantum` style it classifies the Kvantum
theme's `window.color` by its perceived brightness. The user's copy in `~/.config/Kvantum` is
read first, then `$XDG_DATA_DIRS/Kvantum`, then the theme's name; Kvantum's built-in `Default`
counts as light. Under any other style it classifies the palette's `window_color`. Failing
both, it falls back to the merged `theme=` and then today's user-file `theme=` lookup, so
`session.conf` still counts. Keys before the first `[section]` belong to `[General]`, as in
QSettings. Upstream LXQt with Fusion, no palette and a `dark` LXQt theme still answers *dark*,
as before.

## 2. "Open in Fork" is never installed for PCManFM-Qt (Lubuntu's file manager)

`desktop_integration.FILE_MANAGERS` has no PCManFM. On Lubuntu, `install.sh` and `fork-linux
desktop install` (`--file-managers auto`) find none of the file managers they know, so no
"Open in Fork" action is installed. Yet the FileManager-Actions file that fork-linux writes for
Caja (`~/.local/share/file-manager/actions/fork-linux-open.desktop`, template
`fma-action.desktop.in`, "for Caja and PCManFM") is exactly what libfm-qt reads. Placed there,
it shows "Open in Fork" in pcmanfm-qt's folder context menu at once, without a restart.
Pressing it runs `fork-linux open --from-file-manager <folder>`; the tier checks this with the
real context menu (Menu, Up, Up, Return).

Fix: a `pcmanfm` file manager (programs `pcmanfm-qt` or `pcmanfm`, menu entries
`pcmanfm-qt.desktop` or `pcmanfm.desktop`) that gets the existing FMA kind. The `--file-managers`
help, the man page and the fish completion list it (regenerated), and so do the README and the
v1.0.0 release notes.

## 3. qterminal is not a known terminal

`host_helper.TERMINALS` and `DESKTOP_TERMINALS` do not know qterminal, LXQt's terminal. On
Ubuntu, `fork-linux-host terminal` (Fork's Open In Shell) reaches it only through Debian's
`x-terminal-emulator` alternative, with no arguments and the directory as its working
directory. The tier sees that as `pids_of("qterminal") == []`, because the process is named
`x-terminal-emul`. Elsewhere it goes wrong:

- With another terminal installed (GNOME's, for example), that one wins over LXQt's own,
  because `gnome-terminal` comes before `x-terminal-emulator` in the order.
- On distributions without the alternative (Fedora, Arch, openSUSE LXQt), with no other known
  terminal installed, it fails with "no terminal emulator found".

Fix: `("qterminal", ("--workdir", "{dir}"))` in `TERMINALS`, and `"LXQT": "qterminal"` in
`DESKTOP_TERMINALS`. `qterminal --workdir DIR` was checked in the container.

## Note: the menu

LXQt's panel menu (Lubuntu: fancymenu on `lxqt-applications-compact.menu`) files
`Categories=Development` under **Programming**. That is LXQt's name for the folder, not a bug,
so the recipe's `menu_folder` is `("Programming",)`.

## The patch (unified diff against this branch)

```diff
diff --git a/README.md b/README.md
index 896c24a..97118c2 100644
--- a/README.md
+++ b/README.md
@@ -86,7 +86,7 @@ Downloads are cached in `~/.cache/fork-linux/` (winetricks' .NET and font downlo
 - `fork` — start Fork.
 - `fork .` or `fork ~/src/myrepo` — open a repository (a path inside a repository opens its root). If Fork is already running, the repository opens in a new tab.
 - **Desktop launcher** — "Fork for Linux (unofficial)" in your app menu, with Fork's own icon extracted from your copy (at every icon size; a neutral placeholder until Fork is installed).
-- **Open in Fork** — a right-click action in Nautilus, Nemo, Caja, Dolphin and Thunar. `install.sh` and `fork-linux desktop install` add it for the file managers installed on your computer (`--file-managers auto`, the default); `--file-managers all` or a list such as `thunar,nemo` picks them yourself. In Thunar the action is added next to your desktop's own custom actions ("Open Terminal Here", ...), and removing it brings back exactly what was there.
+- **Open in Fork** — a right-click action in Nautilus, Nemo, Caja, Dolphin, Thunar and PCManFM-Qt. `install.sh` and `fork-linux desktop install` add it for the file managers installed on your computer (`--file-managers auto`, the default); `--file-managers all` or a list such as `thunar,nemo` picks them yourself. In Thunar the action is added next to your desktop's own custom actions ("Open Terminal Here", ...), and removing it brings back exactly what was there.
 
 ## Updating
 
diff --git a/data/completions/fork-linux.fish b/data/completions/fork-linux.fish
index 306e75d..e8e628c 100644
--- a/data/completions/fork-linux.fish
+++ b/data/completions/fork-linux.fish
@@ -77,7 +77,7 @@ complete -c fork-linux -n '__fish_seen_subcommand_from settings; and __fish_seen
 complete -c fork-linux -n '__fish_seen_subcommand_from settings; and not __fish_seen_subcommand_from show get set unset apply-defaults backup restore path' -a path -d 'print the location of settings.json'
 complete -c fork-linux -n '__fish_use_subcommand' -a desktop -d 'add or remove the menu entry and \'Open in Fork\' actions'
 complete -c fork-linux -n '__fish_seen_subcommand_from desktop; and not __fish_seen_subcommand_from install remove status' -a install -d 'install (or refresh) the integration'
-complete -c fork-linux -n '__fish_seen_subcommand_from desktop; and __fish_seen_subcommand_from install' -l file-managers -r -d 'comma separated: nautilus, nemo, caja, dolphin, thunar, fma, auto (the installed ones), all or none (default: auto)'
+complete -c fork-linux -n '__fish_seen_subcommand_from desktop; and __fish_seen_subcommand_from install' -l file-managers -r -d 'comma separated: nautilus, nemo, caja, dolphin, thunar, pcmanfm, fma, auto (the installed ones), all or none (default: auto)'
 complete -c fork-linux -n '__fish_seen_subcommand_from desktop; and __fish_seen_subcommand_from install' -l no-menu -d 'no application menu entry'
 complete -c fork-linux -n '__fish_seen_subcommand_from desktop; and __fish_seen_subcommand_from install' -l no-icons -d 'do not install icons'
 complete -c fork-linux -n '__fish_seen_subcommand_from desktop; and __fish_seen_subcommand_from install' -l cli-alias -d 'also link fork and fork-linux into ~/.local/bin'
diff --git a/data/man/fork-linux.1 b/data/man/fork-linux.1
index d6de5af..fb0cf9a 100644
--- a/data/man/fork-linux.1
+++ b/data/man/fork-linux.1
@@ -268,7 +268,7 @@ add or remove the menu entry and 'Open in Fork' actions
 install (or refresh) the integration
 .TP
 \fB\-\-file\-managers\fR \fILIST\fR
-comma separated: nautilus, nemo, caja, dolphin, thunar, fma, auto (the installed ones), all or none (default: auto)
+comma separated: nautilus, nemo, caja, dolphin, thunar, pcmanfm, fma, auto (the installed ones), all or none (default: auto)
 .TP
 \fB\-\-no\-menu\fR
 no application menu entry
diff --git a/docs/releases/v1.0.0.md b/docs/releases/v1.0.0.md
index 1ed75ee..213f0b9 100644
--- a/docs/releases/v1.0.0.md
+++ b/docs/releases/v1.0.0.md
@@ -14,7 +14,7 @@ Report wrapper problems at https://github.com/ventura8/Fork-Linux/issues, not to
   verified by size + sha256 before use).
 - **Desktop integration**: menu entry, AppStream metainfo, our own neutral icon (Fork's icon is
   extracted from your own `Fork.exe` at runtime, at every icon size), "Open in Fork" for the
-  installed file managers among Nautilus, Nemo, Caja, Dolphin, Thunar and FileManager-Actions
+  installed file managers among Nautilus, Nemo, Caja, Dolphin, Thunar, PCManFM(-Qt) and FileManager-Actions
   (Thunar keeps your desktop's own custom actions), manual pages, bash / zsh / fish completions.
 - **Readable interface text**: Fork's interface uses Selawik, Microsoft's open-source (SIL OFL
   1.1) stand-in for Segoe UI, downloaded pinned and sha256-verified; Windows' system fonts and
diff --git a/src/fork_linux/desktop_integration.py b/src/fork_linux/desktop_integration.py
index 347e28f..9de3d0f 100644
--- a/src/fork_linux/desktop_integration.py
+++ b/src/fork_linux/desktop_integration.py
@@ -38,7 +38,7 @@ from .procrun import Runner
 
 log = logging.getLogger(__name__)
 
-FILE_MANAGERS = ("nautilus", "nemo", "caja", "dolphin", "thunar", "fma")
+FILE_MANAGERS = ("nautilus", "nemo", "caja", "dolphin", "thunar", "pcmanfm", "fma")
 LABEL = "Open in Fork"
 TOOLTIP = f"Open the repository in {APP_NAME}"
 UNIQUE_ID = "fork-linux-open"
@@ -82,6 +82,8 @@ _MANAGER_KINDS = {
     "fma": (FMA,),
     "dolphin": (DOLPHIN,),
     "thunar": (THUNAR,),
+    # PCManFM-Qt (LXQt, Lubuntu) and PCManFM (LXDE) read FileManager-Actions entries (libfm).
+    "pcmanfm": (FMA,),
 }
 _FILE_MANAGER_KINDS = frozenset(kind for kinds in _MANAGER_KINDS.values() for kind in kinds)
 # File manager -> (programs on PATH, menu entries in */applications) that show it is installed.
@@ -91,6 +93,7 @@ _MANAGER_PROBES = {
     "caja": (("caja",), ("caja.desktop", "caja-browser.desktop")),
     "dolphin": (("dolphin",), ("org.kde.dolphin.desktop", "dolphin.desktop")),
     "thunar": (("thunar", "Thunar"), ("thunar.desktop", "Thunar.desktop")),
+    "pcmanfm": (("pcmanfm-qt", "pcmanfm"), ("pcmanfm-qt.desktop", "pcmanfm.desktop")),
     "fma": (("fma-config-tool",), ("fma-config-tool.desktop",)),
 }
 _PLACEHOLDER = re.compile(r"@([A-Z][A-Z0-9_]*)@")
diff --git a/src/fork_linux/host_helper.py b/src/fork_linux/host_helper.py
index f86f449..524c342 100644
--- a/src/fork_linux/host_helper.py
+++ b/src/fork_linux/host_helper.py
@@ -78,6 +78,7 @@ TERMINALS: tuple[tuple[str, tuple[str, ...]], ...] = (
     ("terminator", _WORKING_DIRECTORY),
     ("mate-terminal", _WORKING_DIRECTORY),
     ("lxterminal", _WORKING_DIRECTORY),
+    ("qterminal", ("--workdir", _DIR)),
     ("alacritty", ("--working-directory", _DIR)),
     ("kitty", ("--directory", _DIR)),
     ("foot", _WORKING_DIRECTORY),
@@ -93,6 +94,7 @@ DESKTOP_TERMINALS = {
     "XFCE": "xfce4-terminal",
     "MATE": "mate-terminal",
     "LXDE": "lxterminal",
+    "LXQT": "qterminal",
 }
 # Diff/merge tool -> (diff arguments, merge arguments or None).
 DIFF_TOOLS: tuple[tuple[str, tuple[str, ...], tuple[str, ...] | None], ...] = (
diff --git a/src/fork_linux/theme.py b/src/fork_linux/theme.py
index 34640dd..65b46e1 100644
--- a/src/fork_linux/theme.py
+++ b/src/fork_linux/theme.py
@@ -10,7 +10,13 @@ and asked in its own terms:
   ``LookAndFeelPackage`` of ``kdeglobals``, else the ``kdeglobals`` file;
 * XFCE: ``xfconf-query`` ``xsettings`` ``/Net/ThemeName``;
 * MATE: ``org.mate.interface`` ``gtk-theme``;
-* LXQt: ``theme=`` in ``lxqt/lxqt.conf`` or ``lxqt/session.conf``.
+* LXQt: what colours its applications. Under the ``kvantum`` Qt style (Lubuntu's
+  default) that is the Kvantum theme's ``window.color``; under any other style, the
+  widget palette's ``window_color`` of ``lxqt.conf``; else the ``theme=`` entry of
+  ``lxqt/lxqt.conf`` or ``lxqt/session.conf`` (LXQt's own theme only styles the panel
+  and the desktop). LXQt reads ``lxqt.conf`` from ``$XDG_CONFIG_HOME`` over each of
+  ``$XDG_CONFIG_DIRS`` key by key (Lubuntu: ``/etc/xdg/xdg-Lubuntu``), and so does
+  this probe, so a distribution's defaults count too.
 
 When the desktop's own probe has no answer the GNOME probe is tried, then
 GTK's ``settings.ini`` (``gtk-application-prefer-dark-theme`` or a theme name
@@ -22,6 +28,7 @@ wins over everything. Unlike the original, nothing runs as another user
 from __future__ import annotations
 
 import os
+import string
 from collections.abc import Mapping
 from pathlib import Path
 
@@ -36,6 +43,17 @@ SCHEMA_CINNAMON = "org.cinnamon.desktop.interface"
 SCHEMA_MATE = "org.mate.interface"
 GTK_SETTINGS_DIRS = ("gtk-4.0", "gtk-3.0")
 LXQT_FILES = ("lxqt.conf", "session.conf")
+LXQT_CONF = Path("lxqt", "lxqt.conf")
+KVANTUM_CONF = Path("Kvantum", "kvantum.kvconfig")
+KVANTUM_STYLE = "kvantum"
+# Kvantum's built-in theme, used when no kvantum.kvconfig names one (light).
+KVANTUM_DEFAULT = "Default"
+DEFAULT_CONFIG_DIRS = "/etc/xdg"
+DEFAULT_DATA_DIRS = "/usr/local/share:/usr/share"
+# QSettings files: keys before the first [section] belong to [General].
+INI_GENERAL = "General"
+# Perceived brightness (0..255, ITU-R BT.601 weights) below which a colour is dark.
+DARK_BELOW = 128
 
 _GNOME_LIKE = ("gnome", "budgie", "unknown")
 _TRUE = ("1", "true", "yes")
@@ -71,6 +89,44 @@ def _ini_values(content: str) -> dict[str, str]:
     return values
 
 
+def _ini_section(content: str, section: str) -> dict[str, str]:
+    """``key=value`` lines of one ``[section]`` (QSettings: keys before any section are ``[General]``)."""
+    values: dict[str, str] = {}
+    current = INI_GENERAL
+    for line in content.splitlines():
+        text = line.strip()
+        if text.startswith("[") and text.endswith("]"):
+            current = text[1:-1]
+            continue
+        key, sep, value = text.partition("=")
+        if sep and current == section:
+            values.setdefault(key.strip().lower(), value.strip().strip("'\""))
+    return values
+
+
+def _search_dirs(env: Mapping[str, str], name: str, default: str) -> list[Path]:
+    """The absolute directories of the ``name`` search path (``default`` when it is unset or empty)."""
+    return [Path(entry) for entry in (env.get(name) or default).split(":") if os.path.isabs(entry)]
+
+
+def _setting(dirs: list[Path], relative: Path, section: str, key: str) -> str:
+    """The first non-empty ``key`` of ``[section]`` in ``dir/relative`` over ``dirs`` (as QSettings merges them)."""
+    for base in dirs:
+        value = _ini_section(_read(base / relative), section).get(key, "")
+        if value:
+            return value
+    return ""
+
+
+def _by_color(color: str) -> str | None:
+    """Dark or light for a ``#rrggbb`` colour by its perceived brightness, None for anything else."""
+    digits = color.strip().removeprefix("#")
+    if len(digits) != 6 or not set(digits) <= set(string.hexdigits):
+        return None
+    red, green, blue = (int(digits[index : index + 2], 16) for index in (0, 2, 4))
+    return DARK if (299 * red + 587 * green + 114 * blue) / 1000 < DARK_BELOW else LIGHT
+
+
 def _gnome_family(runner: Runner, env: Mapping[str, str], schema: str) -> str | None:
     """``color-scheme`` (GNOME 42+), then the ``gtk-theme`` name, of ``schema``."""
     dconf_dir = "/" + schema.replace(".", "/")
@@ -120,8 +176,19 @@ def _mate(runner: Runner, env: Mapping[str, str], _config_dir: Path) -> str | No
     return _by_name(_query(runner, env, ["gsettings", "get", SCHEMA_MATE, "gtk-theme"]))
 
 
-def _lxqt(_runner: Runner, _env: Mapping[str, str], config_dir: Path) -> str | None:
-    """The ``theme=`` entry of LXQt's configuration."""
+def _kvantum(env: Mapping[str, str], config_dirs: list[Path]) -> str:
+    """The Kvantum theme's window colour: the user's copy in ``Kvantum/``, else the installed one."""
+    name = _setting(config_dirs, KVANTUM_CONF, INI_GENERAL, "theme") or KVANTUM_DEFAULT
+    theme_file = Path("Kvantum", name, f"{name}.kvconfig")
+    for base in (config_dirs[0], *_search_dirs(env, "XDG_DATA_DIRS", DEFAULT_DATA_DIRS)):
+        result = _by_color(_ini_section(_read(base / theme_file), "GeneralColors").get("window.color", ""))
+        if result is not None:
+            return result
+    return DARK if DARK in name.lower() else LIGHT
+
+
+def _lxqt_theme_entry(config_dir: Path) -> str | None:
+    """The ``theme=`` entry of the user's LXQt configuration (``lxqt.conf``, then ``session.conf``)."""
     for name in LXQT_FILES:
         for line in _read(config_dir / "lxqt" / name).splitlines():
             key, _sep, value = line.partition("=")
@@ -132,6 +199,17 @@ def _lxqt(_runner: Runner, _env: Mapping[str, str], config_dir: Path) -> str | N
     return None
 
 
+def _lxqt(_runner: Runner, env: Mapping[str, str], config_dir: Path) -> str | None:
+    """What colours LXQt's applications: the Kvantum theme, the widget palette, else LXQt's theme."""
+    config_dirs = [config_dir, *_search_dirs(env, "XDG_CONFIG_DIRS", DEFAULT_CONFIG_DIRS)]
+    if _setting(config_dirs, LXQT_CONF, "Qt", "style").lower() == KVANTUM_STYLE:
+        return _kvantum(env, config_dirs)
+    result = _by_color(_setting(config_dirs, LXQT_CONF, "Palette", "window_color"))
+    if result is None:
+        result = _by_name(_setting(config_dirs, LXQT_CONF, INI_GENERAL, "theme"))
+    return result if result is not None else _lxqt_theme_entry(config_dir)
+
+
 def _cinnamon(runner: Runner, env: Mapping[str, str], _config_dir: Path) -> str | None:
     """Cinnamon's own interface schema (GNOME's is the generic fallback)."""
     return _gnome_family(runner, env, SCHEMA_CINNAMON)
diff --git a/tests/test_desktop_integration.py b/tests/test_desktop_integration.py
index 9df052f..658f1e1 100644
--- a/tests/test_desktop_integration.py
+++ b/tests/test_desktop_integration.py
@@ -952,11 +952,26 @@ def test_installed_file_managers(env: dict[str, str], tmp_path: Path) -> None:
     (user_apps / "caja-browser.desktop").write_text("[Desktop Entry]\n", encoding="utf-8")
     (user_apps / "nautilus.desktop").mkdir()
     assert di.installed_file_managers(env) == ["nemo", "caja", "dolphin", "thunar"]
+    (system_apps / "pcmanfm-qt.desktop").write_text("[Desktop Entry]\n", encoding="utf-8")
+    assert di.installed_file_managers(env) == ["nemo", "caja", "dolphin", "thunar", "pcmanfm"]
     _program(bindir, "fma-config-tool")
     _program(bindir, "nautilus")
     assert di.installed_file_managers(env) == list(di.FILE_MANAGERS)
 
 
+@pytest.mark.parametrize("program", ["pcmanfm-qt", "pcmanfm"])
+def test_pcmanfm_gets_the_filemanager_actions_entry(
+    paths: Paths, env: dict[str, str], runner: RecordingRunner, tmp_path: Path, program: str
+) -> None:
+    """Lubuntu's PCManFM-Qt (and LXDE's PCManFM) show 'Open in Fork' from libfm's FileManager-Actions."""
+    _program(tmp_path / "empty-path", program)
+    assert di.installed_file_managers(env) == ["pcmanfm"]
+    installed = di.install(paths, env, runner=runner, exec_cmd=[LAUNCHER], menu=False, icons=False)
+    action = _dirs(env).kind_path(di.FMA)
+    assert installed == [action]
+    assert f"Exec={LAUNCHER} open --from-file-manager %f" in action.read_text(encoding="utf-8")
+
+
 def test_installed_file_managers_defaults_to_os_environ(
     env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
 ) -> None:
diff --git a/tests/test_host_helper.py b/tests/test_host_helper.py
index 7608017..cb0a998 100644
--- a/tests/test_host_helper.py
+++ b/tests/test_host_helper.py
@@ -245,6 +245,11 @@ def test_terminal_detection_order() -> None:
     assert host.terminal_argv("/r") == ["konsole", "--workdir", "/r"]
     host, _runner, _popen = _host(installed=("x-terminal-emulator",))
     assert host.terminal_argv("/r") == ["x-terminal-emulator"]
+    # Lubuntu: qterminal by name (x-terminal-emulator may point elsewhere), before GNOME's terminal.
+    host, _runner, _popen = _host(
+        {"XDG_CURRENT_DESKTOP": "LXQt"}, installed=("gnome-terminal", "x-terminal-emulator", "qterminal")
+    )
+    assert host.terminal_argv("/r") == ["qterminal", "--workdir", "/r"]
     host, _runner, _popen = _host()
     with pytest.raises(hh.ToolNotFound, match="no terminal emulator found"):
         host.terminal_argv("/r")
@@ -259,6 +264,7 @@ def test_terminal_detection_order() -> None:
         ("wezterm", ["start", "--cwd", "/r"]),
         ("ghostty", ["--working-directory=/r"]),
         ("foot", ["--working-directory=/r"]),
+        ("qterminal", ["--workdir", "/r"]),
         ("xterm", []),
     ],
 )
diff --git a/tests/test_theme.py b/tests/test_theme.py
index 4db2c39..64eb100 100644
--- a/tests/test_theme.py
+++ b/tests/test_theme.py
@@ -187,19 +187,100 @@ def test_mate(home: Path) -> None:
     assert theme.detect({"XDG_CURRENT_DESKTOP": "MATE"}, runner, home) == "dark"
 
 
+def _lxqt_env(home: Path) -> dict[str, str]:
+    """An LXQt session whose system directories are under ``home`` (never the host's /etc/xdg)."""
+    return {
+        "XDG_CURRENT_DESKTOP": "LXQt",
+        "XDG_CONFIG_DIRS": f"relative/xdg:{home / 'xdg-Lubuntu'}:{home / 'xdg'}",
+        "XDG_DATA_DIRS": str(home / "share"),
+    }
+
+
+def _write_under(home: Path, relative: str, text: str) -> None:
+    path = home / relative
+    path.parent.mkdir(parents=True, exist_ok=True)
+    path.write_text(text, encoding="utf-8")
+
+
+def _lubuntu(home: Path) -> None:
+    """Lubuntu 26.04's defaults: the kvantum Qt style with its light "Lubuntu" Kvantum theme."""
+    _write_under(home, "xdg-Lubuntu/lxqt/lxqt.conf", "[General]\ntheme=Lubuntu Arc\n\n[Qt]\nstyle=kvantum\n")
+    _write_under(home, "xdg-Lubuntu/Kvantum/kvantum.kvconfig", "[General]\ntheme=Lubuntu\n")
+    lubuntu = "[%General]\nx=1\n[GeneralColors]\nwindow.color=#f5f6f7\n"
+    _write_under(home, "share/Kvantum/Lubuntu/Lubuntu.kvconfig", lubuntu)
+    _write_under(home, "share/Kvantum/KvArcDark/KvArcDark.kvconfig", "[GeneralColors]\nwindow.color=#383c4a\n")
+
+
 def test_lxqt_conf(home: Path) -> None:
     _write(home, "lxqt/lxqt.conf", "[General]\nicon_theme=x\ntheme=\ntheme=kvantum-dark\n")
-    assert theme.detect({"XDG_CURRENT_DESKTOP": "LXQt"}, _runner({}), home) == "dark"
+    assert theme.detect(_lxqt_env(home), _runner({}), home) == "dark"
 
 
 def test_lxqt_session_conf(home: Path) -> None:
     _write(home, "lxqt/lxqt.conf", "[General]\nicon_theme=x\n")
     _write(home, "lxqt/session.conf", "theme = frost\n")
-    assert theme.detect({"XDG_CURRENT_DESKTOP": "LXQt"}, _runner({}), home) == "light"
+    assert theme.detect(_lxqt_env(home), _runner({}), home) == "light"
 
 
 def test_lxqt_without_config(home: Path) -> None:
+    assert theme.detect(_lxqt_env(home), _runner({}), home) is None
+
+
+# GNOME's gtk-theme on Lubuntu 26.04 names a theme with dark header bars only.
+LUBUNTU_GSETTINGS = {_gsettings(GNOME, "gtk-theme"): "'Arc-Darker'\n"}
+
+
+@pytest.mark.parametrize(
+    ("files", "expected"),
+    [
+        # Lubuntu as installed: light, whatever GNOME's gtk-theme (Arc-Darker) says.
+        ({}, "light"),
+        # Kvantum Manager: a dark theme, the installed one or the user's own copy of it.
+        ({"Kvantum/kvantum.kvconfig": "[General]\ntheme=KvArcDark\n"}, "dark"),
+        (
+            {
+                "Kvantum/kvantum.kvconfig": "theme=KvArcDark#\n",
+                "Kvantum/KvArcDark#/KvArcDark#.kvconfig": "[GeneralColors]\nwindow.color=#fafafa\n",
+            },
+            "light",
+        ),
+        # A Kvantum theme without a readable window colour: its name decides.
+        ({"Kvantum/kvantum.kvconfig": "[General]\ntheme=KvGnomeDark\n"}, "dark"),
+        ({"Kvantum/kvantum.kvconfig": "[General]\ntheme=KvFlat\n"}, "light"),
+        # Only the panel's LXQt theme is dark: the applications stay light.
+        ({"lxqt/lxqt.conf": "[General]\ntheme=dark\n"}, "light"),
+        # LXQt Appearance > Widget Style: Fusion with the Dark (or Light) palette.
+        ({"lxqt/lxqt.conf": "[Qt]\nstyle=Fusion\n[Palette]\nwindow_color=#232323\n"}, "dark"),
+        ({"lxqt/lxqt.conf": "[Qt]\nstyle=Fusion\n[Palette]\nwindow_color=#efefef\n"}, "light"),
+        # Fusion with a palette that is not #rrggbb: LXQt's theme= decides, as before.
+        ({"lxqt/lxqt.conf": "[General]\ntheme=dark\n[Qt]\nstyle=Fusion\n[Palette]\nwindow_color=#123\n"}, "dark"),
+        ({"lxqt/lxqt.conf": "[Qt]\nstyle=Fusion\n[Palette]\nwindow_color=#gggggg\n"}, "light"),
+    ],
+)
+def test_lxqt_applications_colours(home: Path, files: dict[str, str], expected: str) -> None:
+    _lubuntu(home)
+    for relative, text in files.items():
+        _write(home, relative, text)
+    runner = _runner(LUBUNTU_GSETTINGS)
+    assert theme.detect(_lxqt_env(home), runner, home) == expected
+
+
+def test_lxqt_kvantum_without_any_config_is_kvantums_default(home: Path) -> None:
+    _write(home, "lxqt/lxqt.conf", "[Qt]\nstyle=kvantum\n")
+    assert theme.detect(_lxqt_env(home), _runner(LUBUNTU_GSETTINGS), home) == "light"
+
+
+def test_lxqt_system_dirs_default_to_etc_xdg(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
+    seen: list[Path] = []
+    monkeypatch.setattr(theme, "_read", lambda path: seen.append(path) or "")
     assert theme.detect({"XDG_CURRENT_DESKTOP": "LXQt"}, _runner({}), home) is None
+    assert Path("/etc/xdg/lxqt/lxqt.conf") in seen
+
+
+def test_ini_section_keys_before_any_section_are_general() -> None:
+    content = "theme=a\n[Qt]\nstyle=x\n[General]\ntheme=b\nicon='i'\n"
+    assert theme._ini_section(content, "General") == {"theme": "a", "icon": "i"}
+    assert theme._ini_section(content, "Qt") == {"style": "x"}
 
 
 @pytest.mark.parametrize(
```
