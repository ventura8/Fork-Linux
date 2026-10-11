# MATE desktop tier: fixes needed in `src/fork_linux`

Found by the MATE cell of the desktop E2E tier (`scripts/ci-e2e-de.sh mate [--fork]`, Ubuntu
26.04: marco 1.26.2, mate-settings-daemon 1.26.1, Caja 1.26.4 with caja-actions 1.27.0,
mate-terminal 1.26.1, mate-themes 3.22.26, ubuntu-mate-themes 24.10; see
[../DE-E2E.md](../DE-E2E.md)). `src/` is not changed by the desktop tier's work: the patch below
is a proposal for whoever owns `src/fork_linux`. Until it is applied, the MATE integration tier
fails exactly these tests (no `xfail`, no skip):

| Test (MATE cell) | Fails with | Fixed by |
|---|---|---|
| `test_de_integration.py::test_theme_detection_follows_the_desktop[dark:blackmate]` | `assert 'light' == 'dark'` | 1 |
| `test_de_integration.py::test_theme_detection_follows_the_desktop[dark:highcontrastinverse]` | `assert 'light' == 'dark'` | 1 |

The real-Fork tier is not affected: its dark step uses Ubuntu MATE's `Yaru-MATE-dark`, which
says "dark" in its name.

With the patch applied (checked on a scratch copy of this branch: the release tarball built from
the patched tree with `docker/Dockerfile.release`, installed by the same tier through
`FL_E2E_DE_TARBALL`), all five MATE theme cases pass (`-k theme`: 5 passed); the unit tests
below pass (`tests/test_theme.py`: 51 passed) with 100 % line and branch coverage of `theme.py`
(`fork-linux-ci-coverage:26.04`), and `ruff check` is clean (`fork-linux-ci-lint:26.04`). Without
the source change the new unit test fails 2 of its 5 cases (BlackMATE, HighContrastInverse).

## 1. MATE's stock dark themes are read as light

