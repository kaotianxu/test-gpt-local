# gpt-local-code-operator

Local MCP Server for GPT-powered code operations on personal development machines.

## Architecture

```
ChatGPT (GPT reasoning + code generation)
    │ Secure MCP Tunnel (OpenAI-hosted)
    │ outbound HTTPS via local proxy
    ▼
tunnel-client
    │ loopback HTTP
    ▼
Local Code MCP Server (127.0.0.1:8765)
    │
    ├── Project Registry (config/projects.yaml)
    ├── Workspace Manager (detached Git worktrees)
    ├── Search / Read / Patch
    ├── PowerShell 7 Runner
    └── Git Inspector
```

## Prerequisites

- Python 3.12+
- PowerShell 7 (`pwsh.exe`)
- Git CLI
- ripgrep (`rg`)
- (Optional) OpenAI Secure MCP Tunnel access
- (Optional) Local HTTP proxy (e.g. Clash, v2ray) at `127.0.0.1:7897`

## Quick Start

```powershell
# 1. Install dependencies
pip install mcp[cli] pyyaml aiofile

# 2. Configure projects
# Edit config/projects.yaml to add your project paths

# 3. Start the MCP Server
.\scripts\start-mcp.ps1
```

The server starts on `http://127.0.0.1:8765` with health check at `/healthz`.

## Background Service (Recommended)

Phase 5 can run the MCP server and Secure MCP Tunnel under one user-level background
supervisor, so normal use does not require open terminal windows.

```powershell
# Diagnose the local runtime without exposing the tunnel key
.\scripts\doctor.ps1

# Install one hidden current-user Task Scheduler task and start it
.\scripts\install-service.ps1

# Inspect or control it
.\scripts\status-service.ps1
.\scripts\restart-service.ps1
.\scripts\stop-service.ps1

# Stop it and remove only the scheduled task
.\scripts\uninstall-service.ps1
```

The installer uses the current user with limited privileges. It removes the legacy
two-task layout, is safe to run again when upgrading, and preserves the database,
configuration, logs, and worktrees on uninstall. `start-mcp.ps1` and
`start-tunnel.ps1` remain available for foreground debugging.

Runtime status is stored atomically in `data/service/status.json`. Rotating supervisor,
MCP, and tunnel logs are written under `logs/`; runtime API-key values are redacted.

## Register a Project

Edit `config/projects.yaml`:

```yaml
projects:
  my-project:
    name: My Project
    repository: D:/Code/my-project
    worktree_root: D:/GPTWorktrees/my-project
    pwsh:
      executable: C:/Program Files/PowerShell/7/pwsh.exe
      default_timeout_seconds: 600
      max_timeout_seconds: 3600
      max_output_chars: 200000
      inherit_user_environment: true
```

## Tunnel Setup (Optional)

To connect ChatGPT to the local MCP Server:

1. Create an OpenAI Secure MCP Tunnel in the [OpenAI Platform](https://platform.openai.com)
2. Generate a runtime API key
3. Initialize the tunnel profile:

```powershell
$env:CONTROL_PLANE_API_KEY = "<runtime-api-key>"
$env:CONTROL_PLANE_HTTP_PROXY = "http://127.0.0.1:7897"
$env:NO_PROXY = "127.0.0.1,localhost,::1"

tunnel-client init `
  --profile local-code-operator `
  --tunnel-id tunnel_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx `
  --mcp-server-url http://127.0.0.1:8765/mcp
```

4. Start the tunnel:

```powershell
.\scripts\start-tunnel.ps1
```

5. In ChatGPT, enable Developer Mode, create a Tunnel App, and connect using the tunnel ID.

## MCP Tools

| Tool | Description |
|---|---|
| `ping` | Check server connectivity |
| `list_projects` | List registered projects |
| `create_workspace` | Create detached Git worktree |
| `get_workspace` | Get workspace info |
| `list_workspaces` | List all workspaces |
| `discard_workspace` | Delete worktree |
| `get_repo_map` | Directory overview |
| `search_code` | ripgrep search |
| `read_files` | Read file segments |
| `apply_patch` | Apply unified diff |
| `run_pwsh` | Execute PowerShell 7 |
| `run_check` | Run configured checks |
| `get_process_result` | Get async process output |
| `cancel_process` | Terminate process tree |
| `git_status` | Git status |
| `git_diff` | Git diff |

## Configuration

- `config/operator.yaml` — Server, proxy, process, and logging settings
- `config/projects.yaml` — Registered project definitions

## Project Structure

```
├── app/
│   ├── server.py          # FastMCP server entry point
│   ├── config.py          # Configuration loader
│   ├── tools/             # MCP tool implementations
│   ├── services/          # Business logic services
│   └── storage/           # SQLite state store
├── config/
│   ├── operator.yaml      # Server configuration
│   └── projects.yaml      # Project registry
├── scripts/
│   ├── start-mcp.ps1      # Start MCP server
│   ├── start-tunnel.ps1   # Start tunnel-client
│   ├── install-service.ps1
│   ├── start-service.ps1
│   ├── stop-service.ps1
│   ├── restart-service.ps1
│   ├── status-service.ps1
│   ├── doctor.ps1
│   └── uninstall-service.ps1
├── data/                  # SQLite database (gitignored)
├── logs/                  # Log files (gitignored)
└── pyproject.toml
```

## Architecture Principles

- **GPT is the Agent** — code reasoning, generation, and planning
- **MCP is the Local Interface** — search, read, patch, execute
- **Secure MCP Tunnel** — encrypted outbound-only connection
- **Git Worktree** — version isolation per task
- **PowerShell 7** — universal execution environment
- **Global Proxy** — unified outbound access

## Security

This is a **personal use** tool. It runs on your local machine with your user
permissions. Only register projects you trust.

- MCP Server binds only to `127.0.0.1`
- No automatic commit, merge, push, or deploy
- Commands have timeouts and process tree termination
- Each task uses an isolated Git worktree

## License

MIT
