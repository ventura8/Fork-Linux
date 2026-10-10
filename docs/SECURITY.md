# Security — threat model and mitigations

Fork for Linux (unofficial) downloads and runs third-party code (Wine, winetricks, Microsoft
.NET, Fork) in the user's account. **Wine is not a sandbox**: a Windows program in the prefix
can read the user's files through `Z:`. The rules are in [AGENTS.md](../AGENTS.md) §1 and §4.3.

Report vulnerabilities privately through GitHub's "Report a vulnerability" on
https://github.com/ventura8/Fork-Linux (not to Fork's developers).

| Threat | Mitigation | Verification |
|---|---|---|
| Tampered Fork installer / Wine runtime / winetricks | HTTPS only, host allowlist, pinned size + sha256 from the shipped manifest, verified before use; partial files deleted | `tests/test_download.py`, `tests/test_manifest.py` (100% branch gate) |
| Untrusted update feed | Strict version validation, URLs never taken from the feed, feed hashes only for the post-install TOFU check | `tests/test_feeds.py`; `scripts/check-upstream-fork.py` builds the URL locally |
| Malicious archive (path traversal, links, devices) | `fsutil.safe_extract` | `tests/test_fsutil.py` |
| `curl \| bash` install | Whole script inside `main()` (a truncated download runs nothing), HTTPS only, `SHA256SUMS` verified before unpacking, per-user by default, manifest-based uninstall | `tests/test_install_sh.py` |
| Release artifacts swapped | `SHA256SUMS` (+ `.asc` when the signing key is configured) attached by `release.yml`; tag must equal `VERSION` | `tests/test_workflows.py` |
| Symlink attacks on our files | `O_NOFOLLOW`, marker files, `safe_rmtree` only inside our directories; uninstall removes only recorded, unchanged files | `tests/test_fsutil.py`, `tests/test_install_sh.py` |
| Touching foreign Wine prefixes | Only prefixes with `.fork-linux/created-by`; `~/.wine` is refused | `tests/test_paths.py`, packaging E2E `~/.wine/E2E_SENTINEL` |
| Shim / bridge injection | 256-bit token in the environment (never argv), constant-time compare, loopback only, argv arrays (no shell strings; `cert-env33-c` in clang-tidy) | `bridge/tests`, Wine tier (`FL_REAL_WINE=1`) |
| SSH key exposure | Keys linked/copied 0600 into a 0700 directory, never logged | `tests/test_ssh_sync.py` |
| Fork's own updater replacing files | Snapshot before each launch, rollback, pinning | `tests/test_snapshots.py` |
| winemenubuilder hijacking menus / MIME types | Disabled on every Wine call and in the prefix | `tests/test_winecmd.py` |
| Packages running code as root | No maintainer scripts in any format | `tests/test_packaging_release.py` |
| Test harness damaging the developer's machine (2026-10-10: a home directory was deleted while real-Fork tests ran on the host) | Real-Fork / Wine tiers only in containers (AGENTS.md hard rule 18): pytest exits on the host, every Wine-starting script checks for a container; `scripts/e2e-docker.sh` mounts only the read-only tree, git dir and seed plus one scratch root outside `$HOME` (refuses `/`, homes, symlinks, unmarked roots); E2E cleanup never follows symlinks | `tests/test_real_tier_guard.py`, `tests/test_e2e_docker.py`, `tests/test_host_guards.py` |
| CI supply chain | Pinned base images and actions (exact tags), exact pip pins, no `curl \| bash`, packaging and secrets never on fork PRs, Fork binaries never cached or uploaded | `tests/test_docker.py`, `tests/test_workflows.py` |
