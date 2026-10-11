# Budgie desktop tier: fixes needed in `src/fork_linux`

Found by the Budgie cell of the desktop E2E tier (`scripts/ci-e2e-de.sh budgie [--fork]`, Ubuntu
Budgie 26.04: Budgie 10.10.2 on labwc 0.9.3 with Xwayland, budgie-daemon, Nemo 6.4.5,
xfce4-terminal 1.1.5, budgie-desktop-environment 0.24.16; see [../DE-E2E.md](../DE-E2E.md)).
`src/` (and `libexec/`, `data/`) are not changed by the desktop tier's work: the patches below
are proposals for whoever owns them. Until they are applied, the Budgie tiers fail exactly these
tests (no `xfail`, no skip):

| Test (Budgie cell) | Fails with | Fixed by |
|---|---|---|
| `test_de_integration.py::test_file_manager_runs_open_in_fork` | Nemo's folder menu has no "Open in Fork": `timed out after 60 s waiting for fork-linux's license question` | 1 |
| `test_de_fork.py::test_06_open_in_file_explorer_opens_the_desktops_file_manager` (then 07-09 fail fast) | `timed out after 60 s waiting for a nemo window showing the repository` | 2 |
| `test_de_fork.py::test_07_file_managers_open_in_fork_reaches_the_running_fork` | as the integration test | 1 |

Both were checked on a scratch copy of this branch: the release tarball built from the patched
tree with `docker/Dockerfile.release`, installed by the same tier through `FL_E2E_DE_TARBALL`.
With both patches the Budgie integration tier passes 14/14 and the real-Fork tier 9/9 (see the
matrix in [../DE-E2E.md](../DE-E2E.md)). The unit tests below pass with 100 % line and branch
coverage of `desktop_integration.py` and `host_helper.py` (Python 3.14 in
`fork-linux-ci-coverage:26.04`, Python 3.10 in `fork-linux-ci-jammy:22.04`); `ruff check`,
`no-suppressions-lint.py` and `gen-data.py check` are clean (`fork-linux-ci-lint:26.04`).
Without the source changes the new unit tests fail (fix 1: `tests/test_desktop_integration.py`
does not import, `di.NEMO_FOLDER` is missing; fix 2: the two new `tests/test_host_helper.py`
tests fail). The two patches touch different files and apply in either order (`git apply`).

## 1. Nemo has no "Open in Fork" inside a folder

Nemo shows an action either on the selected items (`Selection=s`, `m`, ...) or on the folder a
window shows, i.e. its background menu (`Selection=none`; `%P` is that folder). Our only
action, `fork-linux-open.nemo_action`, is `Selection=s` with `%F`: right-clicking a repository
folder in its parent works, but right-clicking inside the repository (what Thunar's,
Caja's, Dolphin's and PCManFM-Qt's "Open in Fork" offer, and what the tier does on every
desktop) shows no "Open in Fork" at all. The same holds on Cinnamon, whose file manager is Nemo
too. Checked in the Budgie image: an extra action with `Selection=none` and
`Exec=... %P` appears in the background menu right after Nemo's own "Create New Document" and
gets the folder (also with spaces in its name, `Quote=double`); the existing item action is
unchanged. Nemo watches its action folders, so a running Nemo needs no restart.

Fix: a second Nemo kind, `nemo-folder` (`fork-linux-open-folder.nemo_action`, rendered from the
new template `nemo_action_folder.in`), installed and removed with `nemo` exactly like
Nautilus's extension and script pair; packages install it system-wide too
(`data/fm-actions/meson.build`, `gen-data.py fm-action nemo-folder`).

## 2. "Open In File Explorer" starts Nemo inside Wine's sandbox, where it aborts

Fork's Repository > Open In File Explorer runs `explorer.exe <folder>`, which Wine hands to
`fork-linux-explorer`, then `fork-linux-handoff open <folder>`, then
`fork-linux-host open <folder>`, which runs `xdg-open <folder>`. Without a systemd user manager
(non-systemd distributions, containers) the hand-off runs the helper directly, so `xdg-open`
and the file manager it starts inherit Wine-staging's seccomp filter and `NoNewPrivs=1` (the
reason the bridge exists, spike B2). Budgie's Nemo service quits when idle (about 10 s after its
last window), so `gio open` starts a new Nemo there; GTK's glycin image loaders it spawns are
killed with SIGSYS (exit status 159) and Nemo aborts on the missing icon
(`Gtk:ERROR:...gtkiconhelper.c:495: ... Failed to load .../image-missing.svg: Loader process
exited early`). Nothing opens. Traced in the tier with logging wrappers: the call came with
`NoNewPrivs: 1`, `Seccomp: 2`, and the Nemo it started died as above. On XFCE the folder goes
to the long-running `thunar --daemon`, which is outside Wine, so it never showed there.

Fix: `fork-linux-host open` shows a folder through `org.freedesktop.FileManager1.ShowFolders`
(gdbus, else dbus-send) before falling back to `xdg-open`, as `reveal` already does with
`ShowItems` (the D-Bus call is shared in `Host._file_manager1`). The session bus starts the
file manager's D-Bus service itself, outside Wine's process tree, whether or not a systemd user
manager exists; a desktop without a FileManager1 service keeps the `xdg-open` behaviour. With
it, step 06 shows Nemo's window on the repository (the session bus log shows
`Activating service name='org.freedesktop.FileManager1' requested by ... gdbus call`).

A wider fix of the hand-off itself is not proposed: routing it through the bridge daemon
(`fl-launch.exe`) would help only with the bridge on, and the bridge is off by default
(`[git] bridge = off`, experimental). Terminals and default applications that the direct
hand-off starts still run inside Wine's sandbox on a system without a systemd user manager;
xfce4-terminal and the tier's stub application work there.

## Not a failing test: Budgie's terminal choice

fork-linux reaches xfce4-terminal on Ubuntu Budgie only because Ptyxis, kgx and
gnome-terminal (earlier in `host_helper.TERMINALS`) are not installed; it does not read
Budgie's own setting (`org.gnome.desktop.default-applications.terminal exec`, `xfce4-terminal`
in Ubuntu Budgie's `:Budgie` override, which labwc's Ctrl+Alt+T also runs). A Budgie user who
also has gnome-terminal gets gnome-terminal. Reading that key (and Cinnamon's
`org.cinnamon.desktop.default-applications.terminal`) is a feature decision for the maintainer,
so no patch is proposed here.

## Patch 1: the Nemo folder action (src, data, meson, gen-data + unit tests)

Apply with `git apply` from the repository root.

```diff
diff --git a/data/fm-actions/meson.build b/data/fm-actions/meson.build
--- a/data/fm-actions/meson.build
+++ b/data/fm-actions/meson.build
@@ -7,6 +7,7 @@
 fl_fm_actions = {
   'nautilus': [fl_datadir / 'nautilus-python' / 'extensions', 'fork_linux_nautilus.py', 'rw-r--r--'],
   'nemo': [fl_datadir / 'nemo' / 'actions', 'fork-linux-open.nemo_action', 'rw-r--r--'],
+  'nemo-folder': [fl_datadir / 'nemo' / 'actions', 'fork-linux-open-folder.nemo_action', 'rw-r--r--'],
   'dolphin': [fl_datadir / 'kio' / 'servicemenus', 'fork-linux-open.desktop', 'rwxr-xr-x'],
 }
 foreach fl_kind, fl_spec : fl_fm_actions
