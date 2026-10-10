"""Tests for fork_linux.registry: the REGEDIT5 writer and the Wine hive reader."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from fork_linux import registry
from fork_linux.errors import ForkLinuxError
from fork_linux.registry import (
    REG_BINARY,
    REG_DWORD,
    REG_EXPAND_SZ,
    REG_LINK,
    REG_MULTI_SZ,
    REG_NONE,
    REG_QWORD,
    REG_SZ,
    RegBatch,
    RegHive,
    RegistryFormatError,
    RegKey,
    RegValue,
    normalize_key,
    parse_wine_reg,
    query,
    read_hive,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
USER_REG = FIXTURES / "user.reg"
SYSTEM_REG = FIXTURES / "system.reg"
CRLF = "\r\n"


def _text(batch: RegBatch) -> str:
    """Decode a rendered batch, asserting the BOM is present."""
    data = batch.render()
    assert data[:2] == b"\xff\xfe"
    return data[2:].decode("utf-16-le")


def _regedit_to_wine(text: str) -> str:
    """Rewrite a rendered REGEDIT5 file into Wine's hive syntax (keys relative to the hive)."""
    out = [registry.WINE_HEADER]
    for line in text.split(CRLF)[1:]:
        if line.startswith("["):
            path = line[1:-1].split("\\", 1)[1]
            out.append("[" + path.replace("\\", "\\\\") + "]")
        else:
            out.append(line)
    return "\n".join(out)


@pytest.fixture
def prefix(tmp_path: Path) -> Path:
    """A fake Wine prefix holding copies of the fixture hives."""
    root = tmp_path / "prefix"
    root.mkdir()
    shutil.copy(USER_REG, root / "user.reg")
    shutil.copy(SYSTEM_REG, root / "system.reg")
    return root


# --------------------------------------------------------------------------- writer


def test_render_golden_text() -> None:
    batch = RegBatch()
    batch.set_sz("HKCU\\Software\\Wine\\DllOverrides", "winemenubuilder.exe", "")
    batch.set_dword("hkcu\\Software\\Wine\\Avalon.Graphics", "DisableHWAcceleration", 1)
    batch.set_sz("HKEY_CURRENT_USER\\Software\\Wine\\AppDefaults\\Fork.exe", "Version", "win7")
    batch.set_default(
        "HKLM\\Software\\Classes\\Folder\\shell\\open\\command",
        'C:\\fork-linux\\fl-launch.exe open "%1"',
    )
    batch.delete_key("HKLM\\Software\\Classes\\Folder\\shell\\open\\ddeexec")
    batch.delete_value("HKCU\\Software\\Wine\\Direct3D", "renderer")
    batch.delete_value("HKCU\\Software\\Wine\\Direct3D", "")
    expected = CRLF.join(
        [
            "Windows Registry Editor Version 5.00",
            "",
            "[HKEY_CURRENT_USER\\Software\\Wine\\DllOverrides]",
            '"winemenubuilder.exe"=""',
            "",
            "[HKEY_CURRENT_USER\\Software\\Wine\\Avalon.Graphics]",
            '"DisableHWAcceleration"=dword:00000001',
            "",
            "[HKEY_CURRENT_USER\\Software\\Wine\\AppDefaults\\Fork.exe]",
            '"Version"="win7"',
            "",
            "[HKEY_LOCAL_MACHINE\\Software\\Classes\\Folder\\shell\\open\\command]",
            '@="C:\\\\fork-linux\\\\fl-launch.exe open \\"%1\\""',
            "",
            "[-HKEY_LOCAL_MACHINE\\Software\\Classes\\Folder\\shell\\open\\ddeexec]",
            "",
            "[HKEY_CURRENT_USER\\Software\\Wine\\Direct3D]",
            '"renderer"=-',
            "@=-",
            "",
        ]
    ) + CRLF
    assert batch.render_text() == expected
    assert _text(batch) == expected


def test_render_is_utf16le_with_bom_and_crlf_only() -> None:
    batch = RegBatch().set_sz("HKCU\\Software\\FLTest", "Gr\u00fc\u00dfe", "\u5fae\u8f6f \U0001f600")
    data = batch.render()
    assert data.startswith(b"\xff\xfe")
    assert data.decode("utf-16") == batch.render_text()
    text = batch.render_text()
    assert "\n" not in text.replace(CRLF, "")
    assert "\r" not in text.replace(CRLF, "")
    assert text.endswith(CRLF + CRLF)
    assert '"Gr\u00fc\u00dfe"="\u5fae\u8f6f \U0001f600"' in text.split(CRLF)


