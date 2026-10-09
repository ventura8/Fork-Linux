## What and why

<!-- What changes, and the problem it solves. Link an issue if there is one. -->

## How it was verified

<!-- Commands you ran and what they showed. "CI is green" alone is not enough
     for changes to setup, the Wine prefix, the bridge, or packaging. -->

## Checklist

See [AGENTS.md](../AGENTS.md) §1 (hard rules) and §4.5–4.9 for what each item means.

- [ ] Touched code passes the lint and tests for its type — Python: `py_compile` + pytest (also on Python 3.10); shell: `shellcheck`; C: meson build + `meson test` + clang-tidy; workflows: `actionlint`
- [ ] No suppressions (`# noqa`, `# type: ignore`, `NOSONAR`, `# pragma: no cover`, `NOLINT`, `# shellcheck disable`, `skip`/`xfail` markers, lintian overrides) — `scripts/no-suppressions-lint.py`
- [ ] No Fork binaries or assets: nothing from Fork (installer, `Fork.exe`, DLLs, nupkgs, logo, icon, screenshots) is added, mirrored, patched or uploaded
- [ ] Fork is still downloaded only from `https://cdn.fork.dev/win/` and every download is sha256-verified
- [ ] `~/.wine` and prefixes we did not create are never touched
- [ ] Credits intact: the not-affiliated disclaimer, the Fork developers' credits and the buy link (https://git-fork.com/buy) are unchanged or improved
- [ ] Agent docs updated where behaviour, paths or workflows changed (`AGENTS.md`, `.agents/skills/`, `docs/`)
- [ ] Touches setup, the bridge or packaging? The relevant real-Wine / packaging E2E tier ran (or say why not)