diff --git a/meson.build b/meson.build
--- a/meson.build
+++ b/meson.build
@@ -81,6 +81,7 @@
   fl_templates / 'fma-action.desktop.in',
   fl_templates / 'fork_linux_nautilus.py.in',
   fl_templates / 'nemo_action.in',
+  fl_templates / 'nemo_action_folder.in',
   fl_templates / 'thunar-uca-action.xml.in',
   install_dir: fl_pkgdatadir / 'fork_linux' / 'data' / 'templates',
   install_mode: 'rw-r--r--',
diff --git a/scripts/gen-data.py b/scripts/gen-data.py
--- a/scripts/gen-data.py
+++ b/scripts/gen-data.py
@@ -7,7 +7,7 @@
 * ``metainfo IN OUT``      the AppStream metainfo: ``@APP_ID@`` / ``@APP_NAME@`` and the
   ``@DESCRIPTION@`` paragraphs from ``credits.render_metainfo_paragraphs()``;
 * ``fm-action KIND OUT``   a system-wide "Open in Fork" file-manager action
-  (``nautilus``, ``nemo``, ``dolphin``, ``fma``) from the same templates;
+  (``nautilus``, ``nemo``, ``nemo-folder``, ``dolphin``, ``fma``) from the same templates;
 * ``completions SHELL``    bash / zsh / fish completions generated from the argparse parser;
 * ``man PAGE``             the ``fork-linux.1`` / ``fork.1`` manual pages, from the parser too;
 * ``write`` / ``check``    regenerate (or verify, exit 1 on drift) the committed
