# Recovery material (2026-10-10 data loss)

Not for merging. On 2026-10-10 at about 20:18 EEST the developer's home directory was
deleted while agents were running tests, including the local repository with ~20 unpushed
fix branches. This branch preserves what survived:

- `src/fork_linux/` and `README.md`: copied from the fork-linux 1.0.0 per-user install on the
  NUC (`~/.local/opt/fork-linux`, built from the lost `fix/nuc-integration` branch on
  2026-10-10 08:06). Source only: the matching tests, docs, CI, packaging and bridge C
  changes were not installed and are lost.
- `fl-bridge-helper.strings.txt`: printable strings of the NUC's compiled bridge helper.
  It also serves `fl-askpass`, `fl-ssh-askpass` and `fl-winexec` (symlinks), whose C source
  is lost. `nuc-binaries.sha256` identifies the binaries (kept outside git).
- `*.spec.js` / `*.result.json` / `*.journal.jsonl`: the four agent workflows that were
  running at the time (update-scenario tests, per-file coverage gate + E2E scenario
  registry, desktop-environment E2E matrix, .NET 4.7.2 font fallback): their task specs
  and final reports, which describe the lost changes.