def test_round_trip_through_wine_reader() -> None:
    long_path = "%USERPROFILE%\\AppData\\Local\\Programs\\fork-linux\\" + "very-long-directory-name\\" * 4
    batch = RegBatch()
    key = "HKCU\\Software\\FLTest\\Round Trip"
    batch.set_sz(key, 'quote"and\\slash', 'C:\\path with "quotes"\\')
    batch.set_sz(key, "control", "line1\nline2\ttab")
    batch.set_default(key, "default value")
    batch.set_expand_sz(key, "expand", long_path)
    batch.set_multi_sz(key, "multi", ["Noto Sans", "DejaVu Sans", "\u5fae\u8f6f"])
    batch.set_multi_sz(key, "empty multi", [])
    batch.set_dword(key, "max", 0xFFFFFFFF)
    batch.set_dword(key, "zero", 0)
    batch.set_binary(key, "logfont", bytes(range(92)))
    batch.set_binary(key, "empty binary", b"")
    text = _text(batch)
    for line in text.split(CRLF):
        assert len(line) <= 80
    assert '"control"=hex(1):' in text
    hive = parse_wine_reg(_regedit_to_wine(text))
    values = hive["software\\fltest\\round trip"].values
    assert {k: (v.type, v.data) for k, v in values.items()} == {
        'quote"and\\slash': (REG_SZ, 'C:\\path with "quotes"\\'),
        "control": (1, "line1\nline2\ttab"),
        "": (REG_SZ, "default value"),
        "expand": (REG_EXPAND_SZ, long_path),
        "multi": (REG_MULTI_SZ, ["Noto Sans", "DejaVu Sans", "\u5fae\u8f6f"]),
        "empty multi": (REG_MULTI_SZ, []),
        "max": (REG_DWORD, 0xFFFFFFFF),
        "zero": (REG_DWORD, 0),
        "logfont": (REG_BINARY, bytes(range(92))),
        "empty binary": (REG_BINARY, b""),
    }
    assert values['quote"and\\slash'].name == 'quote"and\\slash'


def test_hex_wrapping_matches_regedit_style() -> None:
    batch = RegBatch().set_expand_sz("HKCU\\Environment", "P", "x" * 60)
    lines = batch.render_text().split(CRLF)[3:-2]
    assert lines[0].startswith('"P"=hex(2):78,00,')
    assert all(line.endswith(",\\") for line in lines[:-1])
    assert all(line.startswith("  ") for line in lines[1:])
    assert lines[-1].endswith("00,00")
    assert len(lines) > 2


def test_hex_first_byte_stays_on_a_long_lead_line() -> None:
    name = "n" * 90
    batch = RegBatch().set_multi_sz("HKCU\\Environment", name, ["a"])
    lines = batch.render_text().split(CRLF)
    first = next(line for line in lines if line.startswith(f'"{name}"'))
    assert first == f'"{name}"=hex(7):61,\\'


def test_binary_is_written_as_plain_hex() -> None:
    lines = RegBatch().set_binary("HKCU\\Control Panel\\Desktop", "b", b"\x00\xff").render_text().split(CRLF)
    assert '"b"=hex:00,ff' in lines


def test_short_hex_has_no_continuation() -> None:
    batch = RegBatch().set_sz("HKCU\\Environment", "c", "\x01")
    assert '"c"=hex(1):01,00,00,00' in batch.render_text().split(CRLF)


@pytest.mark.parametrize(
    ("alias", "full"),
    [
        ("HKCU", "HKEY_CURRENT_USER"),
        ("hkcu", "HKEY_CURRENT_USER"),
        ("HKEY_CURRENT_USER", "HKEY_CURRENT_USER"),
        ("hkey_local_machine", "HKEY_LOCAL_MACHINE"),
        ("HKLM", "HKEY_LOCAL_MACHINE"),
        ("HKCR", "HKEY_CLASSES_ROOT"),
        ("HKU", "HKEY_USERS"),
        ("HKCC", "HKEY_CURRENT_CONFIG"),
    ],
)
def test_normalize_key_aliases(alias: str, full: str) -> None:
    assert normalize_key(f"{alias}\\Software\\Wine") == f"{full}\\Software\\Wine"


