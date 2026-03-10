# AGENTS.md-aware workflow notes

This repository follows the root-level `AGENTS.md` operational pattern.

## Workflow rules

- Read `/Users/JuiceKing/Desktop/Codexapp/AGENTS.md` before making non-trivial changes.
- If a task clearly maps to a listed skill, use that skill flow first.
- Keep changes minimal, explicit, and auditable.
- Prefer `rg` for search and narrow file reads.
- Preserve read-only safety posture for diagnostics; do not add undocumented or control/write routines.

## Safe contribution checklist

- Confirm new OBD commands are documented and read-only.
- Confirm no replay/security/programming/write APIs are exposed.
- Confirm README and `.env.example` stay aligned with code.
- Confirm FastAPI routes fail closed when safety checks reject commands.
