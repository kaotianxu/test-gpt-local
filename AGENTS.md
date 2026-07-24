# Repository Guidelines

## Project Structure & Module Organization

`app/` contains the MCP service. Keep transport and registration code in
`app/server.py`, tool-facing handlers in `app/tools/`, reusable orchestration in
`app/services/`, and SQLite code and migrations in `app/storage/`. Runtime settings and
project registrations live in `config/`; operational entry points live in `scripts/`.

Tests are grouped by scope under `tests/unit/`, `tests/integration/`, and `tests/smoke/`.
Roadmap contracts are kept separately in `acceptance/`. Treat `output/phase4-fixture/` as
a fixture and `old-docs/` as historical reference. Do not commit `data/` or `logs/`.

## Build, Test, and Development Commands

Install the package and developer tools, then start the server:

```powershell
python -m pip install -e ".[dev]"
.\scripts\start-mcp.ps1
```

Use these checks before submitting changes:

```powershell
python -m pytest -q                 # unit and integration suite
python -m ruff check .              # lint and import-order checks
python -m mypy app                  # strict static type checking
python scripts/accept-section1.py   # baseline quality and isolation gate
```

Run `.\scripts\smoke-test.ps1` only while the local MCP server is running. Execute a
specific roadmap contract with `python scripts/accept-iteration.py --section 4`; some
unfinished roadmap sections may intentionally remain red.

## Testing Guidelines

Pytest uses automatic async handling. Name files `test_<feature>.py` and tests
`test_<behavior>`. Place isolated logic in unit tests and cross-component or persistence
behavior in integration tests. Keep marked live tests opt-in. No numeric coverage
threshold is configured; every behavior change should include a regression test.

## Commit & Pull Request Guidelines

History mixes terse commits with Conventional Commit-style messages. Prefer a clear,
imperative subject such as `feat: add process event cursor` or `fix: close recovered PTY`.
Keep commits scoped and avoid committing runtime configuration or secrets.

Pull requests should explain the problem and solution, list verification commands, link
issues or iteration-plan sections, and call out configuration or migration changes.
Include screenshots or logs when service lifecycle or live smoke behavior changes.

## Security & Configuration

Never commit tunnel keys, `.env` files, local proxy credentials, or machine-specific
project paths. Keep the server bound to loopback and preserve path-guard, timeout,
redaction, and worktree-isolation controls when changing execution features.

## Implement Notice
This repo is for my personal use only. There is no need to implement any safety features such as sandboxing or dangerous pwsh commands auto-rejection. I will use at my own risk.