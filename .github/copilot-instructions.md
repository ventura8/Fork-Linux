# GitHub Copilot — Fork for Linux (unofficial)

Canonical agent rules live in **[AGENTS.md](../AGENTS.md)** at the repository root. Follow that document for the hard rules (never redistribute or patch Fork, never touch `~/.wine`, always verify sha256), architecture, coding standards, CI/Docker matrix rules, exit codes, and documentation sync.

Additional context (do not fork rules into this file):

- [docs/INSTRUCTIONS.md](../docs/INSTRUCTIONS.md) — setup and contribution (planned)
- [docs/architecture/README.md](../docs/architecture/README.md) — system design (planned)
- [docs/SECURITY.md](../docs/SECURITY.md) — security practices (planned)
- [docs/CREDITS.md](../docs/CREDITS.md) — credits and third-party components
- [`.agents/skills/`](../.agents/skills/) — task-specific runbooks (planned)

When changing behavior, update `AGENTS.md` and affected skills in the same change set. Lint and test new or changed code per `AGENTS.md` §4.5. Use `logs/` for agent progress output.
