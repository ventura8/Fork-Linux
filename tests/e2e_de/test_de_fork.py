"""Desktop real-Fork tier: the official Fork, set up by the installed fork-linux, inside a real desktop.

Gated by ``FL_E2E_DE=<desktop>`` and ``FL_E2E_DE_FORK=1``; containers only (``scripts/ci-e2e-de.sh
<desktop> --fork``, which holds one of the host-wide E2E slots). The steps run in file order and
build on each other (one prefix, one Fork session); after a failed step the later ones fail fast.
Fork is driven like a user would: its own menus (Repository > Open In Shell / Open In File
Explorer), the ``fork`` command, the file manager's "Open in Fork", the desktop's theme switch.
Screenshots stay in the scratch root (they show Fork's logo).
"""

from __future__ import annotations

import json
import os
import shlex
import time
from collections.abc import Iterator
from pathlib import Path

import dekit
import desktops
import forkui
import pytest

from fixtures import real_tier

if not os.environ.get("FL_E2E_DE") or os.environ.get("FL_E2E_DE_FORK") != "1":
    pytest.skip(
        "requires FL_E2E_DE=<desktop> and FL_E2E_DE_FORK=1 (scripts/ci-e2e-de.sh <desktop> --fork: "
        "real Wine and the official Fork download in a container)",
        allow_module_level=True,
    )
# Containers only (AGENTS.md hard rule 18).
real_tier.enforce()

pytestmark = pytest.mark.usefixtures("in_order", "wine_stopped_afterwards")

SETUP = ("fork-linux", "setup", "--accept-fork-eula", "--no-gui")
SETUP_TIMEOUT = 3600.0
FAST_PATH_SECONDS = 30.0
# Mean brightness of Fork's window: light theme above, dark theme below.
LIGHT_MEAN = 0.65
DARK_MEAN = 0.35
# Fork 2.23.2's menu bar and its Repository menu, as offsets inside the main window (96 DPI).
REPOSITORY_MENU = (108, 16)
OPEN_IN_FILE_EXPLORER = (140, 422)
OPEN_IN_SHELL = (119, 445)
# File managers that read our action only when they start (integration kind -> program, `-q` quits it).
RESTARTED_FILE_MANAGERS = {desktops.THUNAR_UCA: "thunar", desktops.NAUTILUS_EXTENSION: "nautilus"}
# The repository of the tab step 04 opened, the active one from then on.
ACTIVE = "second"
# A terminal that aborts at start (Tilix inside Wine's tree: status 134) is gone well before this.
TERMINAL_ALIVE_SECONDS = 5.0
# doctor's desktop.handoff route in these containers (no systemd user manager).
HANDOFF_ROUTE = "launcher daemon (no systemd user session)"


@pytest.fixture(scope="module")
def wine_stopped_afterwards(session: dekit.Session) -> Iterator[None]:
    """After the scenario: ``wineserver -k`` before the session ends its processes."""
    yield
    forkui.stop_wine(session)


def _repos(session: dekit.Session) -> Path:
    return session.root / "repos"


def _windows_path(path: Path) -> str:
    """How Fork names a Linux path (pathmap: the Z: drive)."""
    return "Z:" + str(path).replace("/", "\\")


def _repository_menu(session: dekit.Session, item: tuple[int, int], name: str) -> None:
    """Click Repository > ``name`` in Fork's main window."""
    window = forkui.wait_main_window(session)
    session.activate(window)
    forkui.click(session, window, REPOSITORY_MENU)
    time.sleep(1.0)
    session.screenshot(f"menu-{name}")
    forkui.click(session, window, item)


def test_01_setup_completes(session: dekit.Session, installed: dekit.Installed) -> None:
    """Scenarios: DE-XFCE-FORK-SETUP, DE-KDE-FORK-SETUP, DE-KDEWAYLAND-FORK-SETUP, DE-LXQT-FORK-SETUP,
    DE-GNOME-FORK-SETUP, DE-BUDGIE-FORK-SETUP, DE-MATE-FORK-SETUP, DE-CINNAMON-FORK-SETUP"""
    forkui.seed_caches(session.home)
    started = time.monotonic()
    session.run(SETUP, timeout=SETUP_TIMEOUT)
    (session.logs / "timing-setup.txt").write_text(f"{time.monotonic() - started:.1f}\n", encoding="utf-8")
    listed = json.loads(session.run(["fork-linux", "setup", "--list-steps", "--json"]).stdout)
    assert listed["complete"] is True
    assert {row["status"] for row in listed["steps"]} <= {"done", "always"}