def test_normalize_key_collapses_separators() -> None:
    assert normalize_key("\\HKCU\\\\Software\\Wine\\") == "HKEY_CURRENT_USER\\Software\\Wine"


@pytest.mark.parametrize(
    "key",
    ["", "\\\\", "Software\\Wine", "HKXX\\Software", "HKCU", "HKCU\\", "HKCU\\bad\nname", "HKCU\\\ud800"],
)
def test_normalize_key_rejects(key: str) -> None:
    with pytest.raises(ValueError):
        normalize_key(key)


def test_batch_rejects_bad_key() -> None:
    batch = RegBatch()
    with pytest.raises(ValueError):
        batch.set_sz("Software\\Wine", "a", "b")
    with pytest.raises(ValueError):
        batch.delete_key("HKLM")


@pytest.mark.parametrize("name", ["bad\nname", "tab\tname", "\udc00"])
def test_value_name_rejected(name: str) -> None:
    batch = RegBatch()
    with pytest.raises(ValueError):
        batch.set_sz("HKCU\\Software", name, "v")


@pytest.mark.parametrize("value", ["nul\x00inside", "\ud800"])
def test_set_sz_rejects(value: str) -> None:
    batch = RegBatch()
    with pytest.raises(ValueError):
        batch.set_sz("HKCU\\Software", "n", value)


@pytest.mark.parametrize("value", ["nul\x00inside", "\ud800"])
def test_set_expand_sz_rejects(value: str) -> None:
    batch = RegBatch()
    with pytest.raises(ValueError):
        batch.set_expand_sz("HKCU\\Software", "n", value)


@pytest.mark.parametrize("value", [-1, 2**32, "1", 1.0, None])
def test_set_dword_rejects(value: object) -> None:
    batch = RegBatch()
    with pytest.raises(ValueError):
        batch.set_dword("HKCU\\Software", "n", value)  # deliberately wrong types


def test_set_dword_accepts_bounds_and_bool() -> None:
    text = RegBatch().set_dword("HKCU\\S", "a", 0).set_dword("HKCU\\S", "b", True).render_text()
    assert '"a"=dword:00000000' in text
    assert '"b"=dword:00000001' in text


@pytest.mark.parametrize("values", ["abc", ["ok", ""], ["nul\x00"], ["\ud800"]])
def test_set_multi_sz_rejects(values: object) -> None:
    batch = RegBatch()
    with pytest.raises(ValueError):
        batch.set_multi_sz("HKCU\\Software", "n", values)


def test_set_multi_sz_accepts_any_iterable() -> None:
    text = RegBatch().set_multi_sz("HKCU\\S", "m", (s for s in ["a", "b"])).render_text()
    assert '"m"=hex(7):61,00,00,00,62,00,00,00,00,00' in text
    empty = RegBatch().set_multi_sz("HKCU\\S", "m", []).render_text()
    assert '"m"=hex(7):00,00' in empty


def test_empty_name_is_default_value() -> None:
    text = RegBatch().set_sz("HKCU\\S", "", "d").render_text()
    assert '@="d"' in text.split(CRLF)


def test_blocks_merge_only_when_adjacent() -> None:
    batch = RegBatch()
    batch.set_sz("HKCU\\Software\\Wine", "a", "1")
    batch.set_sz("hkey_current_user\\SOFTWARE\\wine", "b", "2")
    batch.set_sz("HKCU\\Software\\Other", "c", "3")
    batch.set_sz("HKCU\\Software\\Wine", "d", "4")
    lines = batch.render_text().split(CRLF)
    headers = [line for line in lines if line.startswith("[")]
    assert headers == [
        "[HKEY_CURRENT_USER\\Software\\Wine]",
        "[HKEY_CURRENT_USER\\Software\\Other]",
        "[HKEY_CURRENT_USER\\Software\\Wine]",
    ]
    assert lines[2:6] == ["[HKEY_CURRENT_USER\\Software\\Wine]", '"a"="1"', '"b"="2"', ""]


