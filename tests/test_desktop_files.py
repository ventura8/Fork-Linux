"""The system menu entry and file-manager actions match the per-user ones (single source)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fixtures import gen_data

from fork_linux import APP_ID, APP_NAME, credits, desktop_integration

ROOT = Path(__file__).resolve().parents[1]


gen = gen_data.load()


def test_system_entry_is_the_per_user_template_for_fork_linux_on_path() -> None:
    assert gen.render_desktop() == desktop_integration._render_menu(["fork-linux"])


def test_system_entry_contents() -> None:
    text = gen.render_desktop()
    assert f"Name={APP_NAME}\n" in text
    assert f"Icon={APP_ID}\n" in text
    assert "Exec=fork-linux run %F\n" in text
    assert "TryExec=" not in text
    assert "StartupWMClass=fork.exe\n" in text
    assert f"Comment={credits.render_desktop_comment()}\n" in text
    assert "https://git-fork.com/buy" in text
    assert "not affiliated" in text


def test_desktop_file_validate(tmp_path: Path) -> None:
    if shutil.which("desktop-file-validate") is None:
        pytest.skip("desktop-file-validate is not installed (the lint stage runs it)")
    path = tmp_path / f"{APP_ID}.desktop"
    path.write_text(gen.render_desktop(), encoding="utf-8")
    subprocess.run(["desktop-file-validate", str(path)], check=True)


@pytest.mark.parametrize("kind", ["nautilus", "nemo", "dolphin", "fma"])
def test_system_file_manager_actions(kind: str) -> None:
    text = gen.render_fm_action(kind)
    per_user, _mode = desktop_integration._render_kind(kind, ["fork-linux"])
    assert text == per_user.replace(gen.GENERATED_NOTE, gen.SYSTEM_NOTE)
    assert gen.GENERATED_NOTE not in text


def test_unknown_action_kind_is_refused() -> None:
    with pytest.raises(SystemExit):
        gen.render_fm_action("thunar")


def test_output_paths_are_confined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert gen.safe_path("x/y.desktop") == (tmp_path / "x" / "y.desktop").resolve()
    with pytest.raises(SystemExit):
        gen.safe_path("/etc/passwd")
    with pytest.raises(SystemExit):
        gen.safe_path("../outside.desktop")
    assert gen.main(["desktop", "out.desktop"]) == 0
    assert (tmp_path / "out.desktop").read_text(encoding="utf-8") == gen.render_desktop()


def test_build_info() -> None:
    text = gen.render_build_info("1.2.3", "deb")
    assert 'VERSION = "1.2.3"' in text
    assert 'FLAVOR = "deb"' in text
    assert "from __future__ import annotations" in text
    for version, flavor in (("1.2", "deb"), ("1.2.3", "Deb"), ("1.2.3", "a b")):
        with pytest.raises(SystemExit):
            gen.render_build_info(version, flavor)


def test_name_lookup(capsys: pytest.CaptureFixture[str]) -> None:
    assert gen.main(["name", "APP_ID"]) == 0
    assert capsys.readouterr().out.strip() == APP_ID