@@ -38,7 +38,7 @@
     ("man", "fork-linux"): Path("data/man/fork-linux.1"),
     ("man", "fork"): Path("data/man/fork.1"),
 }
-FM_KINDS = ("nautilus", "nemo", "dolphin", "fma")
+FM_KINDS = ("nautilus", "nemo", "nemo-folder", "dolphin", "fma")
 SYSTEM_COMMAND = ["fork-linux"]
 GENERATED_NOTE = "GENERATED by fork-linux desktop install; removed by fork-linux desktop remove."
 SYSTEM_NOTE = "Installed system-wide by the fork-linux package (meson -Dfile_manager_actions=true)."
diff --git a/src/fork_linux/data/templates/nemo_action_folder.in b/src/fork_linux/data/templates/nemo_action_folder.in
new file mode 100644
--- /dev/null
+++ b/src/fork_linux/data/templates/nemo_action_folder.in
@@ -0,0 +1,11 @@
+# @APP_NAME@: "@LABEL@" for Nemo, on the folder a Nemo window shows (its background menu).
+# GENERATED by fork-linux desktop install; removed by fork-linux desktop remove.
+[Nemo Action]
+Active=true
+Name=@LABEL@
+Comment=@TOOLTIP@
+Exec=@EXEC@ open --from-file-manager %P
+Icon-Name=@APP_ID@
+Selection=none
+Extensions=any;
+Quote=double
diff --git a/src/fork_linux/desktop_integration.py b/src/fork_linux/desktop_integration.py
--- a/src/fork_linux/desktop_integration.py
+++ b/src/fork_linux/desktop_integration.py
@@ -67,17 +67,19 @@
 NAUTILUS = "nautilus"
 NAUTILUS_SCRIPT = "nautilus-script"
 NEMO = "nemo"
+NEMO_FOLDER = "nemo-folder"
 FMA = "fma"
 DOLPHIN = "dolphin"
 THUNAR = "thunar"
 CLI_ALIAS = "cli-alias"
-FILE_KINDS = (MENU, ICON, NAUTILUS, NAUTILUS_SCRIPT, NEMO, FMA, DOLPHIN)
+FILE_KINDS = (MENU, ICON, NAUTILUS, NAUTILUS_SCRIPT, NEMO, NEMO_FOLDER, FMA, DOLPHIN)
 
 AUTO = "auto"
 # File manager name -> integration kinds it needs.
 _MANAGER_KINDS = {
     "nautilus": (NAUTILUS, NAUTILUS_SCRIPT),
-    "nemo": (NEMO,),
+    # Nemo shows an action either on selected items or on the folder it shows (its background).
+    "nemo": (NEMO, NEMO_FOLDER),
     "caja": (FMA,),
     "fma": (FMA,),
     "dolphin": (DOLPHIN,),
@@ -163,6 +165,7 @@
             NAUTILUS: self.data / "nautilus-python" / "extensions" / "fork_linux_nautilus.py",
             NAUTILUS_SCRIPT: self.data / "nautilus" / "scripts" / LABEL,
             NEMO: self.data / "nemo" / "actions" / "fork-linux-open.nemo_action",
+            NEMO_FOLDER: self.data / "nemo" / "actions" / "fork-linux-open-folder.nemo_action",
             FMA: self.data / "file-manager" / "actions" / "fork-linux-open.desktop",
             DOLPHIN: self.data / "kio" / "servicemenus" / "fork-linux-open.desktop",
         }[kind]