`theme._by_name` calls a theme dark only when its name contains "dark", and MATE has no
`color-scheme` key (`org.mate.interface` has only `gtk-theme`), so `theme.detect` answers
`light` for MATE's own dark theme **BlackMATE** ("A clean dark theme", mate-themes) and for
**HighContrastInverse** ("White-on-black text and icons", which mate-themes ships and MATE's
Appearance dialog lists). Fork then starts light (`Theme=0`) on a dark MATE desktop. Fix: a
small set of known dark theme names that do not say so (`DARK_THEMES`), compared
case-insensitively in `_by_name`, so every probe that classifies a name (MATE, XFCE, GNOME's
`gtk-theme`, GTK's `settings.ini`, KDE) knows them. The other mate-themes themes were checked
against their `index.theme` comments: Blue-Submarine and Green-Submarine have dark menus and
panels but light windows; the rest are light. `ContrastHigh` is a metatheme of the light
`HighContrast`.

```diff
diff --git a/src/fork_linux/theme.py b/src/fork_linux/theme.py
--- a/src/fork_linux/theme.py
+++ b/src/fork_linux/theme.py
@@ -14,7 +14,9 @@ and asked in its own terms:
 
 When the desktop's own probe has no answer the GNOME probe is tried, then
 GTK's ``settings.ini`` (``gtk-application-prefer-dark-theme`` or a theme name
-containing "dark"). A ``GTK_THEME`` containing ``dark`` (``Adwaita:dark``)
+containing "dark"). A theme name is dark when it contains "dark" or is one of
+:data:`DARK_THEMES` (dark themes whose names do not say so, such as MATE's
+stock ``BlackMATE``). A ``GTK_THEME`` containing ``dark`` (``Adwaita:dark``)
 wins over everything. Unlike the original, nothing runs as another user
 (fork-linux never runs as root), and ``None`` means "no idea".
 """
@@ -36,6 +38,9 @@ SCHEMA_CINNAMON = "org.cinnamon.desktop.interface"
 SCHEMA_MATE = "org.mate.interface"
 GTK_SETTINGS_DIRS = ("gtk-4.0", "gtk-3.0")
 LXQT_FILES = ("lxqt.conf", "session.conf")
+# Dark themes without "dark" in their names (lowercased): MATE's stock dark theme and
+# the inverse high-contrast theme MATE ships (mate-themes).
+DARK_THEMES = frozenset({"blackmate", "highcontrastinverse"})
 
 _GNOME_LIKE = ("gnome", "budgie", "unknown")
 _TRUE = ("1", "true", "yes")
@@ -47,10 +52,11 @@ def _query(runner: Runner, env: Mapping[str, str], argv: list[str]) -> str:
 
 
 def _by_name(name: str) -> str | None:
-    """Dark if a theme/scheme name says so, light for any other name, None for no name."""
+    """Dark if a theme/scheme name says so or is a known dark theme, light for any other name, None for no name."""
     if not name:
         return None
-    return DARK if DARK in name.lower() else LIGHT
+    lowered = name.lower()
+    return DARK if DARK in lowered or lowered in DARK_THEMES else LIGHT
 
 
 def _read(path: Path) -> str:
diff --git a/tests/test_theme.py b/tests/test_theme.py
--- a/tests/test_theme.py
+++ b/tests/test_theme.py
@@ -187,6 +187,22 @@ def test_mate(home: Path) -> None:
     assert theme.detect({"XDG_CURRENT_DESKTOP": "MATE"}, runner, home) == "dark"
 
 
+@pytest.mark.parametrize(
+    ("name", "expected"),
+    [
+        ("'Menta'", "light"),
+        ("'BlackMATE'", "dark"),
+        ("'HighContrastInverse'", "dark"),
+        ("'HighContrast'", "light"),
+        ("'Yaru-MATE-dark'", "dark"),
+    ],
+)
+def test_mate_stock_themes(home: Path, name: str, expected: str) -> None:
+    """MATE's stock dark themes (mate-themes) do not say "dark" in their names."""
+    runner = _runner({_gsettings("org.mate.interface", "gtk-theme"): name + "\n"})
+    assert theme.detect({"XDG_CURRENT_DESKTOP": "MATE"}, runner, home) == expected
+
+
 def test_lxqt_conf(home: Path) -> None:
     _write(home, "lxqt/lxqt.conf", "[General]\nicon_theme=x\ntheme=\ntheme=kvantum-dark\n")
     assert theme.detect({"XDG_CURRENT_DESKTOP": "LXQt"}, _runner({}), home) == "dark"
```

The patch applies to `fix/de-e2e` as of this proposal. `docs/qa/de-fixes/lxqt.md` also changes
`theme.py` (the LXQt probe and the module docstring's LXQt line): the hunks touch different
lines, but the first hunk's context may need a fuzzy apply after LXQt's patch.

## Not a fork-linux bug (MATE facts the tier relies on)

- Scale needs no MATE probe: mate-settings-daemon publishes `Xft.dpi` = font DPI (Appearance >
  Fonts > Details > Resolution, `org.mate.font-rendering dpi`, 0 = the X server's) x window
  scaling (`org.mate.interface window-scaling-factor`), so `xrdb` answers 192 for window
  scaling 2 and 144 / 192 for those font DPIs, and `display.scale_percent` gives 200 / 150 /
  200 % (checked in the tier).
- A window-scaling change makes mate-settings-daemon run `marco --replace`, and the replaced
  marco never exits (marco 1.26.2: `g_main_loop_is_running: assertion 'loop != NULL' failed`).
  A marco bug, worked around in the tier's recipe (`_mate_window_scaling` ends the replaced
  marco). Separately, marco now and then held an X server grab for a minute or more while it
  waited for gdk-pixbuf's glycin image loaders; the tier's X queries wait up to 300 s. See
  [../DE-E2E.md](../DE-E2E.md).
- Caja runs the FileManager-Actions entry through caja-actions' "Caja-Actions actions" submenu
  and picks up a new action without a restart (caja-actions watches the actions directories):
  no `caja -q` notice is needed, unlike Thunar and Nautilus.
