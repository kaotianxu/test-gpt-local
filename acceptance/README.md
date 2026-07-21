# Iteration acceptance gates

These checks turn `iteration-plan.md` Sections 1–9 into executable contracts.
They live outside the normal `tests/` tree so unfinished roadmap sections do
not make the established unit/integration suite red.

Run every section:

```powershell
python scripts/accept-iteration.py --section all
```

Run one section:

```powershell
python scripts/accept-iteration.py --section 4
```

The same gates are registered in `config/projects.yaml` as
`section1_acceptance` through `section9_acceptance`, plus
`iteration_acceptance` for the complete set.

| Section | Executable acceptance contract |
|---|---|
| 1 | Ruff, strict mypy, full non-live tests in randomized order, then four process-isolated parallel shards |
| 2 | Central `ToolSpec` registry and middleware; stable errors; package-derived version; complete idempotency input |
| 3 | Keyed fair scheduler, resource/queue policy, recovery API, and process-identity persistence |
| 4 | Symbols, definitions, references, implementations, call hierarchy, diagnostics, and changed-symbol tools |
| 5 | Append-only event persistence, opaque cursor retrieval, and process subscription |
| 6 | Begin, stage, validate, commit, and rollback operations for atomic multi-file change sets |
| 7 | Typed plan steps/evidence plus dependency-aware parsed check graphs |
| 8 | Streaming file hashes/ranges, seek-based output, search cursors/timeouts, and artifact hash caching |
| 9 | Portable local/example config, versioned SQL migrations, schema tracking, and dependency locking |

Section 1 is currently green. Sections 2–9 are expected to remain red until
their roadmap implementation lands; each failure names the missing contract.
