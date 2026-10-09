# Skills (moved)

Fork for Linux (unofficial) agent skills live under **[`.agents/skills/`](.agents/skills/)** (planned) — one directory per skill with a `SKILL.md` runbook.

Canonical agent rules: [AGENTS.md](AGENTS.md). Setup guide: [docs/INSTRUCTIONS.md](docs/INSTRUCTIONS.md) (planned).

Skills marked **(planned)** have no `SKILL.md` yet; drop the marker when you add one (AGENTS.md §4.7).

| Skill | Purpose |
|-------|---------|
| [pipeline-runner](.agents/skills/pipeline-runner/SKILL.md) (planned) | Full local gate: lint → coverage → bridge → compat ×7 → packaging ×9; fix until green |
| [test-runner](.agents/skills/test-runner/SKILL.md) (planned) | pytest unit + contract tests, `FL_REAL_WINE=1` bridge tier, `FL_E2E_FORK=1` end-to-end tier |
| [ci-docker-matrix](.agents/skills/ci-docker-matrix/SKILL.md) (planned) | Docker CI stages and per-distro compat cells (`docker/Dockerfile.ci*`) |
| [release](.agents/skills/release/SKILL.md) (planned) | Release notes from **all** branch changes, `VERSION` bump, `release: vX.Y.Z - <Title>` commit |
| [release-packaging](.agents/skills/release-packaging/SKILL.md) (planned) | Local multi-format builds (deb / rpm / arch / snap / AppImage / Flatpak / tarball) |
| [installer-tester](.agents/skills/installer-tester/SKILL.md) (planned) | `install.sh` / `uninstall.sh` tests |
| [resolve-pr-comments](.agents/skills/resolve-pr-comments/SKILL.md) (planned) | Resolve PR review threads |
| [review-with-coderabbit](.agents/skills/review-with-coderabbit/SKILL.md) (planned) | CodeRabbit review (user-gated) |
| [troubleshoot](.agents/skills/troubleshoot/SKILL.md) (planned) | Setup / launch failures, logs, Wine debug knobs |
| [diagnostics](.agents/skills/diagnostics/SKILL.md) (planned) | `fork-linux doctor` checks, `--fix`, `--json` |
| [wine-runtime-bump](.agents/skills/wine-runtime-bump/SKILL.md) (planned) | Bump the pinned Kron4ek Wine build / winetricks in the runtime manifest |
| [fork-version-bump](.agents/skills/fork-version-bump/SKILL.md) (planned) | Mark a new Fork version known-good (installer sha256 + E2E evidence) |
| [git-bridge](.agents/skills/git-bridge/SKILL.md) (planned) | Experimental native-git bridge: shims, helper, record mode |
| [desktop-integration](.agents/skills/desktop-integration/SKILL.md) (planned) | `.desktop`, metainfo, icon extraction, file-manager "Open in Fork" |
| [legal-guardrails](.agents/skills/legal-guardrails/SKILL.md) (planned) | Fork EULA, branding, credits and no-redistribution rules |