def test_02_doctor_deep_has_no_failures(session: dekit.Session) -> None:
    """Scenarios: DE-XFCE-FORK-SETUP, DE-KDE-FORK-SETUP, DE-KDEWAYLAND-FORK-SETUP, DE-LXQT-FORK-SETUP,
    DE-GNOME-FORK-SETUP, DE-BUDGIE-FORK-SETUP, DE-MATE-FORK-SETUP, DE-CINNAMON-FORK-SETUP"""
    report = json.loads(session.run(["fork-linux", "doctor", "--deep", "--json"], timeout=300).stdout)
    failed = [check for check in report["checks"] if check["status"] == "fail"]
    warned = [check for check in report["checks"] if check["status"] == "warn"]
    assert failed == [], failed
    assert warned == [], warned


def test_03_fork_runs_under_the_desktops_window_manager(
    session: dekit.Session, desktop: desktops.Desktop, installed: dekit.Installed
) -> None:
    """Scenarios: DE-XFCE-FORK-WINDOW, DE-KDE-FORK-WINDOW, DE-KDEWAYLAND-FORK-WINDOW, DE-LXQT-FORK-WINDOW,
    DE-GNOME-FORK-WINDOW, DE-BUDGIE-FORK-WINDOW, DE-MATE-FORK-WINDOW, DE-CINNAMON-FORK-WINDOW"""
    demo = forkui.make_repo(session, _repos(session) / "demo")
    # The desktop's light theme (some desktops, such as Ubuntu Budgie, start dark).
    for command in desktop.themes[desktops.LIGHT]:
        session.run(command)
    forkui.launch(session, demo)
    window = forkui.wait_main_window(session, first_run=True)
    time.sleep(5)
    assert session.window_manager_name() == desktop.wm_name
    assert window.has_class(forkui.WM_CLASS)
    # The menu entry's StartupWMClass names this window, so the desktop groups it under our icon.
    assert f"StartupWMClass={window.wm_class[-1]}" in installed.menu_file.read_text(encoding="utf-8")
    if desktop.frames_undecorated:
        assert session.parent(window.id) != session.root_window(), "the window manager did not frame Fork"
    else:  # unframed (its own title bar), but still one of the windows the window manager manages
        managed = session.run(["xprop", "-root", "_NET_CLIENT_LIST"]).stdout.rpartition("#")[2]
        assert hex(int(window.id)) in managed.replace(",", " ").split(), "the window manager does not manage Fork"
    shot = forkui.drawn_screenshot(session, "03-main")
    assert session.image_stat(shot, "mean") > LIGHT_MEAN, "the desktop is light"
    assert len(forkui.fork_pids(forkui.prefix(session))) == 1
    settings = forkui.settings(session)
    assert settings["Theme"] == 0, "the desktop is light"


def test_04_fork_command_opens_a_second_tab(session: dekit.Session) -> None:
    """Scenarios: DE-XFCE-FORK-SECOND-TAB, DE-KDE-FORK-SECOND-TAB, DE-KDEWAYLAND-FORK-SECOND-TAB,
    DE-LXQT-FORK-SECOND-TAB, DE-GNOME-FORK-SECOND-TAB, DE-BUDGIE-FORK-SECOND-TAB, DE-MATE-FORK-SECOND-TAB,
    DE-CINNAMON-FORK-SECOND-TAB"""
    second = forkui.make_repo(session, _repos(session) / "second")
    started = time.monotonic()
    result = session.run(["fork", str(second)], timeout=FAST_PATH_SECONDS)
    assert result.returncode == 0
    assert time.monotonic() - started < FAST_PATH_SECONDS
    time.sleep(3)
    assert len(forkui.fork_pids(forkui.prefix(session))) == 1, "fork must hand over to the running Fork"


