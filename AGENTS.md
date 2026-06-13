# Agent Instructions

Read `../AGENTS.md` first when it exists. `AGENTS.md` files are authoritative for all agents; `CLAUDE.md` is only a Claude Code adapter.

## Local Platform Scope

The local requester works on Windows only. Default to Windows commands, paths, setup steps, and testing instructions unless the user explicitly asks otherwise.

Keep the Linux sources in the repository. A collaborator is responsible for Linux testing, so do not remove, archive, or ignore Linux project files just because the local requester is Windows-only.

When changing shared behavior, update Windows first and only mirror changes into Linux files when the code path or project convention clearly requires it.

## Git Discipline

Commit after every major change and push the scoped commit to `origin/development`.

Stage only files changed for the current task. Do not stage unrelated work or local assistant state.

## Local Codex Desktop History Safety

Do not emit raw directive-shaped tokens like `::name{...}` in final answers or ordinary chat text. If such text must be discussed, break the prefix, for example `: :name{...}`, so Codex Desktop does not treat old conversation text as live renderer directive syntax.
