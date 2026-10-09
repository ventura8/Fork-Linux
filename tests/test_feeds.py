"""Tests for fork_linux.feeds: Velopack/Squirrel feed parsing and the conditional-GET cache."""

from __future__ import annotations

import copy
import json
import random
import time
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from fixtures.http_server import FakeHTTPServer
from fork_linux import download, feeds, fsutil
from fork_linux.errors import DownloadFailed, ExitCode, IntegrityFailed

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LOOP = download.Policy(allow_loopback_http=True)
RELEASES_JSON = (FIXTURES / "releases.win.json").read_text(encoding="utf-8")
RELEASES_LEGACY = (FIXTURES / "RELEASES").read_bytes().decode("utf-8")
ETAG = '"6abcdef1-550a"'
LAST_MODIFIED = "Wed, 30 Sep 2026 10:05:37 GMT"


def feed_doc() -> dict[str, Any]:
    return json.loads(RELEASES_JSON)


def one_asset(**changes: Any) -> str:
    """The fixture's first asset with ``changes`` applied (None removes a key), as a feed document."""
    asset = copy.deepcopy(feed_doc()["Assets"][0])
    for key, value in changes.items():
        if value is None:
            asset.pop(key, None)
        else:
            asset[key] = value
    return json.dumps({"Assets": [asset]})


def make_asset(version: str, kind: str = "Full") -> feeds.FeedAsset:
    return feeds.FeedAsset(version, kind, f"Fork-{version}-{kind.lower()}.nupkg", "a" * 64, None, 10)


# -- releases.win.json -------------------------------------------------------------------


def test_parse_fixture_feed() -> None:
    assets = feeds.parse_releases_json(RELEASES_JSON)
    assert [(a.version, a.type) for a in assets] == [
        ("2.23.2", "Full"),
        ("2.23.2", "Delta"),
        ("2.23.1", "Full"),
        ("2.22.0", "Full"),
    ]
    first = assets[0]
    assert first == feeds.FeedAsset(
        version="2.23.2",
        type="Full",
        filename="Fork-2.23.2-full.nupkg",
        sha256="b70e7be92b73baeab9fee700ad352ae9e261dafcded0d10e5c94133f639b1c1f",
        sha1="52376cb9a78706df0e62ce21e9cc0bb9116c838d",
        size=73531413,
    )
    assert feeds.latest_full(assets) == first


def test_bom_and_unknown_keys_are_accepted() -> None:
    text = "\ufeff" + one_asset(SomethingNew={"nested": True}, NotesHTML=None, NotesMarkdown=None)
    assert feeds.parse_releases_json(text)[0].version == "2.23.2"


def test_sha1_is_optional_and_package_id_defaults_to_fork() -> None:
    asset = feeds.parse_releases_json(one_asset(SHA1=None, PackageId=None))[0]
    assert asset.sha1 is None


def test_empty_asset_list() -> None:
    assert feeds.parse_releases_json('{"Assets": []}') == []


def test_exact_duplicates_are_dropped_and_conflicts_rejected() -> None:
    doc = feed_doc()
    doc["Assets"].append(copy.deepcopy(doc["Assets"][0]))
    assert len(feeds.parse_releases_json(json.dumps(doc))) == 4
    doc["Assets"][-1]["SHA256"] = "0" * 64
    with pytest.raises(IntegrityFailed, match="conflicting entries for Fork-2.23.2-full.nupkg"):
        feeds.parse_releases_json(json.dumps(doc))


@pytest.mark.parametrize(
    "text",
    [
        "",
        "{not json",
        '{"Assets": [NaN]}',
        "[]",
        '{"assets": []}',
        '{"Assets": {}}',
        '{"Assets": ["Fork-2.23.2-full.nupkg"]}',
        "[" * 100_000,
        '{"Assets": ' + "[" * 100_000,
    ],
    ids=lambda text: repr(text)[:40],
)
def test_garbage_documents(text: str) -> None:
    with pytest.raises(IntegrityFailed) as info:
        feeds.parse_releases_json(text)
    assert info.value.exit_code == ExitCode.INTEGRITY_FAILED
    assert "ventura8/Fork-Linux" in info.value.hint