def test_delete_key_ordering_and_dedup() -> None:
    batch = RegBatch()
    batch.delete_key("HKCR\\Folder\\shell\\open\\ddeexec")
    batch.delete_key("hkcr\\folder\\shell\\open\\DDEEXEC")
    batch.set_sz("HKCR\\Folder\\shell\\open\\ddeexec", "x", "1")
    batch.delete_key("HKCR\\Folder\\shell\\open\\ddeexec")
    batch.set_sz("HKCR\\Folder\\shell\\open\\ddeexec", "y", "2")
    headers = [line for line in batch.render_text().split(CRLF) if line.startswith("[")]
    assert headers == [
        "[-HKEY_CLASSES_ROOT\\Folder\\shell\\open\\ddeexec]",
        "[HKEY_CLASSES_ROOT\\Folder\\shell\\open\\ddeexec]",
        "[-HKEY_CLASSES_ROOT\\Folder\\shell\\open\\ddeexec]",
        "[HKEY_CLASSES_ROOT\\Folder\\shell\\open\\ddeexec]",
    ]


def test_is_empty_and_empty_render() -> None:
    batch = RegBatch()
    assert batch.is_empty()
    assert batch.render_text() == "Windows Registry Editor Version 5.00\r\n\r\n"
    batch.delete_value("HKCU\\S", "x")
    assert not batch.is_empty()


def test_methods_chain() -> None:
    batch = RegBatch()
    assert batch.set_sz("HKCU\\S", "a", "b") is batch
    assert batch.delete_key("HKCU\\T") is batch


def test_write_creates_parents(tmp_path: Path) -> None:
    batch = RegBatch().set_dword("HKCU\\Software\\Wine\\WineDbg", "ShowCrashDialog", 0)
    target = tmp_path / "deep" / "dir" / "batch.reg"
    result = batch.write(str(target))
    assert result == target
    assert isinstance(result, Path)
    assert target.read_bytes() == batch.render()
    assert sorted(p.name for p in target.parent.iterdir()) == ["batch.reg"]


