# Skills (moved)

Fork for Linux (unofficial) agent skills live under **[`.agents/skills/`](.agents/skills/)** — one directory per skill with a `SKILL.md` runbook.

Canonical agent rules: [AGENTS.md](AGENTS.md). Setup guide: [docs/INSTRUCTIONS.md](docs/INSTRUCTIONS.md).

| Skill | Purpose |
|-------|---------|
| [pipeline-runner](.agents/skills/pipeline-runner/SKILL.md) | Full local gate: lint → coverage → bridge → compat ×7 → packaging ×9; fix until green |
| [test-runner](.agents/skills/test-runner/SKILL.md) | pytest unit + contract tests on the host; `FL_REAL_WINE=1` bridge tier and `FL_E2E_FORK=1` end-to-end tier in containers only |
| [e2e-docker](.agents/skills/e2e-docker/SKILL.md) | Real-Fork / Wine work (E2E tier, xdotool, strace, QA, spikes) — only via `scripts/e2e-docker.sh` (hard rule 18) |
| [ci-docker-matrix](.agents/skills/ci-docker-matrix/SKILL.md) | Docker CI stages and per-distro compat cells (`docker/Dockerfile.ci*`) |
| [release](.agents/skills/release/SKILL.md) | Release notes from **all** branch changes, `VERSION` bump, `release: vX.Y.Z - <Title>` commit |
| [release-packaging](.agents/skills/release-packaging/SKILL.md) | Local multi-format builds (deb / rpm / arch / snap / AppImage / Flatpak / tarball) |
| [installer-tester](.agents/skills/installer-tester/SKILL.md) | `install.sh` / `uninstall.sh` tests |
| [resolve-pr-comments](.agents/skills/resolve-pr-comments/SKILL.md) | Resolve PR review threads |
| [review-with-coderabbit](.agents/skills/review-with-coderabbit/SKILL.md) | CodeRabbit review (user-gated) |
| [troubleshoot](.agents/skills/troubleshoot/SKILL.md) | Setup / launch failures, logs, Wine debug knobs |
| [diagnostics](.agents/skills/diagnostics/SKILL.md) | `fork-linux doctor` checks, `--fix`, `--json` |
| [wine-runtime-bump](.agents/skills/wine-runtime-bump/SKILL.md) | Bump the pinned Kron4ek Wine build / winetricks in the runtime manifest |
| [fork-version-bump](.agents/skills/fork-version-bump/SKILL.md) | Mark a new Fork version known-good (installer sha256 + E2E evidence) |
| [git-bridge](.agents/skills/git-bridge/SKILL.md) | Experimental native-git bridge: shims, helper, record mode |
| [desktop-integration](.agents/skills/desktop-integration/SKILL.md) | `.desktop`, metainfo, icon extraction, file-manager "Open in Fork" |
| [legal-guardrails](.agents/skills/legal-guardrails/SKILL.md) | Fork EULA, branding, credits and no-redistribution rules |