def test_05_open_in_shell_opens_the_desktops_terminal(session: dekit.Session, desktop: desktops.Desktop) -> None:
    """Scenarios: DE-XFCE-FORK-SHELL, DE-KDE-FORK-SHELL, DE-KDEWAYLAND-FORK-SHELL, DE-LXQT-FORK-SHELL,
    DE-GNOME-FORK-SHELL, DE-BUDGIE-FORK-SHELL, DE-MATE-FORK-SHELL, DE-CINNAMON-FORK-SHELL, I-HANDOFF-DAEMON,
    D-DESKTOP-HANDOFF"""
    active = _repos(session) / ACTIVE
    before = {window.id for window in session.windows()}
    _repository_menu(session, OPEN_IN_SHELL, "open-in-shell")
    window = session.wait_window(
        lambda w: w.id not in before and w.has_class(desktop.terminal_class), f"a new {desktop.terminal} window", 60
    )
    session.evidence("05-terminal", window)
    dekit.wait_for(lambda: str(active) in dekit.children_cwds(window.pid or 0), 30, f"a shell in {active}")
    # The container has no systemd user manager: the hand-off went through the launcher's daemon,
    # so the terminal runs outside Wine's process tree, without any NoNewPrivs and seccomp filter a
    # Wine build gives its programs (a GTK 3 terminal that loads icons through glycin, such as Tilix,
    # aborts with them). It has what this test process has (the container's own baseline: Docker's
    # default seccomp profile).
    terminal = window.pid or 0
    daemon = forkui.handoff_daemon(session)
    (fork,) = forkui.fork_pids(forkui.prefix(session))
    baseline = forkui.confinement(os.getpid())
    report = {
        "this test (outside Wine)": (os.getpid(), baseline),
        "Fork.exe (Wine)": (fork, forkui.confinement(fork)),
        "hand-off daemon": (daemon.get("pid", 0), forkui.confinement(int(daemon.get("pid", 0)))),
        f"{desktop.terminal} (Open in Shell)": (terminal, forkui.confinement(terminal)),
    }
    time.sleep(TERMINAL_ALIVE_SECONDS)
    report[f"{desktop.terminal} after {TERMINAL_ALIVE_SECONDS:g} s"] = (terminal, forkui.confinement(terminal))
    (session.logs / "05-confinement.txt").write_text(
        "".join(f"{name}: pid {pid} {fields}\n" for name, (pid, fields) in report.items())
        + f"daemon record: handoff={daemon.get('handoff')}\nhandoff.log: {forkui.handoff_log(session)!r}\n",
        encoding="utf-8",
    )
    # Kron4ek 11.0 staging gave Fork NoNewPrivs and an extra seccomp filter; the pinned 11.18 adds neither
    # (Fork's line in 05-confinement.txt shows which). Either way nothing the hand-off starts may carry more
    # than this process.
    assert baseline["NoNewPrivs"] == "0", report
    assert daemon.get("handoff") is True, f"no hand-off daemon in session.json: {daemon}"
    assert forkui.confinement(int(daemon["pid"])) == baseline, f"the daemon runs inside Wine's tree: {report}"
    assert forkui.confinement(terminal) == baseline, report
    assert window.id in {w.id for w in session.windows()}, f"{desktop.terminal} did not stay open"
    assert forkui.handoff_log(session) == "", "the hand-off ran inside Wine's process tree"
    # doctor names the same route and the running daemon, and has no fallback to report.
    report = json.loads(session.run(["fork-linux", "doctor", "--check", "desktop.handoff", "--json"]).stdout)
    check = forkui.doctor_check(report, "desktop.handoff")
    (session.logs / "05-doctor-handoff.json").write_text(json.dumps(check, indent=2) + "\n", encoding="utf-8")
    assert check.get("status") == "ok", check
    assert check.get("detail") == (
        f"{HANDOFF_ROUTE}: the hand-off daemon runs (pid {daemon['pid']}, port {daemon['port']})"
    ), check


def test_06_open_in_file_explorer_opens_the_desktops_file_manager(
    session: dekit.Session, desktop: desktops.Desktop
) -> None:
    """Scenarios: DE-XFCE-FORK-EXPLORER, DE-KDE-FORK-EXPLORER, DE-KDEWAYLAND-FORK-EXPLORER,
    DE-LXQT-FORK-EXPLORER, DE-GNOME-FORK-EXPLORER, DE-BUDGIE-FORK-EXPLORER, DE-MATE-FORK-EXPLORER,
    DE-CINNAMON-FORK-EXPLORER"""
    before = {window.id for window in session.windows()}
    _repository_menu(session, OPEN_IN_FILE_EXPLORER, "open-in-file-explorer")
    window = session.wait_window(
        lambda w: w.id not in before and w.has_class(desktop.file_manager_class) and ACTIVE in w.name,
        f"a {desktop.file_manager} window showing the repository", 60,
    )
    session.evidence("06-file-manager", window)


def _opening(repo: Path) -> list[str]:
    """Command lines of the processes opening ``repo`` in Fork: the file manager's ``fork-linux open
    --from-file-manager``, then the Fork.exe that hands the (Windows) path over to the running Fork."""
    linux, windows = str(repo).encode(), _windows_path(repo).encode()
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            cmdline = Path("/proc", entry, "cmdline").read_bytes()
        except OSError:
            continue
        if windows in cmdline or (b"--from-file-manager" in cmdline and linux in cmdline):
            found.append(f"{entry}: {shlex.join(os.fsdecode(arg) for arg in cmdline.split(bytes(1)) if arg)}")
    return found


