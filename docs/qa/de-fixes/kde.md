# KDE Plasma desktop tier: fixes needed in `src/fork_linux`

Found by the KDE cell of the desktop E2E tier (`scripts/ci-e2e-de.sh kde [--fork]`, Kubuntu 26.04:
Plasma 6.6 on X11, KF 6.24, Dolphin 25.12; see [../DE-E2E.md](../DE-E2E.md)). `src/` is not
changed by the desktop tier's work: the patch below is a proposal for whoever owns
`src/fork_linux`. Until it is applied, the KDE integration tier fails exactly this test (no
`xfail`, no skip):

| Test (KDE cell) | Fails with | Fixed by |
|---|---|---|
| `test_de_integration.py::test_theme_detection_follows_the_desktop[dark:custom-colors]` | `assert 'light' == 'dark'` | 1 |

Everything else in the KDE cell passes without a change to `src/`: integration 17/18, real Fork
9/9. With the patch applied (checked on a scratch copy of this branch: the release tarball built
from the patched tree, the same image and tests) the integration tier passes 18/18.

## 1. A dark colour scheme whose name does not say "dark" is detected as light

`theme._kde` decides from names only: `ColorScheme` (`kreadconfig6 --file kdeglobals --group
General --key ColorScheme`), then `LookAndFeelPackage`, each "dark" when the name contains
"dark". Plasma's stock schemes follow that (BreezeLight, BreezeDark, BreezeClassic), but many
popular schemes do not (Nordic, Dracula, Sweet, Catppuccin Mocha, a user's own copy): with one of
them fork-linux tells Fork the desktop is light and Fork starts light on a dark desktop.

Plasma itself decides from the colours: xdg-desktop-portal-kde reports `color-scheme`
`prefer-dark` when the grey (Qt's `qGray`, (11 R + 16 G + 5 B) / 32) of the colour scheme's
window background, kdeglobals `[Colors:Window] BackgroundNormal`, is below 192. Fix: ask that
first (through `kreadconfig6` / `kreadconfig5`, so the kdeglobals cascade with
`~/.config/kdedefaults` counts, as for the names), then the names as today, then the kdeglobals
file. A session where no scheme was ever applied has no `[Colors:Window]` anywhere and keeps the
name-based answer. The line was measured in the image: with schemes whose window background is
`191,191,191` / `192,192,192` applied by `plasma-apply-colorscheme`, the running
xdg-desktop-portal-kde answers `org.freedesktop.appearance` `color-scheme` 1 (prefer dark) / 2
(prefer light); Breeze Dark (`32,35,38`) 1, Breeze Light (`239,240,241`) 2. (Asking the portal
itself over D-Bus would give the same answer but needs a running portal; kdeglobals is there in
every Plasma session and for `kreadconfig5` on Plasma 5.)

Evidence (the KDE image, a real Plasma session from `tests/e2e_de/desktops.py`'s recipe; a copy
of `/usr/share/color-schemes/BreezeDark.colors` saved as
`~/.local/share/color-schemes/Nightfall.colors` with `Name=Nightfall`, applied with
`plasma-apply-colorscheme Nightfall`, as System Settings > Colors does for a downloaded scheme):

| Desktop state | kdeglobals | `theme.detect` today | with the patch |
|---|---|---|---|
| Kubuntu default | `LookAndFeelPackage=org.kubuntu.desktop` | light | light |
| Colors: Nightfall | `ColorScheme=Nightfall`, `BackgroundNormal=32,35,38` | **light** | dark |
| Global Theme Kubuntu Dark | kdedefaults `ColorScheme=BreezeDark` | dark | dark |
| Kubuntu Dark + Colors: Breeze Light | `ColorScheme=BreezeLight` | light | light |
| Kubuntu + Colors: Breeze Dark | `ColorScheme=BreezeDark` | dark | dark |

Unit tests, on the same scratch copy: the tests below pass with 100 % line and branch
coverage of `theme.py` (Python 3.14 in `fork-linux-ci-coverage:26.04`, Python 3.10 in
`fork-linux-ci-jammy:22.04`), the whole unit suite passes (3461 passed), `ruff check` is clean;
without the `theme.py` part, 6 of the new tests fail.

```diff
--- a/src/fork_linux/theme.py
+++ b/src/fork_linux/theme.py
@@ -6,8 +6,9 @@
 * GNOME, Budgie (and unknown desktops): ``color-scheme``, then the
   ``gtk-theme`` name, of ``org.gnome.desktop.interface`` (dconf, then gsettings);
 * Cinnamon: the same keys of ``org.cinnamon.desktop.interface``, then GNOME's;
-* KDE Plasma: ``kreadconfig6``/``kreadconfig5`` ``ColorScheme`` and
-  ``LookAndFeelPackage`` of ``kdeglobals``, else the ``kdeglobals`` file;
+* KDE Plasma: ``kreadconfig6``/``kreadconfig5``: the window background colour of
+  ``kdeglobals`` (grey below 192 is dark, as Plasma's portal says), then the
+  ``ColorScheme`` and ``LookAndFeelPackage`` names, else the ``kdeglobals`` file;
 * XFCE: ``xfconf-query`` ``xsettings`` ``/Net/ThemeName``;
 * MATE: ``org.mate.interface`` ``gtk-theme``;
 * LXQt: ``theme=`` in ``lxqt/lxqt.conf`` or ``lxqt/session.conf``.
@@ -22,6 +23,7 @@
 from __future__ import annotations
 
 import os
+import re
 from collections.abc import Mapping
 from pathlib import Path
 
@@ -36,6 +38,9 @@
 SCHEMA_MATE = "org.mate.interface"
 GTK_SETTINGS_DIRS = ("gtk-4.0", "gtk-3.0")
 LXQT_FILES = ("lxqt.conf", "session.conf")
+# Plasma's line between dark and light window backgrounds (grey 0-255, xdg-desktop-portal-kde).
+KDE_DARK_GRAY = 192
+_KDE_RGB = re.compile(r"^(\d{1,3}),(\d{1,3}),(\d{1,3})(?:,\d{1,3})?$", re.ASCII)
 
 _GNOME_LIKE = ("gnome", "budgie", "unknown")
 _TRUE = ("1", "true", "yes")
@@ -96,14 +101,41 @@
     return False
 
 
+def _by_window_background(value: str) -> str | None:
+    """Dark or light from a kdeglobals colour ``R,G,B[,A]`` as Plasma decides it, None for no colour.
+
+    xdg-desktop-portal-kde reports ``prefer-dark`` when the window background's grey (Qt's
+    ``qGray``: (11 R + 16 G + 5 B) / 32) is below :data:`KDE_DARK_GRAY`.
+    """
+    match = _KDE_RGB.match(value)
+    if match is None:
+        return None
+    red, green, blue = (int(part) for part in match.groups())
+    return DARK if (red * 11 + green * 16 + blue * 5) // 32 < KDE_DARK_GRAY else LIGHT
+
+
+def _kreadconfig(runner: Runner, env: Mapping[str, str], group: str, key: str) -> str:
+    """``key`` of ``group`` in the kdeglobals cascade (kreadconfig6, then kreadconfig5), or ""."""
+    for tool in ("kreadconfig6", "kreadconfig5"):
+        value = _query(runner, env, [tool, "--file", "kdeglobals", "--group", group, "--key", key])
+        if value:
+            return value
+    return ""
+
+
 def _kde(runner: Runner, env: Mapping[str, str], config_dir: Path) -> str | None:
-    """Plasma: kreadconfig6/5 ``ColorScheme`` / ``LookAndFeelPackage``, else ``kdeglobals``."""
-    for key, group in (("ColorScheme", "General"), ("LookAndFeelPackage", "KDE")):
-        for tool in ("kreadconfig6", "kreadconfig5"):
-            argv = [tool, "--file", "kdeglobals", "--group", group, "--key", key]
-            result = _by_name(_query(runner, env, argv))
-            if result is not None:
-                return result
+    """Plasma: the colour scheme's window background (kreadconfig6/5), then the ``ColorScheme`` /
+    ``LookAndFeelPackage`` names, else the ``kdeglobals`` file.
+
+    The colours come first: a scheme named without "dark" (Nordic, Dracula, a user's own) is
+    dark when Plasma says so.
+    """
+    result = _by_window_background(_kreadconfig(runner, env, "Colors:Window", "BackgroundNormal"))
+    for group, key in (("General", "ColorScheme"), ("KDE", "LookAndFeelPackage")):
+        if result is None:
+            result = _by_name(_kreadconfig(runner, env, group, key))
+    if result is not None:
+        return result
     content = _read(config_dir / "kdeglobals")
     if _kdeglobals_names_dark(content):
         return DARK
--- a/tests/test_theme.py
+++ b/tests/test_theme.py
@@ -149,6 +149,47 @@
     assert theme.detect({"XDG_CURRENT_DESKTOP": "KDE"}, runner, home) == "light"
 
 
+WINDOW_BACKGROUND = ("Colors:Window", "BackgroundNormal")
+
+
+@pytest.mark.parametrize(
+    ("colour", "expected"),
+    [
+        ("35,38,41", "dark"),  # a dark scheme whose name does not say so (Nordic, Dracula, ...)
+        ("32,35,38,255", "dark"),  # with alpha
+        ("191,191,191", "dark"),  # grey 191: below Plasma's line
+        ("192,192,192", "light"),  # grey 192: on it
+        ("239,240,241", "light"),
+    ],
+)
+def test_kde_window_background_wins_over_the_names(home: Path, colour: str, expected: str) -> None:
+    runner = _runner(
+        {
+            _kread("kreadconfig6", *WINDOW_BACKGROUND): colour + "\n",
+            _kread("kreadconfig6", "General", "ColorScheme"): "Nordic\n",
+            _kread("kreadconfig6", "KDE", "LookAndFeelPackage"): "org.kde.breezedark.desktop\n",
+        }
+    )
+    assert theme.detect({"XDG_CURRENT_DESKTOP": "KDE"}, runner, home) == expected
+    assert runner.argvs == [list(_kread("kreadconfig6", *WINDOW_BACKGROUND))]
+
+
+def test_kde_window_background_from_kreadconfig5(home: Path) -> None:
+    runner = _runner({_kread("kreadconfig5", *WINDOW_BACKGROUND): "42,46,50\n"}, installed=("kreadconfig5",))
+    assert theme.detect({"XDG_CURRENT_DESKTOP": "KDE"}, runner, home) == "dark"
+
+
+@pytest.mark.parametrize("value", ["#232629", "35,38", "256x,1,1", "1,2,3,4,5"])
+def test_kde_a_window_background_that_is_no_colour_falls_back_to_the_names(home: Path, value: str) -> None:
+    runner = _runner(
+        {
+            _kread("kreadconfig6", *WINDOW_BACKGROUND): value + "\n",
+            _kread("kreadconfig6", "General", "ColorScheme"): "BreezeDark\n",
+        }
+    )
+    assert theme.detect({"XDG_CURRENT_DESKTOP": "KDE"}, runner, home) == "dark"
+
+
 def test_kde_kdeglobals_file(home: Path) -> None:
     _write(home, "kdeglobals", "[General]\nName=x\n\n[KDE]\nLookAndFeelPackage=org.kde.breezedark.desktop\n")
     assert theme.detect({"XDG_CURRENT_DESKTOP": "KDE"}, _runner({}, installed=()), home) == "dark"
```