@@ -360,6 +363,8 @@
         return render_template("nautilus-script", {**common, "EXEC_SH": shlex.join(cmd)}), 0o755
     if kind == NEMO:
         return render_template("nemo_action", common), 0o644
+    if kind == NEMO_FOLDER:
+        return render_template("nemo_action_folder", common), 0o644
     if kind == FMA:
         return render_template("fma-action.desktop", common), 0o644
     return render_template("dolphin-servicemenu.desktop", common), 0o755
diff --git a/tests/test_desktop_files.py b/tests/test_desktop_files.py
--- a/tests/test_desktop_files.py
+++ b/tests/test_desktop_files.py
@@ -41,7 +41,7 @@
     subprocess.run(["desktop-file-validate", str(path)], check=True)
 
 
-@pytest.mark.parametrize("kind", ["nautilus", "nemo", "dolphin", "fma"])
+@pytest.mark.parametrize("kind", ["nautilus", "nemo", "nemo-folder", "dolphin", "fma"])
 def test_system_file_manager_actions(kind: str) -> None:
     text = gen.render_fm_action(kind)
     per_user, _mode = desktop_integration._render_kind(kind, ["fork-linux"])
diff --git a/tests/test_desktop_integration.py b/tests/test_desktop_integration.py
--- a/tests/test_desktop_integration.py
+++ b/tests/test_desktop_integration.py
@@ -235,6 +235,25 @@
     assert "Exec=e open --from-file-manager %F" in text
 
 
+def test_nemo_gets_an_action_on_items_and_one_on_the_folder_it_shows(
+    paths: Paths, env: dict[str, str], runner: RecordingRunner
+) -> None:
+    """Nemo shows an action on selected items (Selection=s) or on its background (Selection=none),
+    never both: right-clicking inside a repository needs the folder action, which gets the folder
+    Nemo shows as %P."""
+    dirs = _dirs(env)
+    installed = _install(paths, env, runner, menu=False, file_managers="nemo")
+    assert installed == [dirs.kind_path(di.NEMO), dirs.kind_path(di.NEMO_FOLDER)]
+    items = dirs.kind_path(di.NEMO).read_text(encoding="utf-8")
+    folder = dirs.kind_path(di.NEMO_FOLDER).read_text(encoding="utf-8")
+    assert "Selection=s\n" in items and "open --from-file-manager %F\n" in items
+    assert "Selection=none\n" in folder and "open --from-file-manager %P\n" in folder
+    assert dirs.kind_path(di.NEMO_FOLDER).name == "fork-linux-open-folder.nemo_action"
+    assert {entry["kind"] for entry in _registry(paths).values()} == {di.NEMO, di.NEMO_FOLDER}
+    di.remove(paths, env)
+    assert not dirs.kind_path(di.NEMO).exists() and not dirs.kind_path(di.NEMO_FOLDER).exists()
+
+
 # --- registry --------------------------------------------------------------------------------------
 
 
@@ -298,6 +317,7 @@
         dirs.kind_path(di.NAUTILUS),
         dirs.kind_path(di.NAUTILUS_SCRIPT),
         dirs.kind_path(di.NEMO),
+        dirs.kind_path(di.NEMO_FOLDER),
         dirs.kind_path(di.FMA),
         dirs.kind_path(di.DOLPHIN),
         dirs.thunar_uca,
@@ -305,7 +325,7 @@
     assert installed == expected
     registry = _registry(paths)
     assert {e["kind"] for e in registry.values()} == {
-        di.MENU, di.ICON, di.NAUTILUS, di.NAUTILUS_SCRIPT, di.NEMO, di.FMA, di.DOLPHIN, di.THUNAR,
+        di.MENU, di.ICON, di.NAUTILUS, di.NAUTILUS_SCRIPT, di.NEMO, di.NEMO_FOLDER, di.FMA, di.DOLPHIN, di.THUNAR,
     }
     for path in expected[:-1]:
         assert registry[str(path)]["sha256"] == di.fsutil.sha256_file(path)