@pytest.mark.parametrize(
    "changes",
    [
        {"PackageId": "Evil"},
        {"Version": None},
        {"Version": 2.23},
        {"Version": "latest"},
        {"Version": "2.23.1"},
        {"Type": "full"},
        {"Type": "Patch"},
        {"Type": ["Full"]},
        {"FileName": None},
        {"FileName": 42},
        {"FileName": "Fork-2.23.2-delta.nupkg"},
        {"FileName": "../Fork-2.23.2-full.nupkg"},
        {"FileName": "Fork-2.23.2-full.nupkg.exe"},
        {"FileName": "Fork-2.23.2-full.nupkg\n"},
        {"SHA256": None},
        {"SHA256": "B70E7BE9"},
        {"SHA256": 12},
        {"SHA1": "XYZ"},
        {"SHA1": 7},
        {"Size": None},
        {"Size": 0},
        {"Size": -1},
        {"Size": True},
        {"Size": "73531413"},
        {"Size": 1.5},
    ],
    ids=lambda c: repr(c)[:50],
)
def test_invalid_assets(changes: dict[str, Any]) -> None:
    with pytest.raises(IntegrityFailed, match=r"Assets\[0\]"):
        feeds.parse_releases_json(one_asset(**changes))


# -- legacy RELEASES -------------------------------------------------------------------------


def test_parse_fixture_legacy_feed() -> None:
    assets = feeds.parse_releases_legacy(RELEASES_LEGACY)
    assert [a.version for a in assets] == ["2.22.0", "2.23.1", "2.23.2"]
    assert assets[-1] == feeds.FeedAsset(
        version="2.23.2",
        type="Full",
        filename="Fork-2.23.2-full.nupkg",
        sha256=None,
        sha1="52376cb9a78706df0e62ce21e9cc0bb9116c838d",
        size=73531413,
    )
    assert feeds.latest_full(assets) == assets[-1]


def test_legacy_crlf_blank_lines_and_delta() -> None:
    text = "\r\n52376CB9A78706DF0E62CE21E9CC0BB9116C838D  Fork-2.23.2-delta.nupkg\t2465782\r\n   \r\n"
    assert feeds.parse_releases_legacy(text) == [
        feeds.FeedAsset("2.23.2", "Delta", "Fork-2.23.2-delta.nupkg", None, "52376cb9a78706df0e62ce21e9cc0bb9116c838d", 2465782)
    ]
    assert feeds.parse_releases_legacy("") == []


@pytest.mark.parametrize(
    "line",
    [
        "52376CB9A78706DF0E62CE21E9CC0BB9116C838D Fork-2.23.2-full.nupkg",
        "52376CB9A78706DF0E62CE21E9CC0BB9116C838D Fork-2.23.2-full.nupkg 1 extra",
        "52376CB9 Fork-2.23.2-full.nupkg 73531413",
        "52376CB9A78706DF0E62CE21E9CC0BB9116C838D Fork-latest-full.nupkg 73531413",
        "52376CB9A78706DF0E62CE21E9CC0BB9116C838D https://evil.example/Fork-2.23.2-full.nupkg 73531413",
        "52376CB9A78706DF0E62CE21E9CC0BB9116C838D Fork-2.23.2-full.nupkg 0",
        "52376CB9A78706DF0E62CE21E9CC0BB9116C838D Fork-2.23.2-full.nupkg -5",
        "52376CB9A78706DF0E62CE21E9CC0BB9116C838D Fork-2.23.2-full.nupkg ²",
        "<html>Not Found</html>",
    ],
)
def test_invalid_legacy_lines(line: str) -> None:
    with pytest.raises(IntegrityFailed, match="RELEASES line 2 is malformed"):
        feeds.parse_releases_legacy("52376CB9A78706DF0E62CE21E9CC0BB9116C838D Fork-2.23.2-full.nupkg 1\n" + line)


def test_legacy_conflicting_duplicates() -> None:
    text = (
        "52376CB9A78706DF0E62CE21E9CC0BB9116C838D Fork-2.23.2-full.nupkg 1\n"
        "52376CB9A78706DF0E62CE21E9CC0BB9116C838D Fork-2.23.2-full.nupkg 2\n"
    )
    with pytest.raises(IntegrityFailed, match="conflicting"):
        feeds.parse_releases_legacy(text)


# -- latest_full ---------------------------------------------------------------------------


def test_latest_full_does_not_trust_order() -> None:
    assets = [make_asset(v) for v in ("2.9.0", "2.23.2", "2.10.1", "2.23.1")] + [make_asset("2.24.0", "Delta")]
    for seed in range(5):
        shuffled = assets[:]
        random.Random(seed).shuffle(shuffled)
        best = feeds.latest_full(shuffled)
        assert best is not None
        assert best.version == "2.23.2"


