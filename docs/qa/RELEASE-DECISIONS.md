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