@@ -396,7 +416,7 @@
     nemo.parent.mkdir(parents=True)
     nemo.symlink_to(tmp_path)
     installed = _install(paths, env, runner, file_managers="nemo,dolphin")
-    assert installed == [dirs.kind_path(di.DOLPHIN)]
+    assert installed == [dirs.kind_path(di.NEMO_FOLDER), dirs.kind_path(di.DOLPHIN)]
     assert dirs.menu_file.read_text(encoding="utf-8") == "[Desktop Entry]\nName=Mine\n"
     assert nemo.is_symlink()
 
@@ -412,7 +432,8 @@
     assert "Exec=/new/fork-linux open" in nemo.read_text(encoding="utf-8")
     # A file the user edited is kept.
     nemo.write_text("edited\n", encoding="utf-8")
-    assert _install(paths, env, runner, file_managers="nemo") == [dirs.menu_file]
+    reinstalled = _install(paths, env, runner, file_managers="nemo")
+    assert reinstalled == [dirs.menu_file, dirs.kind_path(di.NEMO_FOLDER)]
     assert nemo.read_text(encoding="utf-8") == "edited\n"
 
 
@@ -890,8 +911,8 @@
         ("none", []),
         ("NONE", []),
         ("caja, fma", [di.FMA]),
-        (["Thunar", "nemo"], [di.THUNAR, di.NEMO]),
-        ("all", [di.NAUTILUS, di.NAUTILUS_SCRIPT, di.NEMO, di.FMA, di.DOLPHIN, di.THUNAR]),
+        (["Thunar", "nemo"], [di.THUNAR, di.NEMO, di.NEMO_FOLDER]),
+        ("all", [di.NAUTILUS, di.NAUTILUS_SCRIPT, di.NEMO, di.NEMO_FOLDER, di.FMA, di.DOLPHIN, di.THUNAR]),
     ],
 )
 def test_kinds(spec: Any, kinds: list[str]) -> None:
@@ -911,11 +932,11 @@
 @pytest.mark.parametrize(
     ("spec", "kinds"),
     [
-        ("auto", [di.NEMO, di.THUNAR]),
-        ("AUTO", [di.NEMO, di.THUNAR]),
-        ("auto,dolphin", [di.NEMO, di.THUNAR, di.DOLPHIN]),
-        ("thunar,auto", [di.NEMO, di.THUNAR]),
-        ("auto,all", [di.NAUTILUS, di.NAUTILUS_SCRIPT, di.NEMO, di.FMA, di.DOLPHIN, di.THUNAR]),
+        ("auto", [di.NEMO, di.NEMO_FOLDER, di.THUNAR]),
+        ("AUTO", [di.NEMO, di.NEMO_FOLDER, di.THUNAR]),
+        ("auto,dolphin", [di.NEMO, di.NEMO_FOLDER, di.THUNAR, di.DOLPHIN]),
+        ("thunar,auto", [di.NEMO, di.NEMO_FOLDER, di.THUNAR]),
+        ("auto,all", [di.NAUTILUS, di.NAUTILUS_SCRIPT, di.NEMO, di.NEMO_FOLDER, di.FMA, di.DOLPHIN, di.THUNAR]),
     ],
 )
 def test_kinds_auto_uses_detection(spec: str, kinds: list[str]) -> None:
@@ -972,7 +993,7 @@
     dirs = _dirs(env)
     _program(tmp_path / "empty-path", "nemo")
     installed = di.install(paths, env, runner=runner, exec_cmd=[LAUNCHER], menu=False, icons=False)