def test_latest_full_keeps_the_first_of_equal_versions() -> None:
    first, second = make_asset("2.23.2"), feeds.FeedAsset("2.23.2.0", "Full", "Fork-2.23.2.0-full.nupkg", None, None, 1)
    assert feeds.latest_full([first, second]) is first


def test_latest_full_without_full_packages() -> None:
    assert feeds.latest_full([]) is None
    assert feeds.latest_full([make_asset("2.23.2", "Delta")]) is None


# -- fetch_feed ------------------------------------------------------------------------------


def serve_feed(server: FakeHTTPServer, body: str = RELEASES_JSON, **options: Any) -> str:
    options.setdefault("etag", ETAG)
    options.setdefault("last_modified", LAST_MODIFIED)
    server.add("/update/win/releases.win.json", body.encode("utf-8"), **options)
    return server.url("/update/win/releases.win.json")


def age_cache(cache: Path, seconds: float) -> None:
    meta_path = cache / feeds.META_FILE
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["fetched_at"] = time.time() - seconds
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


def test_fetch_feed_caches_with_validators(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    url = serve_feed(http_server)
    cache = tmp_path / "feeds" / "json"
    assert feeds.fetch_feed(url, cache, policy=LOOP) == RELEASES_JSON
    meta = json.loads((cache / feeds.META_FILE).read_text(encoding="utf-8"))
    assert meta["url"] == url
    assert meta["etag"] == ETAG
    assert meta["last_modified"] == LAST_MODIFIED
    assert (cache / feeds.FEED_FILE).read_text(encoding="utf-8") == RELEASES_JSON
    assert list(cache.glob("*.tmp")) == []
    first = http_server.requests[0]
    assert "If-None-Match" not in first.headers
    assert first.headers["User-Agent"].startswith("fork-linux/")


def test_fresh_cache_needs_no_network(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    url = serve_feed(http_server)
    feeds.fetch_feed(url, tmp_path, policy=LOOP)
    assert feeds.fetch_feed(url, tmp_path, policy=LOOP) == RELEASES_JSON
    assert len(http_server.requests) == 1


def test_stale_cache_is_revalidated_with_304(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    url = serve_feed(http_server)
    feeds.fetch_feed(url, tmp_path, policy=LOOP)
    age_cache(tmp_path, 90000)
    assert feeds.fetch_feed(url, tmp_path, policy=LOOP) == RELEASES_JSON
    second = http_server.requests[1]
    assert second.headers["If-None-Match"] == ETAG
    assert second.headers["If-Modified-Since"] == LAST_MODIFIED
    meta = json.loads((tmp_path / feeds.META_FILE).read_text(encoding="utf-8"))
    assert time.time() - meta["fetched_at"] < 60
    assert feeds.fetch_feed(url, tmp_path, policy=LOOP) == RELEASES_JSON
    assert len(http_server.requests) == 2


def test_changed_feed_replaces_the_cache(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    url = serve_feed(http_server)
    feeds.fetch_feed(url, tmp_path, policy=LOOP)
    newer = '{"Assets": []}'
    serve_feed(http_server, newer, etag='"new"', last_modified=None)
    assert feeds.fetch_feed(url, tmp_path, max_age=0, policy=LOOP) == newer
    meta = json.loads((tmp_path / feeds.META_FILE).read_text(encoding="utf-8"))
    assert meta["etag"] == '"new"'
    assert meta["last_modified"] is None
    serve_feed(http_server, newer, etag=None, last_modified=None)
    feeds.fetch_feed(url, tmp_path, max_age=0, policy=LOOP)
    assert "If-Modified-Since" not in http_server.requests[-1].headers
    assert http_server.requests[-1].headers["If-None-Match"] == '"new"'
    assert feeds.fetch_feed(url, tmp_path, max_age=0, policy=LOOP) == newer
    assert "If-None-Match" not in http_server.requests[-1].headers


def test_cache_from_the_future_is_revalidated(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    url = serve_feed(http_server)
    feeds.fetch_feed(url, tmp_path, policy=LOOP)
    age_cache(tmp_path, -3600)
    feeds.fetch_feed(url, tmp_path, policy=LOOP)
    assert len(http_server.requests) == 2


def test_legacy_feed_bom_is_stripped(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/update/win/RELEASES", (FIXTURES / "RELEASES").read_bytes())
    text = feeds.fetch_feed(http_server.url("/update/win/RELEASES"), tmp_path, policy=LOOP)
    assert not text.startswith("\ufeff")
    assert len(feeds.parse_releases_legacy(text)) == 3


def test_offline_uses_cache_or_fails(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    url = serve_feed(http_server)
    with pytest.raises(DownloadFailed, match="offline") as info:
        feeds.fetch_feed(url, tmp_path, offline=True, policy=LOOP)
    assert "--offline" in info.value.hint
    feeds.fetch_feed(url, tmp_path, policy=LOOP)
    age_cache(tmp_path, 10**7)
    assert feeds.fetch_feed(url, tmp_path, offline=True, policy=LOOP) == RELEASES_JSON
    assert len(http_server.requests) == 1


def test_cache_for_another_url_is_a_miss(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    url = serve_feed(http_server)
    feeds.fetch_feed(url, tmp_path, policy=LOOP)
    http_server.add("/other", b"{}")
    assert feeds.fetch_feed(http_server.url("/other"), tmp_path, policy=LOOP) == "{}"


@pytest.mark.parametrize(
    "damage",
    ["meta-not-json", "meta-not-object", "feed-tampered", "feed-missing", "bad-timestamp", "bool-timestamp"],
)
def test_damaged_cache_is_a_miss(http_server: FakeHTTPServer, tmp_path: Path, damage: str) -> None:
    url = serve_feed(http_server)
    feeds.fetch_feed(url, tmp_path, policy=LOOP)
    meta_path, feed_path = tmp_path / feeds.META_FILE, tmp_path / feeds.FEED_FILE
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if damage == "meta-not-json":
        meta_path.write_text("{", encoding="utf-8")
    elif damage == "meta-not-object":
        meta_path.write_text("[]", encoding="utf-8")
    elif damage == "feed-tampered":
        feed_path.write_text('{"Assets": []}', encoding="utf-8")
    elif damage == "feed-missing":
        feed_path.unlink()
    else:
        meta["fetched_at"] = "yesterday" if damage == "bad-timestamp" else True
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(DownloadFailed):
        feeds.fetch_feed(url, tmp_path, offline=True, policy=LOOP)
    assert feeds.fetch_feed(url, tmp_path, policy=LOOP) == RELEASES_JSON
    assert "If-None-Match" not in http_server.requests[-1].headers


def test_http_errors(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    url = serve_feed(http_server, status=500)
    with pytest.raises(DownloadFailed, match="HTTP 500") as info:
        feeds.fetch_feed(url, tmp_path, policy=LOOP)
    assert info.value.exit_code == ExitCode.DOWNLOAD_FAILED
    assert not (tmp_path / feeds.FEED_FILE).exists()


def test_304_without_a_cache_is_an_error(tmp_path: Path) -> None:
    class NotModified:
        def open(self, request: Any, timeout: float = 0) -> Any:
            raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", {}, None)

    with pytest.raises(DownloadFailed, match="HTTP 304"):
        feeds.fetch_feed("https://git-fork.com/update/win/releases.win.json", tmp_path, opener=NotModified())


def test_network_errors(tmp_path: Path) -> None:
    class Refused:
        def open(self, request: Any, timeout: float = 0) -> Any:
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    with pytest.raises(DownloadFailed, match="Connection refused") as info:
        feeds.fetch_feed("https://git-fork.com/update/win/releases.win.json", tmp_path, opener=Refused())
    assert "network" in info.value.hint


def test_custom_opener_receives_conditional_request(tmp_path: Path) -> None:
    class Response:
        status = 200
        headers = {"ETag": '"x"'}

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self, amount: int) -> bytes:
            return b'{"Assets": []}'

    class Recorder:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        def open(self, request: Any, timeout: float = 0) -> Response:
            self.requests.append((request, timeout))
            return Response()

    opener = Recorder()
    url = "https://git-fork.com/update/win/releases.win.json"
    assert feeds.fetch_feed(url, tmp_path, opener=opener, timeout=3) == '{"Assets": []}'
    assert feeds.fetch_feed(url, tmp_path, opener=opener, max_age=0) == '{"Assets": []}'
    request, timeout = opener.requests[0]
    assert timeout == 3
    assert request.get_header("User-agent").startswith("fork-linux/")
    assert opener.requests[1][0].get_header("If-none-match") == '"x"'


def test_oversized_feed_is_refused_from_its_content_length(
    http_server: FakeHTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feeds, "FEED_MAX_BYTES", 100)
    url = serve_feed(http_server)
    with pytest.raises(IntegrityFailed, match=f"larger than 100 bytes \\({len(RELEASES_JSON)} announced\\)"):
        feeds.fetch_feed(url, tmp_path, policy=LOOP)
    assert not (tmp_path / feeds.FEED_FILE).exists()


def test_oversized_chunked_feed_is_refused_while_reading(
    http_server: FakeHTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feeds, "FEED_MAX_BYTES", 100)
    url = serve_feed(http_server, chunked=True)
    with pytest.raises(IntegrityFailed, match="larger than 100 bytes$"):
        feeds.fetch_feed(url, tmp_path, policy=LOOP)
    assert not (tmp_path / feeds.FEED_FILE).exists()


def test_truncated_feed_is_not_cached(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    url = serve_feed(http_server, content_length=len(RELEASES_JSON) + 50)
    with pytest.raises(DownloadFailed, match=f"closed after {len(RELEASES_JSON)} of {len(RELEASES_JSON) + 50} bytes") as info:
        feeds.fetch_feed(url, tmp_path, policy=LOOP)
    assert info.value.exit_code == ExitCode.DOWNLOAD_FAILED
    assert not (tmp_path / feeds.FEED_FILE).exists()


def test_truncated_chunked_feed_is_not_cached(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    url = serve_feed(http_server, chunked=True, truncate_after=500)
    with pytest.raises(DownloadFailed):
        feeds.fetch_feed(url, tmp_path, policy=LOOP)
    assert not (tmp_path / feeds.FEED_FILE).exists()


@pytest.mark.parametrize("which", ["feed", "meta"])
def test_oversized_cache_files_are_a_miss(
    http_server: FakeHTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    url = serve_feed(http_server)
    feeds.fetch_feed(url, tmp_path, policy=LOOP)
    if which == "feed":
        monkeypatch.setattr(feeds, "FEED_MAX_BYTES", 100)
        serve_feed(http_server, '{"Assets": []}')
        expected = '{"Assets": []}'
    else:
        meta_path = tmp_path / feeds.META_FILE
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["padding"] = "x" * feeds.META_MAX_BYTES
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        expected = RELEASES_JSON
    with pytest.raises(DownloadFailed, match="offline"):
        feeds.fetch_feed(url, tmp_path, offline=True, policy=LOOP)
    assert feeds.fetch_feed(url, tmp_path, policy=LOOP) == expected
    assert "If-None-Match" not in http_server.requests[-1].headers


def test_deeply_nested_meta_is_a_miss(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    url = serve_feed(http_server)
    feeds.fetch_feed(url, tmp_path, policy=LOOP)
    (tmp_path / feeds.META_FILE).write_text("[" * 50_000, encoding="utf-8")
    with pytest.raises(DownloadFailed, match="offline"):
        feeds.fetch_feed(url, tmp_path, offline=True, policy=LOOP)


def test_default_cap_is_five_megabytes() -> None:
    assert feeds.FEED_MAX_BYTES == 5 * 1024 * 1024


def test_non_utf8_feed(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    http_server.add("/feed", b"\xff\xfe\x00garbage")
    with pytest.raises(IntegrityFailed, match="not UTF-8"):
        feeds.fetch_feed(http_server.url("/feed"), tmp_path, policy=LOOP)


def test_validate_runs_before_caching(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    url = serve_feed(http_server, "<html>maintenance</html>")
    with pytest.raises(IntegrityFailed):
        feeds.fetch_feed(url, tmp_path, policy=LOOP, validate=feeds.parse_releases_json)
    assert not (tmp_path / feeds.FEED_FILE).exists()
    serve_feed(http_server)
    assert feeds.fetch_feed(url, tmp_path, policy=LOOP, validate=feeds.parse_releases_json) == RELEASES_JSON


def test_insecure_feed_url_is_refused(tmp_path: Path) -> None:
    with pytest.raises(IntegrityFailed, match="refusing"):
        feeds.fetch_feed("http://git-fork.com/update/win/releases.win.json", tmp_path)


def test_unwritable_cache_still_returns_the_feed(http_server: FakeHTTPServer, tmp_path: Path) -> None:
    url = serve_feed(http_server)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file", encoding="utf-8")
    assert feeds.fetch_feed(url, blocker, policy=LOOP) == RELEASES_JSON
    assert feeds.fetch_feed(url, blocker / "sub", policy=LOOP) == RELEASES_JSON


def test_failed_atomic_write_leaves_no_temp_files(
    http_server: FakeHTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = serve_feed(http_server)

    def broken_replace(src: str, dst: str) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(fsutil.os, "replace", broken_replace)
    assert feeds.fetch_feed(url, tmp_path, policy=LOOP) == RELEASES_JSON
    assert list(tmp_path.iterdir()) == []
