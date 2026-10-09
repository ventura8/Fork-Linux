"""Tests for fork_linux.manifest: the packaged manifest, overrides and strict validation."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from fork_linux import manifest
from fork_linux.errors import ExitCode, IntegrityFailed, NotFound, UsageError

WINE_ID = "kron4ek-11.0-staging-wow64"
DELETE = object()
GOOD_SHA = "a" * 64


def base_data() -> dict[str, Any]:
    """The packaged manifest document as plain JSON data."""
    return json.loads(manifest.DEFAULT_PATH.read_text(encoding="utf-8"))


def mutate(data: dict[str, Any], path: tuple[str, ...], value: Any) -> dict[str, Any]:
    """Copy ``data`` and set (or delete, with DELETE) the key at ``path``."""
    out = copy.deepcopy(data)
    node = out
    for key in path[:-1]:
        node = node[key]
    if value is DELETE:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return out


def write(tmp_path: Path, name: str, data: Any) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def extra_version(sha: str = GOOD_SHA, status: str = "known-good") -> dict[str, Any]:
    return {
        "size": 1000,
        "sha256": sha,
        "full_nupkg_sha256": "b" * 64,
        "status": status,
        "tested_with": [],
    }


# -- the packaged manifest ------------------------------------------------------


def test_packaged_manifest_loads() -> None:
    m = manifest.load()
    assert m.revision == "2026.10.1"
    assert m.bootstrap_revision == 1
    assert m.overridden is False
    assert m.min_system_wine == "9.0"
    assert m.dotnet_verbs == ("dotnet48", "dotnet472")
    assert m.dotnet_min_release == 461808
    assert m.dotnet_framework == "4.7.2"
    assert m.fork_default == "2.23.2"
    assert m.feed_url == "https://git-fork.com/update/win/releases.win.json"
    assert m.legacy_feed_url == "https://git-fork.com/update/win/RELEASES"
    assert m.allowed_hosts == ("cdn.fork.dev", "git-fork.com", "fork.dev")


def test_wine_default_build() -> None:
    m = manifest.load()
    build = m.wine_default
    assert build == m.wine_build(WINE_ID)
    assert build.id == WINE_ID
    assert build.version == "11.0"
    assert build.flavor == "staging"
    assert build.arch == "x86_64"
    assert build.wow64 is True
    assert build.url.startswith("https://github.com/Kron4ek/Wine-Builds/releases/download/11.0/")
    assert build.sha256 == "e6538a417dd2e7f738ad77addf74bfd9e45c9d970653674f93bde2df29055f2f"
    assert build.size == 74975524
    assert build.strip_components == 1
    assert build.min_glibc == "2.27"
    assert build.status == "known-good"
    assert m.wine_builds == (build,)


def test_wine_build_unknown_raises_not_found() -> None:
    with pytest.raises(NotFound) as info:
        manifest.load().wine_build("nope")
    assert info.value.exit_code == ExitCode.NOT_FOUND
    assert WINE_ID in info.value.hint


def test_winetricks_pin() -> None:
    tricks = manifest.load().winetricks
    assert tricks == manifest.Winetricks(
        version="20260125",
        url="https://raw.githubusercontent.com/Winetricks/winetricks/20260125/src/winetricks",
        sha256="431f82fc74000e6c864409f1d8fb495d696c03928808e3e8acffc45179312a7b",
        size=830687,
    )


SHIPPED_INSTALLER_SHA = "fee9b2bf84aca6297d7b7e10b29a09c624ac16a82486f2c04136f5ebaf8f079e"


def test_shipped_fork_entry_is_pinned() -> None:
    entry = manifest.load().fork_version("2.23.2")
    assert entry is not None
    assert entry.sha256 == SHIPPED_INSTALLER_SHA


def test_fork_version_entry_with_pending_sha(tmp_path: Path) -> None:
    data = mutate(base_data(), ("fork", "versions", "2.23.2", "sha256"), "PENDING")
    m = manifest.load(write(tmp_path, "m.json", data))
    entry = m.fork_version("2.23.2")
    assert entry is not None
    assert entry.sha256 is None
    assert entry.size == 76278256
    assert entry.full_nupkg_sha256 == "b70e7be92b73baeab9fee700ad352ae9e261dafcded0d10e5c94133f639b1c1f"
    assert entry.status == "known-good"
    assert entry.tested_with == (WINE_ID,)
    assert m.fork_version("2.23.2.0") == entry
    assert m.fork_version("1.0") is None
    assert m.fork_version("garbage") is None


def test_installer_url_and_entry() -> None:
    m = manifest.load()
    assert m.installer_url("2.23.2") == "https://cdn.fork.dev/win/Fork-2.23.2.exe"
    entry = m.installer_entry()
    assert entry.version == "2.23.2"
    assert entry.url == "https://cdn.fork.dev/win/Fork-2.23.2.exe"
    assert entry.size == 76278256
    assert entry.sha256 == SHIPPED_INSTALLER_SHA
    assert entry.requires_tofu is False


def test_installer_entry_with_pending_sha_requires_tofu(tmp_path: Path) -> None:
    data = mutate(base_data(), ("fork", "versions", "2.23.2", "sha256"), "PENDING")
    entry = manifest.load(write(tmp_path, "m.json", data)).installer_entry()
    assert entry.sha256 is None
    assert entry.requires_tofu is True


def test_installer_entry_for_unknown_version_requires_tofu() -> None:
    entry = manifest.load().installer_entry("2.30.0")
    assert entry == manifest.InstallerEntry("2.30.0", "https://cdn.fork.dev/win/Fork-2.30.0.exe", None, None)
    assert entry.requires_tofu is True


@pytest.mark.parametrize("bad", ["", "2.23.2/../x", "latest", "2.23.2-beta", "1.2.3.4.5", "２.23"])
def test_installer_url_rejects_non_plain_versions(bad: str) -> None:
    with pytest.raises(UsageError):
        manifest.load().installer_url(bad)


def test_installer_url_rejects_non_string() -> None:
    with pytest.raises(UsageError):
        manifest.load().installer_url(2.23)


def test_known_good_and_known_bad() -> None:
    m = manifest.load()
    assert m.known_good_versions() == ["2.23.2"]
    assert m.is_known_bad("2.23.2") is False
    assert m.known_bad_reason("2.23.2") is None
    assert m.known_bad_reason("9.9") is None
    assert m.is_known_bad("nonsense") is False


def test_as_dict_is_a_copy() -> None:
    m = manifest.load()
    snapshot = m.as_dict()
    snapshot["revision"] = "changed"
    assert m.as_dict()["revision"] == "2026.10.1"
    assert snapshot["schema"] == 1


# -- overrides --------------------------------------------------------------------


def test_override_adds_versions_and_sets_default(tmp_path: Path) -> None:
    patch = {
        "fork": {
            "default": "2.24.0",
            "versions": {
                "2.24.0": extra_version(),
                "2.23.0": extra_version(status="testing"),
                "2.22.0": extra_version(status="known-bad"),
                "2.21.0": extra_version(),
            },
            "known_bad": {"2.21.0": "crashes on start under Wine 11"},
        }
    }
    m = manifest.load(override=write(tmp_path, "override.json", patch))
    assert m.overridden is True
    assert m.fork_default == "2.24.0"
    assert m.known_good_versions() == ["2.24.0", "2.23.2"]
    assert m.is_known_bad("2.22.0") is True
    assert "known-bad" in (m.known_bad_reason("2.22") or "")
    assert m.known_bad_reason("2.21.0.0") == "crashes on start under Wine 11"
    assert m.is_known_bad("2.23.0") is False
    entry = m.installer_entry()
    assert entry.sha256 == GOOD_SHA
    assert entry.size == 1000
    assert entry.requires_tofu is False


def test_override_null_deletes_a_key(tmp_path: Path) -> None:
    m = manifest.load(override=write(tmp_path, "o.json", {"fork": {"known_bad": None}}))
    assert "known_bad" not in m.as_dict()["fork"]
    assert m.is_known_bad("2.23.2") is False


def test_missing_override_is_ignored(tmp_path: Path) -> None:
    m = manifest.load(override=tmp_path / "absent.json")
    assert m.overridden is False
    (tmp_path / "dangling.json").symlink_to(tmp_path / "nowhere.json")
    assert manifest.load(override=tmp_path / "dangling.json").overridden is False


def test_override_null_document_must_be_an_object(tmp_path: Path) -> None:
    with pytest.raises(IntegrityFailed, match="must be a JSON object"):
        manifest.load(override=write(tmp_path, "o.json", None))


def test_unreadable_override_is_an_error_not_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    override = write(tmp_path, "o.json", {"revision": "2026.10.2"})
    real_read_text = Path.read_text

    def guarded(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == override:
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    with pytest.raises(IntegrityFailed, match="cannot read runtime manifest .*Permission denied"):
        manifest.load(override=override)


def test_override_must_be_an_object(tmp_path: Path) -> None:
    with pytest.raises(IntegrityFailed, match="must be a JSON object"):
        manifest.load(override=write(tmp_path, "o.json", ["not", "an", "object"]))


def test_override_with_invalid_json(tmp_path: Path) -> None:
    bad = tmp_path / "o.json"
    bad.write_text("{nope", encoding="utf-8")
    with pytest.raises(IntegrityFailed, match="not valid JSON"):
        manifest.load(override=bad)


def test_override_that_is_a_directory(tmp_path: Path) -> None:
    (tmp_path / "o.json").mkdir()
    with pytest.raises(IntegrityFailed, match="cannot read"):
        manifest.load(override=tmp_path / "o.json")


def test_override_that_breaks_validation(tmp_path: Path) -> None:
    with pytest.raises(IntegrityFailed) as info:
        manifest.load(override=write(tmp_path, "o.json", {"wine": {"default": "missing-build"}}))
    assert info.value.exit_code == ExitCode.INTEGRITY_FAILED
    assert "override" in info.value.hint


def test_load_explicit_path(tmp_path: Path) -> None:
    data = mutate(base_data(), ("revision",), "2027.1.5")
    assert manifest.load(write(tmp_path, "m.json", data)).revision == "2027.1.5"


def test_load_missing_file(tmp_path: Path) -> None:
    with pytest.raises(IntegrityFailed, match="cannot read"):
        manifest.load(tmp_path / "missing.json")


def test_load_non_utf8_file(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    path.write_bytes(b"\xff\xfe{}")
    with pytest.raises(IntegrityFailed, match="cannot read"):
        manifest.load(path)


def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    path.write_text('{"schema": 1, "schema": 1}', encoding="utf-8")
    with pytest.raises(IntegrityFailed, match="duplicate key"):
        manifest.load(path)


def test_nan_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    path.write_text('{"schema": NaN}', encoding="utf-8")
    with pytest.raises(IntegrityFailed, match="NaN is not allowed"):
        manifest.load(path)


def test_comment_keys_are_ignored() -> None:
    data = mutate(base_data(), ("_comment",), "top-level note")
    data = mutate(data, ("fork", "versions", "2.23.2", "_note"), "sha filled by spike 3")
    assert manifest.Manifest(data).fork_default == "2.23.2"


def test_comment_keys_are_ignored_inside_maps() -> None:
    data = mutate(base_data(), ("wine", "builds", "_comment"), "builds are listed newest first")
    data = mutate(data, ("fork", "versions", "_comment"), "installer sha256 comes from spike 3")
    data = mutate(data, ("fork", "known_bad", "_comment"), "version -> reason")
    m = manifest.Manifest(data)
    assert [build.id for build in m.wine_builds] == [WINE_ID]
    assert m.known_good_versions() == ["2.23.2"]
    assert m.is_known_bad("2.23.2") is False


def test_a_map_holding_only_comments_counts_as_empty() -> None:
    with pytest.raises(IntegrityFailed, match="at least one build"):
        manifest.Manifest(mutate(base_data(), ("wine", "builds"), {"_comment": "nothing yet"}))
    with pytest.raises(IntegrityFailed, match="at least one version"):
        manifest.Manifest(mutate(base_data(), ("fork", "versions"), {"_comment": "nothing yet"}))


def test_deeply_nested_json_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    path.write_text("[" * 100_000, encoding="utf-8")
    with pytest.raises(IntegrityFailed, match="not valid JSON"):
        manifest.load(path)


def test_deeply_nested_override_is_rejected(tmp_path: Path) -> None:
    # Parses on Python >= 3.12 (C-stack based JSON limit) but would overflow merge_patch;
    # Python 3.10 already refuses it while parsing. Either way it is IntegrityFailed.
    depth = 3000
    override = tmp_path / "o.json"
    override.write_text('{"a":' * depth + "1" + "}" * depth, encoding="utf-8")
    with pytest.raises(IntegrityFailed, match="nested too deeply|not valid JSON"):
        manifest.load(override=override)


def test_merge_recursion_error_becomes_integrity_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def overflow(target: Any, patch: Any) -> Any:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(manifest, "merge_patch", overflow)
    with pytest.raises(IntegrityFailed, match="nested too deeply"):
        manifest.load(override=write(tmp_path, "o.json", {"revision": "2026.10.2"}))


def test_override_may_not_add_download_hosts(tmp_path: Path) -> None:
    patch = {
        "fork": {
            "allowed_hosts": ["cdn.fork.dev", "git-fork.com", "fork.dev", "mirror.example"],
            "installer_url_template": "https://mirror.example/Fork-{version}.exe",
        }
    }
    with pytest.raises(IntegrityFailed, match="may not add Fork download hosts: mirror.example") as info:
        manifest.load(override=write(tmp_path, "o.json", patch))
    assert "official hosts" in info.value.hint


def test_override_may_narrow_download_hosts(tmp_path: Path) -> None:
    patch = {
        "fork": {
            "allowed_hosts": ["cdn.fork.dev", "git-fork.com"],
        }
    }
    m = manifest.load(override=write(tmp_path, "o.json", patch))
    assert m.allowed_hosts == ("cdn.fork.dev", "git-fork.com")
    assert m.installer_url("2.23.2") == "https://cdn.fork.dev/win/Fork-2.23.2.exe"


def test_override_cannot_repair_a_broken_base(tmp_path: Path) -> None:
    broken = write(tmp_path, "m.json", mutate(base_data(), ("revision",), "bad"))
    fix = write(tmp_path, "o.json", {"revision": "2026.10.2"})
    with pytest.raises(IntegrityFailed, match="revision"):
        manifest.load(broken, override=fix)


def test_lookups_tolerate_non_strings() -> None:
    m = manifest.load()
    assert m.fork_version(None) is None
    assert m.is_known_bad(None) is False
    assert m.known_bad_reason(2.23) is None


# -- merge_patch ------------------------------------------------------------------


def test_merge_patch_rfc7386_semantics() -> None:
    target = {"a": {"b": 1, "c": 2}, "d": [1, 2], "e": "keep"}
    patch = {"a": {"b": None, "x": {"y": 1}}, "d": [3], "z": None}
    result = manifest.merge_patch(target, patch)
    assert result == {"a": {"c": 2, "x": {"y": 1}}, "d": [3], "e": "keep"}
    assert target == {"a": {"b": 1, "c": 2}, "d": [1, 2], "e": "keep"}


def test_merge_patch_replaces_non_objects() -> None:
    assert manifest.merge_patch({"a": 1}, [1]) == [1]
    assert manifest.merge_patch("text", {"a": 1}) == {"a": 1}


# -- strict validation --------------------------------------------------------------

FORK_V = ("fork", "versions", "2.23.2")
BUILD = ("wine", "builds", WINE_ID)

INVALID: list[tuple[tuple[str, ...], Any]] = [
    (("schema",), 2),
    (("schema",), True),
    (("schema",), "1"),
    (("schema",), DELETE),
    (("unexpected",), 1),
    (("revision",), "2026-10-01"),
    (("revision",), ""),
    (("revision",), "2026.10.1\n"),
    (("bootstrap_revision",), 0),
    (("wine",), []),
    (("wine", "default"), "missing"),
    (("wine", "default"), "Bad ID"),
    (("wine", "min_system_version"), "nine"),
    (("wine", "builds"), {}),
    (("wine", "builds"), []),
    (("wine", "builds", "Bad ID"), {}),
    (BUILD + ("version",), "11.0-rc1"),
    (BUILD + ("flavor",), "Staging!"),
    (BUILD + ("arch",), "i386"),
    (BUILD + ("arch",), 64),
    (BUILD + ("wow64",), "yes"),
    (BUILD + ("url",), "http://github.com/x.tar.xz"),
    (BUILD + ("url",), "https://user:pw@github.com/x.tar.xz"),
    (BUILD + ("url",), "https:///x.tar.xz"),
    (BUILD + ("url",), "https://github.com/x y.tar.xz"),
    (BUILD + ("url",), "https://github.com:0/x.tar.xz"),
    (BUILD + ("url",), "https://github.com:notaport/x.tar.xz"),
    (BUILD + ("url",), "https://[::1/x.tar.xz"),
    (BUILD + ("url",), "https://[::1]/x.tar.xz"),
    (BUILD + ("url",), "https://github.com\\evil.example/x.tar.xz"),
    (BUILD + ("url",), "https://github.com/caf\u00e9.tar.xz"),
    (BUILD + ("url",), "https://github.com/x\x7f.tar.xz"),
    (BUILD + ("url",), "https://github.com/x\t.tar.xz"),
    (BUILD + ("sha256",), "E6538A417DD2E7F738AD77ADDF74BFD9E45C9D970653674F93BDE2DF29055F2F"),
    (BUILD + ("sha256",), "PENDING"),
    (BUILD + ("sha256",), "abc"),
    (BUILD + ("size",), 0),
    (BUILD + ("size",), 1.5),
    (BUILD + ("size",), True),
    (BUILD + ("strip_components",), -1),
    (BUILD + ("min_glibc",), None),
    (BUILD + ("status",), "great"),
    (BUILD + ("status",), "known-bad"),
    (BUILD + ("extra",), 1),
    (BUILD + ("url",), DELETE),
    (("winetricks", "version"), "2026-01-25"),
    (("winetricks", "url"), "ftp://example.com/winetricks"),
    (("winetricks", "sha256"), "PENDING"),
    (("winetricks", "size"), -5),
    (("dotnet", "verbs"), []),
    (("dotnet", "verbs"), "dotnet48"),
    (("dotnet", "verbs"), ["dotnet48", "dotnet48"]),
    (("dotnet", "verbs"), ["vcrun2019"]),
    (("dotnet", "verbs"), [48]),
    (("dotnet", "min_release"), 0),
    (("fork", "default"), "2.0.0"),
    (("fork", "default"), "v2.23.2"),
    (("fork", "installer_url_template"), "https://cdn.fork.dev/win/Fork.exe"),
    (("fork", "installer_url_template"), "https://cdn.fork.dev/win/Fork-{version}-{version}.exe"),
    (("fork", "installer_url_template"), "https://cdn.fork.dev/win/Fork-{version}-{arch}.exe"),
    (("fork", "installer_url_template"), "https://cdn.fork.dev/win/Fork-{version}}.exe"),
    (("fork", "installer_url_template"), "https://evil.example/win/Fork-{version}.exe"),
    (("fork", "installer_url_template"), "http://cdn.fork.dev/win/Fork-{version}.exe"),
    (("fork", "feed_url"), "https://evil.example/releases.win.json"),
    (("fork", "legacy_feed_url"), "https://git-fork.com.evil.example/RELEASES"),
    (("fork", "allowed_hosts"), []),
    (("fork", "allowed_hosts"), ["CDN.fork.dev", "git-fork.com"]),
    (("fork", "allowed_hosts"), ["cdn.fork.dev", "cdn.fork.dev", "git-fork.com"]),
    (("fork", "allowed_hosts"), ["cdn.fork.dev/", "git-fork.com"]),
    (("fork", "requires"), {}),
    (("fork", "requires", "dotnet_framework"), "4.7.2+"),
    (("fork", "versions"), {}),
    (("fork", "versions"), []),
    (("fork", "versions", "2.23.2.0"), extra_version()),
    (("fork", "versions", "next"), extra_version()),
    (FORK_V + ("sha256",), "pending"),
    (FORK_V + ("sha256",), "A" * 64),
    (FORK_V + ("size",), 0),
    (FORK_V + ("full_nupkg_sha256",), "PENDING"),
    (FORK_V + ("full_nupkg_sha256",), "G" * 64),
    (FORK_V + ("status",), "fine"),
    (FORK_V + ("status",), "known-bad"),
    (FORK_V + ("tested_with",), "kron4ek"),
    (FORK_V + ("tested_with",), [WINE_ID, WINE_ID]),
    (FORK_V + ("tested_with",), ["Not An Id"]),
    (FORK_V + ("comment",), "x"),
    (("fork", "known_bad"), []),
    (("fork", "known_bad"), {"2.23.2": "broken"}),
    (("fork", "known_bad"), {"2.20": "a", "2.20.0": "b"}),
    (("fork", "known_bad"), {"2.20": ""}),
    (("fork", "known_bad"), {"2.20": 5}),
    (("fork", "known_bad"), {"bad key": "x"}),
]


@pytest.mark.parametrize(("path", "value"), INVALID, ids=[f"{'.'.join(p)}={v!r}"[:60] for p, v in INVALID])
def test_invalid_manifests_raise_integrity_failed(path: tuple[str, ...], value: Any) -> None:
    with pytest.raises(IntegrityFailed) as info:
        manifest.Manifest(mutate(base_data(), path, value))
    assert info.value.message.startswith("runtime manifest: ")
    assert info.value.hint


def test_top_level_must_be_an_object() -> None:
    with pytest.raises(IntegrityFailed, match="must be an object"):
        manifest.Manifest([])


def test_known_bad_is_optional() -> None:
    m = manifest.Manifest(mutate(base_data(), ("fork", "known_bad"), DELETE))
    assert m.known_good_versions() == ["2.23.2"]


def test_tested_with_may_be_empty() -> None:
    m = manifest.Manifest(mutate(base_data(), FORK_V + ("tested_with",), []))
    entry = m.fork_version("2.23.2")
    assert entry is not None
    assert entry.tested_with == ()


def test_pinned_installer_sha_is_kept() -> None:
    m = manifest.Manifest(mutate(base_data(), FORK_V + ("sha256",), GOOD_SHA))
    assert m.installer_entry().sha256 == GOOD_SHA
    assert m.installer_entry().requires_tofu is False


def test_url_with_port_is_accepted() -> None:
    url = "https://github.com:443/Kron4ek/x.tar.xz"
    assert manifest.Manifest(mutate(base_data(), BUILD + ("url",), url)).wine_default.url == url