def test_07_file_managers_open_in_fork_reaches_the_running_fork(
    session: dekit.Session, desktop: desktops.Desktop
) -> None:
    """Scenarios: DE-XFCE-FORK-OPEN-IN-FORK, DE-KDE-FORK-OPEN-IN-FORK, DE-KDEWAYLAND-FORK-OPEN-IN-FORK,
    DE-LXQT-FORK-OPEN-IN-FORK, DE-GNOME-FORK-OPEN-IN-FORK, DE-BUDGIE-FORK-OPEN-IN-FORK,
    DE-MATE-FORK-OPEN-IN-FORK, DE-CINNAMON-FORK-OPEN-IN-FORK"""
    third = forkui.make_repo(session, _repos(session) / "third")
    # install.sh asked to restart a file manager that was running while it added the action.
    program = RESTARTED_FILE_MANAGERS.get(desktop.integration)
    if program:
        session.run([program, "-q"], check=False)
        dekit.wait_for(lambda: not dekit.pids_of(program), 30, f"{program} to quit")
    session.spawn("fm-third", [desktop.file_manager, str(third)])
    window = session.wait_window(
        lambda w: w.has_class(desktop.file_manager_class) and third.name in w.name, f"{desktop.file_manager} on {third}"
    )
    assert _opening(third) == []
    try:
        # `or None`: wait_for returns any value but None/False at once, an empty list included.
        started = session.context_action(
            window, desktop.open_in_fork_keys, lambda: _opening(third) or None,
            "the file manager to start fork-linux on the repository",
        )
    finally:
        session.screenshot("07-after-keys")
    (session.logs / "07-opening.txt").write_text("\n".join(started) + "\n", encoding="utf-8")
    dekit.wait_for(lambda: not _opening(third), 60, "fork-linux to hand the repository over and exit")
    assert len(forkui.fork_pids(forkui.prefix(session))) == 1, "Open in Fork must reach the running Fork"


def test_08_tabs_were_kept_and_a_dark_desktop_relaunches_dark(
    session: dekit.Session, desktop: desktops.Desktop
) -> None:
    """Scenarios: DE-XFCE-FORK-DARK, DE-KDE-FORK-DARK, DE-KDEWAYLAND-FORK-DARK, DE-LXQT-FORK-DARK,
    DE-GNOME-FORK-DARK, DE-BUDGIE-FORK-DARK, DE-MATE-FORK-DARK, DE-CINNAMON-FORK-DARK"""
    forkui.close_fork(session)
    # The terminal Fork opened in step 05 is the user's now: closing Fork leaves it running.
    assert any(window.has_class(desktop.terminal_class) for window in session.windows())
    saved = json.dumps(forkui.settings(session))
    for name in ("demo", "second", "third"):
        assert json.dumps(_windows_path(_repos(session) / name))[1:-1] in saved, f"Fork has no tab for {name}"
    for command in desktop.themes[desktops.DARK]:
        session.run(command)
    try:
        forkui.launch(session, _repos(session) / "demo")
        # Started without a user action, the relaunched Fork may open behind the file manager of
        # step 07 (muffin's focus-stealing prevention): bring it up as a click on it would.
        session.activate(forkui.wait_main_window(session))
        settings = forkui.settings(session)
        assert settings["Theme"] == 1
        assert settings["FollowSystemTheme"] is False
        shot = forkui.drawn_screenshot(session, "08-dark")
        assert session.image_stat(shot, "mean") < DARK_MEAN
    finally:
        for command in desktop.reset:
            session.run(command)


def test_09_doctor_after_use_has_no_failures(session: dekit.Session) -> None:
    """Scenarios: DE-XFCE-FORK-SETUP, DE-KDE-FORK-SETUP, DE-KDEWAYLAND-FORK-SETUP, DE-LXQT-FORK-SETUP,
    DE-GNOME-FORK-SETUP, DE-BUDGIE-FORK-SETUP, DE-MATE-FORK-SETUP, DE-CINNAMON-FORK-SETUP, D-DESKTOP-HANDOFF"""
    forkui.close_fork(session)
    report = json.loads(session.run(["fork-linux", "doctor", "--deep", "--json"], timeout=300).stdout)
    failed = [check for check in report["checks"] if check["status"] == "fail"]
    assert failed == [], failed
    # Fork closed: the route the next launch takes, and no hand-off fell back during this one.
    handoff = forkui.doctor_check(report, "desktop.handoff")
    assert handoff.get("status") == "ok", handoff
    assert handoff.get("detail") == f"{HANDOFF_ROUTE}: a hand-off-only daemon starts with Fork", handoff
    # The desktop held up through Fork's whole run: none of its own programs crashed.
    assert session.components_gone() == [], dekit.PROCESS_LOG