def test_write_replaces_symlink_instead_of_following(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    target = tmp_path / "batch.reg"
    target.symlink_to(victim)
    batch = RegBatch().set_sz("HKCU\\Software\\FLTest", "a", "b")
    batch.write(target)
    assert victim.read_text() == "keep me"
    assert not target.is_symlink()
    assert target.read_bytes() == batch.render()


def test_write_overwrites_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "batch.reg"
    target.write_bytes(b"old")
    batch = RegBatch().set_dword("HKCU\\Software\\FLTest", "n", 7)
    batch.write(target)
    assert target.read_bytes() == batch.render()


def test_write_failure_leaves_no_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "out" / "batch.reg"
    (target / "occupied").mkdir(parents=True)
    batch = RegBatch().set_sz("HKCU\\Software\\FLTest", "a", "b")
    with pytest.raises(OSError):
        batch.write(target)
    assert sorted(p.name for p in target.parent.iterdir()) == ["batch.reg"]
    assert target.is_dir()


@pytest.mark.parametrize(
    "call",
    [
        lambda b: b.set_sz("HKCU\\S", "n", Path("/x")),
        lambda b: b.set_sz("HKCU\\S", 5, "v"),
        lambda b: b.set_expand_sz("HKCU\\S", "n", b"bytes"),
        lambda b: b.set_multi_sz("HKCU\\S", "n", ["ok", 3]),
        lambda b: b.set_binary("HKCU\\S", "n", bytearray(b"x")),
        lambda b: b.set_binary("HKCU\\S", "n", "text"),
        lambda b: b.set_sz(Path("HKCU/S"), "n", "v"),
        lambda b: b.delete_key(None),
    ],
)
def test_non_string_arguments_are_rejected(call: object) -> None:
    batch = RegBatch()
    with pytest.raises(ValueError):
        call(batch)
    assert batch.is_empty()


# --------------------------------------------------------------------------- reader


def test_parse_user_fixture() -> None:
    hive = read_hive(USER_REG)
    assert isinstance(hive, RegHive)
    assert hive.root == "REGISTRY\\User\\S-1-5-21-0-0-0-1000"
    assert hive.arch == "win64"
    desktop = hive["Control Panel\\Desktop"]
    assert desktop.path == "Control Panel\\Desktop"
    assert desktop.timestamp == 1791520885
    assert desktop.get("logpixels") == RegValue("LogPixels", REG_DWORD, 96)
    assert desktop.get("FontSmoothing") == RegValue("FontSmoothing", REG_SZ, "2")
    assert desktop.get("UserPreferencesMask") == RegValue(
        "UserPreferencesMask", REG_BINARY, bytes([0x30, 0, 2, 0x80, 0x12, 0, 0, 0])
    )
    caption = hive["control panel\\desktop\\windowmetrics"].get("CaptionFont")
    assert caption is not None
    assert caption.type == REG_BINARY
    assert isinstance(caption.data, bytes)
    assert len(caption.data) == 47
    assert caption.data[:2] == b"\x0a\x00"
    assert caption.data[28:40] == "Tahoma".encode("utf-16-le")
    assert hive["Control Panel\\Desktop\\WindowMetrics"].get("IconTitleWrap") == RegValue(
        "IconTitleWrap", REG_SZ, "1"
    )
    folders = hive["Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\User Shell Folders"]
    assert folders.get("desktop") == RegValue("Desktop", REG_EXPAND_SZ, "%USERPROFILE%\\Desktop")
    overrides = hive["SOFTWARE\\WINE\\DLLOVERRIDES"]
    assert overrides.get("winemenubuilder.exe") == RegValue("winemenubuilder.exe", REG_SZ, "")
    assert overrides.get("*mscoree") == RegValue("*mscoree", REG_SZ, "native")
    replacements = hive["Software\\Wine\\Fonts\\Replacements"]
    assert replacements.get("\u5fae\u8f6f\u96c5\u9ed1") == RegValue(
        "\u5fae\u8f6f\u96c5\u9ed1", REG_SZ, "Noto Sans CJK SC"
    )
    command = hive["Software\\Classes\\Folder\\shell\\open\\command"]
    assert command.get("") == RegValue("", REG_SZ, 'C:\\fork-linux\\fl-launch.exe open "%1"')


def test_parse_escapes_and_types() -> None:
    key = read_hive(USER_REG)["Software\\FLTest\\Escapes [brackets]"]
    assert key.path == "Software\\FLTest\\Escapes [brackets]"

    def data(name: str) -> object:
        value = key.get(name)
        assert value is not None
        return value.data

    assert data("quoted") == 'say "hi"\tthen\\leave\n'
    assert data("octal") == "a\x00bAA2"
    assert data("hex") == "\u5fae\u8f6f\u96c5\u9ed1 \U0001f600 xzz"
    assert data("unknown escape") == "q"
    assert key.get("mixed case name") == RegValue("Mixed Case Name", REG_SZ, "kept")
    assert key.values["mixed case name"].name == "Mixed Case Name"
    assert data("multi") == ["Noto Sans", "DejaVu Sans", "Liberation Sans"]
    assert data("multi hex") == ["ab", "c"]
    assert data("expand hex") == "%USERPROFILE%"
    assert key.get("expand hex") == RegValue("expand hex", REG_EXPAND_SZ, "%USERPROFILE%")
    assert key.get("sz hex") == RegValue("sz hex", REG_SZ, "ok")
    assert key.get("qword") == RegValue("qword", REG_QWORD, 0x200000000)
    assert data("dword be") == 256
    assert key.get("short dword") == RegValue("short dword", REG_DWORD, b"\x01\x00")
    assert key.get("none") == RegValue("none", REG_NONE, b"")
    assert key.get("device") == RegValue("device", 0xFFFF0012, b"\x01\x02")
    assert data("explicit sz") == "plain"


def test_parse_system_fixture() -> None:
    hive = read_hive(str(SYSTEM_REG))
    assert hive.root == "REGISTRY\\Machine"
    full = hive["Software\\Microsoft\\NET Framework Setup\\NDP\\v4\\Full"]
    assert full.get("Release") == RegValue("Release", REG_DWORD, 528049)
    assert full.get("InstallPath") == RegValue(
        "InstallPath", REG_SZ, "C:\\windows\\Microsoft.NET\\Framework64\\v4.0.30319\\"
    )
    link = hive["Software\\Classes\\Wow6432Node\\AppId"].get("SymbolicLinkValue")
    assert link == RegValue("SymbolicLinkValue", REG_LINK, "\\Registry\\Machine\\Software\\Classes\\AppId")
    asp = hive["Software\\Microsoft\\ASP.NET\\4.0.30319.0"]
    assert asp.get("LastInstallTime") == RegValue("LastInstallTime", REG_QWORD, 0x01DD57A8B653884E)
    system_link = hive["Software\\Microsoft\\Windows NT\\CurrentVersion\\FontLink\\SystemLink"]
    assert system_link.get("Tahoma") == RegValue(
        "Tahoma", REG_MULTI_SZ, ["MSGOTHIC.TTC,MS UI Gothic", "MINGLIU.TTC,PMingLiU"]
    )
    assert hive["Software\\Wine\\Drives"].values == {}


def test_parse_older_root_line_crlf_and_bom() -> None:
    text = "\ufeffWINE REGISTRY Version 2\r\n;; All keys relative to \\\\Machine\r\n\r\n[A\\\\B]\r\n\"v\"=\"1\"\r\n"
    hive = parse_wine_reg(text)
    assert hive.root == "\\Machine"
    assert hive.arch == ""
    assert hive["a\\b"].get("v") == RegValue("v", REG_SZ, "1")
    assert hive["A\\B"].timestamp is None


@pytest.mark.parametrize(
    "text",
    ["", "garbage\n[A]\n", "WINE REGISTRY Version 1\n", "REGEDIT4\n", "Windows Registry Editor Version 5.00\n"],
)
def test_parse_rejects_bad_header(text: str) -> None:
    with pytest.raises(RegistryFormatError) as info:
        parse_wine_reg(text)
    assert isinstance(info.value, ForkLinuxError)
    assert info.value.hint


def test_parse_skips_malformed_lines() -> None:
    text = "\n".join(
        [
            registry.WINE_HEADER,
            '"orphan"="no key yet"',
            "[Unterminated\\\\Key",
            '"lost"="belongs to the broken key"',
            "[Good] notanumber",
            "#time=1dd57a87157acb0",
            '#class="x"',
            "; comment",
            "unrecognised junk",
            "  01,02,03",
            '"unterminated name=1',
            '"noequals" "x"',
            '"badtype"=foo:1',
            '"unterminated string"="abc',
            '"unterminated multi"=str(7):"abc',
            '"bad hex"=hex:zz,01',
            '"long token"=hex:123',
            '"bad hex type"=hex(zz):00',
            '"bad dword"=dword:zz',
            '"ok"=dword:0000002a',
            '@ = "spaced default"',
            '  "indented"="yes"',
            '"dangling"="a\\',
        ]
    )
    hive = parse_wine_reg(text)
    assert list(hive) == ["good"]
    good = hive["Good"]
    assert good.timestamp is None
    assert {k: v.data for k, v in good.values.items()} == {"ok": 42, "": "spaced default", "indented": "yes"}


def test_parse_continuation_at_eof_is_dropped() -> None:
    hive = parse_wine_reg(registry.WINE_HEADER + "\n[K]\n\"x\"=hex:01,\\")
    assert hive["K"].values == {}


def test_parse_continuation_joins_lines() -> None:
    text = registry.WINE_HEADER + "\n[K]\n\"x\"=hex:01,02,\\\n  03,\\\n\t04\n\"y\"=dword:00000001\n"
    values = parse_wine_reg(text)["K"].values
    assert values["x"].data == b"\x01\x02\x03\x04"
    assert values["y"].data == 1


@pytest.mark.parametrize("stamp", ["²", "١٢", "12 34", "-5", "0x10"])
def test_parse_non_ascii_or_odd_timestamp_is_ignored(stamp: str) -> None:
    text = registry.WINE_HEADER + f'\n[K] {stamp}\n"v"="1"\n'
    key = parse_wine_reg(text)["K"]
    assert key.timestamp is None
    assert key.get("v") == RegValue("v", REG_SZ, "1")


def test_parse_large_continued_value() -> None:
    data = bytes(range(256)) * 1200
    lines = registry._hex_lines('"big"=hex:', data)
    assert len(lines) > 10000
    text = "\n".join([registry.WINE_HEADER, "[K]", *lines, '"after"=dword:00000002', ""])
    values = parse_wine_reg(text)["K"].values
    assert values["big"].data == data
    assert values["after"].data == 2


def test_write_with_very_long_file_name(tmp_path: Path) -> None:
    target = tmp_path / ("n" * 250 + ".reg")
    batch = RegBatch().set_sz("HKCU\\Software\\FLTest", "a", "b")
    assert batch.write(target).read_bytes() == batch.render()


def test_parse_lone_surrogate_is_replaced() -> None:
    text = registry.WINE_HEADER + '\n[K]\n"s"="a\\xd800b"\n'
    assert parse_wine_reg(text)["K"].get("s") == RegValue("s", REG_SZ, "a\ufffdb")


def test_parse_merges_duplicate_keys() -> None:
    text = "\n".join(
        [registry.WINE_HEADER, "[K] 5", '"a"="1"', '"b"="1"', "[Other]", "[k] 9", '"B"="2"', '"c"="3"']
    )
    hive = parse_wine_reg(text)
    key = hive["K"]
    assert key.timestamp == 5
    assert {name: (v.name, v.data) for name, v in key.values.items()} == {
        "a": ("a", "1"),
        "b": ("B", "2"),
        "c": ("c", "3"),
    }


def test_parse_keys_filter() -> None:
    hive = read_hive(SYSTEM_REG, keys=["software\\microsoft\\NET Framework Setup\\NDP\\v4\\FULL\\"])
    assert list(hive) == ["software\\microsoft\\net framework setup\\ndp\\v4\\full"]
    assert hive["Software\\Microsoft\\NET Framework Setup\\NDP\\v4\\Full"].get("Release") is not None
    assert parse_wine_reg(USER_REG.read_text(), keys=[]) == {}


def test_hive_lookup_semantics() -> None:
    hive = read_hive(USER_REG)
    assert "software\\wine\\direct3d" in hive
    assert "\\Software\\\\Wine\\Direct3D\\" in hive
    assert "Software\\Wine\\Missing" not in hive
    assert 42 not in hive
    assert hive.get("Software\\Wine\\Missing") is None
    sentinel = RegKey("x")
    assert hive.get("Software\\Wine\\Missing", sentinel) is sentinel
    with pytest.raises(KeyError):
        hive["Software\\Wine\\Missing"]
    assert all(name == name.lower() for name in hive)
    assert hive["Software\\Wine\\Direct3D"].get("missing") is None


def test_hive_setitem_normalizes() -> None:
    hive = RegHive()
    hive["A\\B"] = RegKey("A\\B")
    assert list(hive) == ["a\\b"]
    assert hive["a\\B"].path == "A\\B"


def test_read_hive_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_hive(tmp_path / "nope.reg")


def test_read_hive_size_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "system.reg"
    content = (registry.WINE_HEADER + '\n[K]\n"v"="1"\n').encode()
    path.write_bytes(content)
    monkeypatch.setattr(registry, "_MAX_HIVE_BYTES", len(content))
    assert read_hive(path)["K"].get("v") is not None
    monkeypatch.setattr(registry, "_MAX_HIVE_BYTES", len(content) - 1)
    with pytest.raises(RegistryFormatError) as info:
        read_hive(path)
    assert "larger than" in str(info.value)
    assert info.value.hint


def test_read_hive_tolerates_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "user.reg"
    path.write_bytes(registry.WINE_HEADER.encode() + b'\n[K]\n"v"="a\xffb"\n')
    assert read_hive(path)["K"].get("v") == RegValue("v", REG_SZ, "a\ufffdb")


# --------------------------------------------------------------------------- query


def test_query_hkcu(prefix: Path) -> None:
    assert query(prefix, "HKCU", "Software\\Wine\\Direct3D", "renderer") == "gdi"
    assert query(prefix, "hkcu", "software\\wine\\avalon.graphics", "DisableHWAcceleration") == 1
    assert query(prefix, "HKEY_CURRENT_USER", "Software\\Wine\\AppDefaults\\Fork.exe", "version") == "win7"
    assert query(str(prefix), "HKCU", "Software\\FLTest\\Escapes [brackets]", "multi") == [
        "Noto Sans",
        "DejaVu Sans",
        "Liberation Sans",
    ]
    assert query(prefix, "HKCU", "Control Panel\\Desktop", "UserPreferencesMask") == bytes(
        [0x30, 0, 2, 0x80, 0x12, 0, 0, 0]
    )
    assert (
        query(prefix, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\User Shell Folders", "AppData")
        == "%USERPROFILE%\\AppData\\Roaming"
    )


def test_query_hklm(prefix: Path) -> None:
    key = "Software\\Microsoft\\NET Framework Setup\\NDP\\v4\\Full"
    assert query(prefix, "HKLM", key, "Release") == 528049
    assert query(prefix, "HKEY_LOCAL_MACHINE", key, "Version") == "4.8.04084"
    assert query(prefix, "HKLM", "Software\\Classes\\txtfile\\shell\\open\\command", "") == (
        "%SystemRoot%\\system32\\notepad.exe %1"
    )


def test_query_hkcr_prefers_user_classes(prefix: Path) -> None:
    assert query(prefix, "HKCR", "Folder\\shell\\open\\command", "") == 'C:\\fork-linux\\fl-launch.exe open "%1"'
    assert query(prefix, "HKEY_CLASSES_ROOT", "Folder\\shell\\explore\\command", "") == (
        'C:\\windows\\explorer.exe /n,/e,"%1"'
    )
    assert query(prefix, "HKCR", "Folder\\shell\\missing", "") is None


def test_query_absent_values(prefix: Path, tmp_path: Path) -> None:
    assert query(prefix, "HKCU", "Software\\Wine\\Missing", "x") is None
    assert query(prefix, "HKCU", "Software\\Wine\\Direct3D", "missing") is None
    assert query(prefix, "HKLM", "Software\\Wine\\Drives", "c:") is None
    empty = tmp_path / "empty-prefix"
    empty.mkdir()
    assert query(empty, "HKLM", "Software\\Microsoft\\NET Framework Setup\\NDP\\v4\\Full", "Release") is None
    assert query(empty, "HKCR", "Folder\\shell\\open\\command", "") is None


_MANY = [
    ("HKCU", "Software\\Wine\\Direct3D", "renderer"),
    ("HKLM", "Software\\Microsoft\\NET Framework Setup\\NDP\\v4\\Full", "Release"),
    ("HKCR", "Folder\\shell\\open\\command", ""),
    ("HKCR", "Folder\\shell\\explore\\command", ""),
    ("HKCR", "Folder\\shell\\missing", ""),
    ("HKCU", "Software\\Wine\\Direct3D", "missing"),
]


def test_query_many_matches_query(prefix: Path, tmp_path: Path) -> None:
    assert registry.query_many(prefix, _MANY) == [query(prefix, *item) for item in _MANY]
    assert registry.query_many(prefix, []) == []
    empty = tmp_path / "empty-prefix"
    empty.mkdir()
    assert registry.query_many(empty, _MANY) == [None] * len(_MANY)


def test_query_many_parses_each_hive_once(prefix: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list[str] = []
    real = registry.read_hive

    def counting(path: Path | str, *, keys: list[str] | None = None) -> registry.RegHive:
        reads.append(Path(path).name)
        return real(path, keys=keys)

    monkeypatch.setattr(registry, "read_hive", counting)
    registry.query_many(prefix, _MANY)
    assert sorted(reads) == ["system.reg", "user.reg"]


@pytest.mark.parametrize(
    ("text", "decoded"),
    [
        ("[a\\\\b] 1", ("a\\b", 6)),  # only escaped backslashes: the fast path
        ("[a\\]b] 1", ("a]b", 6)),  # an escaped terminator: the full decoder
        ("[a\\\\\\]b] 1", ("a\\]b", 8)),
    ],
)
def test_unescape_fast_path_keeps_escaped_terminators(text: str, decoded: tuple[str, int]) -> None:
    assert registry._unescape(text, 1, "]") == decoded


@pytest.mark.parametrize("hive", ["HKU", "HKEY_USERS", "HKCC", "bogus", ""])
def test_query_rejects_hive(prefix: Path, hive: str) -> None:
    with pytest.raises(ValueError):
        query(prefix, hive, "Software", "x")


def test_query_bad_hive_file_raises(tmp_path: Path) -> None:
    (tmp_path / "user.reg").write_text("not a registry\n")
    with pytest.raises(RegistryFormatError):
        query(tmp_path, "HKCU", "Software", "x")


def test_decode_binary_fallbacks() -> None:
    assert registry._decode_binary(REG_SZ, "abc".encode("utf-16-le")) == "abc"
    assert registry._decode_binary(REG_SZ, b"a\x00\x00\x00junk") == "a"
    assert registry._decode_binary(REG_QWORD, b"\x01") == b"\x01"
    assert registry._decode_binary(REG_BINARY, b"\x01\x02") == b"\x01\x02"
    assert registry._decode_binary(REG_MULTI_SZ, b"") == []
