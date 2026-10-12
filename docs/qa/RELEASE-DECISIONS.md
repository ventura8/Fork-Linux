# Maintainer decisions for v1.0.0

Decisions the maintainer made on 2026-10-11 for questions the build and review agents could not settle
themselves. Treat them as settled: don't raise them again as open questions. Where a decision asks for a
change, the change is part of v1.0.0.

| Topic | Decision |
|---|---|
| Version | Ship everything as **1.0.0**. v1.0.0 was never tagged or published. Text that mentions 1.0.1, "the NUC build", or an earlier release users could have is wrong. |
| Stale Wine / Fork pin after an upgrade | `doctor` `env.manifest` **warns only**. A deliberate user pin is legitimate. |
| Commit scopes | `e2e` and `e2e-de` are valid scopes (AGENTS.md §6). History is not rewritten. |
| Flaky desktop-tier UI spots (context menus, Fork's close button) | Keep the **logged retries**. Root-causing XTEST key loss isn't required for 1.0.0. |
| `update --fork` and the user's `AppData\Local\Fork` entries | **Accepted**: around the official installer, the user's own entries are moved aside and back unchanged, never read, copied or edited (AGENTS.md hard rule 3 wording). |
| Hand-off without a systemd user manager | **Fix for 1.0.0**: when there is no systemd user session, terminals and default applications are started by a process outside Wine's process tree (our bridge daemon or another host-side starter), so they never inherit Wine's NoNewPrivs / seccomp. |
| Noto fonts | Packages **recommend** Noto (core + CJK) wherever the format supports weak dependencies. doctor keeps its install hint for systems without them. |
| Damaged composite-font copy | Patch the copy WPF **reads** for the installed .NET version. A damaged copy that version never reads is only reported by doctor; it doesn't block the fix. |
| Install channels in README / release notes (2026-10-12) | List only the channels that are **live at v1.0.0**: the GitHub Release (deb, rpm, AppImage, Flatpak, tarball, `install.sh`) and the PPA `ppa:ventura8/fork-linux`. The AUR and the Snap Store move to a "coming later" note. Remove "the first release is in preparation". |
| Desktop theme unknown with `theme=follow` (2026-10-12) | **Hand control back to Fork**: when no desktop probe answers, turn Fork's own "follow system theme" back on instead of keeping the theme fork-linux last forced. |
| Update scenarios covered only by unit tests (2026-10-12) | **Real-Fork tests for the user-facing ones**: F8 offline update check, F12 pin warning, F13 update guards, F21 menu-launch notice and error dialog, A13 and A14 `install.sh`, A20 bad manifest override, plus a registry scenario for rendering non-Latin text (FF-NON-LATIN). The internal ones (integrity, pruning and similar) are unit-only by design and are listed in the registry as such. |
| Velopack "OS Error 731" in CI (2026-10-12) | CI **re-runs a real-tier module once** when Fork's own updater fails with "Failed to start application (OS Error 731)", and logs that it did. |
| Wine's "Git Error" (GLE 731) on busy repositories (2026-10-12) | The release notes and troubleshooting docs **point to the experimental git bridge** as the way to avoid it, besides "repeat the action". |
| ssh-agent with Fork's bundled ssh (2026-10-12) | **Build it for 1.0.0**: an msys-socket relay so Fork's bundled ssh can use the user's ssh-agent without the git bridge. |