-    assert installed == [dirs.kind_path(di.NEMO)]
+    assert installed == [dirs.kind_path(di.NEMO), dirs.kind_path(di.NEMO_FOLDER)]
 
 
 def test_install_auto_removes_actions_of_file_managers_that_are_gone(
@@ -1183,7 +1204,7 @@
     removed = di.remove(paths, env, runner=runner)
     icons = [dirs.hicolor / f"{n}x{n}" / "apps" / f"{APP_ID}.png" for n in ICON_SIZES]
     icons.append(dirs.scalable_icon)
-    assert sorted(removed) == sorted([dirs.menu_file, *icons])
+    assert sorted(removed) == sorted([dirs.menu_file, *icons, dirs.kind_path(di.NEMO_FOLDER)])
     assert dirs.kind_path(di.NEMO).read_text(encoding="utf-8") == "changed\n"
     assert not paths.integrations_file.exists()
     assert di.remove(paths, env, runner=runner) == []
@@ -1218,7 +1239,7 @@
     assert result["ok"]
     assert result["launcher"] == di.launcher_command(env)
     assert {e["kind"]: e["state"] for e in result["entries"]} == {
-        di.MENU: "ok", di.NEMO: "ok", di.THUNAR: "ok", di.CLI_ALIAS: "ok",
+        di.MENU: "ok", di.NEMO: "ok", di.NEMO_FOLDER: "ok", di.THUNAR: "ok", di.CLI_ALIAS: "ok",
     }
     dirs.kind_path(di.NEMO).write_text("x", encoding="utf-8")
     (dirs.bin / "fork").unlink()
diff --git a/tests/test_meson_install.py b/tests/test_meson_install.py
--- a/tests/test_meson_install.py
+++ b/tests/test_meson_install.py
@@ -112,10 +112,11 @@
 def test_system_file_manager_actions(installed: Path) -> None:
     share = installed / "share"
     nemo = share / "nemo" / "actions" / "fork-linux-open.nemo_action"
+    nemo_folder = share / "nemo" / "actions" / "fork-linux-open-folder.nemo_action"
     dolphin = share / "kio" / "servicemenus" / "fork-linux-open.desktop"
     fma = share / "file-manager" / "actions" / "fork-linux-open.desktop"
     nautilus = share / "nautilus-python" / "extensions" / "fork_linux_nautilus.py"
-    for path in (nemo, dolphin, fma, nautilus):
+    for path in (nemo, nemo_folder, dolphin, fma, nautilus):
         text = path.read_text(encoding="utf-8")
         assert "Installed system-wide by the fork-linux package" in text
         assert "fork-linux open --from-file-manager" in text or "'fork-linux'" in text
```

## Patch 2: open a folder through FileManager1 (src + unit tests)

```diff
diff --git a/src/fork_linux/host_helper.py b/src/fork_linux/host_helper.py
--- a/src/fork_linux/host_helper.py
+++ b/src/fork_linux/host_helper.py
@@ -49,7 +49,7 @@
 verbs:
   terminal [DIR]                   open a terminal in DIR (default: the working directory)
   reveal PATH                      show PATH in the file manager
-  open PATH|URL                    open a file or an http(s)/mailto link
+  open PATH|URL                    open a file, a folder (in the file manager) or an http(s)/mailto link
   diff LEFT RIGHT                  compare two files (waits)
   merge BASE LOCAL REMOTE MERGED   three-way merge into MERGED (waits)
   edit PATH [LINE]                 open PATH in its default editor (waits)
@@ -271,36 +271,47 @@
             return False
         return result.ok
 
-    def reveal(self, target: str) -> int:
-        """Select ``target`` in the file manager (FileManager1), else open its folder."""
-        path = self.existing(target)
+    def _file_manager1(self, method: str, path: str) -> bool:
+        """``org.freedesktop.FileManager1.<method>`` on ``path`` (gdbus, else dbus-send); True when answered.
+
+        The file manager's D-Bus service is started by the session bus, outside the caller's
+        process tree: a helper Wine started (no systemd user manager to hand it to) never
+        passes Wine's seccomp filter on to the file manager.
+        """
         uri = file_uri(path)
         gdbus = [
             "gdbus", "call", "--session",
             "--dest", FILE_MANAGER1,
             "--object-path", FILE_MANAGER1_PATH,
-            "--method", f"{FILE_MANAGER1}.ShowItems",
+            "--method", f"{FILE_MANAGER1}.{method}",
             f"['{uri}']", "",
         ]
-        if self._try(gdbus):
-            return 0
         dbus_send = [
             "dbus-send", "--session", "--print-reply", f"--dest={FILE_MANAGER1}",
-            FILE_MANAGER1_PATH, f"{FILE_MANAGER1}.ShowItems",
+            FILE_MANAGER1_PATH, f"{FILE_MANAGER1}.{method}",
             f"array:string:{uri}", "string:",
         ]
-        if self._try(dbus_send):
+        return self._try(gdbus) or self._try(dbus_send)
+
+    def reveal(self, target: str) -> int:
+        """Select ``target`` in the file manager (FileManager1), else open its folder."""
+        path = self.existing(target)
+        if self._file_manager1("ShowItems", path):
             return 0
         return self.spawn(["xdg-open", os.path.dirname(path)])
 
     def open(self, target: str) -> int:
-        """Open a file with its default application, or an http(s) / mailto link."""
+        """Open a file with its default application, a folder in the file manager (FileManager1,
+        else xdg-open), or an http(s) / mailto link."""
         url = _URL.match(target)
         if url is not None:
             if url.group(1).lower() not in URL_SCHEMES:
                 raise UsageError(f"refusing to open a {url.group(1)!r} link", hint="only http, https and mailto links")
             return self.spawn(["xdg-open", target])
-        return self.spawn(["xdg-open", self.existing(target)])
+        path = self.existing(target)
+        if os.path.isdir(path) and self._file_manager1("ShowFolders", path):
+            return 0
+        return self.spawn(["xdg-open", path])
 
     def edit(self, target: str, line: str | None = None) -> int:
         """Open ``target`` in its default editor (the line number is accepted and ignored)."""
diff --git a/tests/test_host_helper.py b/tests/test_host_helper.py
--- a/tests/test_host_helper.py
+++ b/tests/test_host_helper.py
@@ -342,6 +342,33 @@
         host.open("C:\\missing.txt")
 
 
+def test_open_folders_go_to_filemanager1(tmp_path: Path) -> None:
+    """Fork's "Open In File Explorer" opens a folder: FileManager1.ShowFolders, whose service the
+    session bus starts outside Wine's process tree (Nemo started by xdg-open inside it, with no
+    systemd user manager, aborts when Wine's seccomp filter kills its image loaders)."""
+    host, runner, popen = _host(installed=("gdbus", "dbus-send", "xdg-open"))
+    assert host.open(str(tmp_path)) == 0
+    uri = hh.file_uri(tmp_path)
+    assert runner.argvs == [
+        [
+            "gdbus", "call", "--session", "--dest", hh.FILE_MANAGER1, "--object-path", hh.FILE_MANAGER1_PATH,
+            "--method", f"{hh.FILE_MANAGER1}.ShowFolders", f"['{uri}']", "",
+        ]
+    ]
+    assert popen.calls == []
+    host, runner, popen = _host(installed=("gdbus", "dbus-send", "xdg-open"), responses={"gdbus": 1})
+    assert host.open(str(tmp_path)) == 0
+    assert runner.argvs[1][5] == f"{hh.FILE_MANAGER1}.ShowFolders"
+    assert popen.calls == []
+
+
+def test_open_folder_without_filemanager1_uses_xdg_open(tmp_path: Path) -> None:
+    host, runner, popen = _host(installed=("gdbus", "dbus-send", "xdg-open"), responses={"gdbus": 1, "dbus-send": 1})
+    assert host.open(str(tmp_path)) == 0
+    assert [argv[0] for argv in runner.argvs] == ["gdbus", "dbus-send"]
+    assert popen.argvs == [["xdg-open", str(tmp_path)]]
+
+
 def test_edit(tmp_path: Path) -> None:
     target = tmp_path / "f.py"
     target.write_text("x", encoding="utf-8")
```
