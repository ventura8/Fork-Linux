# Gemini CLI — Fork for Linux (unofficial)

Canonical agent rules for this repository: **[AGENTS.md](AGENTS.md)**.

This file is a thin project entrypoint for Gemini CLI context. Prefer `AGENTS.md` over duplicating rules here.

Also load as needed:

- [docs/INSTRUCTIONS.md](docs/INSTRUCTIONS.md) (planned)
- [docs/architecture/README.md](docs/architecture/README.md) (planned)
- [docs/SECURITY.md](docs/SECURITY.md) (planned)
- [docs/CREDITS.md](docs/CREDITS.md)
- [`.agents/skills/`](.agents/skills/) (planned)

Optional import (if using Gemini `@` imports in your local setup):

```text
@./AGENTS.md
```

Progress logs: `logs/`. Lint and test new or changed code per `AGENTS.md` §4.5. Sync agent docs with code per `AGENTS.md` §4.7. Baseline OS for CI/docs: Ubuntu **26.04**; oldest supported: Ubuntu **22.04** (Python 3.10).
