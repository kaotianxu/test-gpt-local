# QA Log

> Conceptual / learning Q&A captured from this project. Reverse chronological order.

## 2026-07-14 — Claude Code statusLine on Windows + claude-hud

**My question (verbatim):**
> 查一下claude code的config，为什么我这段状态显示没用了：`statusLine.command` 是 bash 风格的 `case/esac/stty/ls` + `/c/Program Files/nodejs/node.exe`。

**Core answer:**

Two compounding root causes — the symptom is "statusLine shows nothing" but the harness is silently swallowing the failure:

1. **Shell mismatch.** Claude Code 2.1.x switched the Windows default shell for statusLine execution to **PowerShell 7** (not Git Bash). The inline `statusLine.command` is bash syntax (`case ... esac`, `stty size </dev/tty`, `awk`, `ls -1d`, `sort -V`, command-substitution `$()`). When run by PowerShell, the parser fails immediately on `case` with `表达式语句中缺少")"` — the process exits before `exec node` ever runs, so stdout is empty and Claude Code renders nothing. Reproduced locally: `powershell -Command "& { ... case ... esac ... }"` → parse error, no node invocation.

2. **Hardcoded wrong node path.** The command hardcodes `/c/Program Files/nodejs/node.exe`, but this machine uses **nvm4w** — actual `node.exe` is at `C:\nvm4w\nodejs\node.exe`. Even if bash were available, `exec` would fail silently on a missing path. (`where node.exe` confirms.)

Plus one trap baked into older claude-hud templates: the PowerShell `Get-ChildItem 'plugins\cache\*\claude-hud'` glob is missing the trailing `\*`. Without it, the cmdlet matches the `claude-hud` directory itself; the version regex `^\d+(\.\d+)+$` then filters it out, `$pluginDir` becomes `$null`, and the subsequent `Join-Path` throws `Cannot bind argument to parameter 'Path' because it is null`. Newer setup fixed this; inline hand-written commands are still vulnerable.

**Fix (per claude-hud PR #538):** split the command into a standalone `statusline.ps1` wrapper called by a one-liner. The wrapper is plain PowerShell, written **UTF-8 without BOM** (PS 5.1's `Set-Content -Encoding UTF8` writes a BOM, which would corrupt settings.json — use `[System.IO.File]::WriteAllText` with `New-Object System.Text.UTF8Encoding $false`). Wrapper body:

```powershell
try { $w = [Console]::WindowWidth } catch { $w = 120 }   # Claude Code spawns with no console handle on Windows → IO exception
$env:COLUMNS = [Math]::Max(1, $w - 4)
$claudeDir = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME '.claude' }
$pluginDir = (Get-ChildItem (Join-Path $claudeDir 'plugins\cache\*\claude-hud\*') -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^\d+(\.\d+)+$' } |
    Sort-Object { [version]$_.Name } -Descending | Select-Object -First 1).FullName
if (-not $pluginDir) { exit 0 }
& 'C:\nvm4w\nodejs\node.exe' (Join-Path $pluginDir 'dist\index.js')
```

`settings.json` `statusLine.command` becomes one line:

```json
"powershell -NoProfile -ExecutionPolicy Bypass -File \"C:\\Users\\20906\\.claude\\plugins\\claude-hud\\statusline.ps1\""
```

Sanity check: `echo '{}' | powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\20906\.claude\plugins\claude-hud\statusline.ps1"` should print `[claude-hud] Initializing...`. Then restart Claude Code.

**Lazy alternative:** re-run the in-repo `claude-hud:setup` skill — v0.1.1 already includes the PR #538 fix and will lay down the wrapper + correct `statusLine` line for you.

**Relevant citations:**
- [jarrodwatts/claude-hud Issue #521 — statusLine not working on Windows after Claude Code 2.1.x switched default shell to PowerShell 7](https://github.com/jarrodwatts/claude-hud/issues/521)
- [jarrodwatts/claude-hud PR #538 — fix(setup): generate PowerShell wrapper with try/catch + corrected version-dir glob](https://github.com/jarrodwatts/claude-hud/pull/538) (merged fix)
- Related closed issues called out in #521: #326 (Git Bash on Windows), #196 (HUD disappears after restart), #121 (setup generates broken command on win32)

**How to apply:** When writing any Claude Code `settings.json` `statusLine` / hook on Windows: assume **PowerShell 7 is the runner**, not bash. Inline multi-line shell logic in a JSON string is fragile (quoting, BOM, no console handle for `[Console]::WindowWidth`); prefer a sidecar `.ps1` script + one-line `powershell -NoProfile -ExecutionPolicy Bypass -File "..."` invocation. Always resolve `node` via `(Get-Command node).Source` rather than hardcoding — nvm4w, scoop, and chocolatey put it in different places.

---

## 2026-07-14 — `/claude-hud:setup` ran end-to-end (resolves the previous question)

**What we did:**
1. **Detection** (Step 0+1): `Platform: win32` + `$OSTYPE: msys` + `SHELL: /bin/bash.exe` → setup doc table routes this to the **Windows + Git Bash branch** (not PowerShell wrapper). My earlier PowerShell-wrapper suggestion was the wrong branch for this machine. Lesson: `OSTYPE=msys` is the authoritative signal, even if Claude Code's primary shell is PowerShell.
2. **Node path**: `command -v node` → `/c/nvm4w/nodejs/node.exe` (not `/c/Program Files/nodejs/`). The previous config's hardcoded `/c/Program Files/nodejs/node.exe` was the actual cause of "HUD silent" — `exec` on a missing path returns nothing.
3. **Step 2.5 backup**: `settings.json.bak.20260714-131343` + saved previous command to `~/.claude/plugins/claude-hud/previous-statusline.txt` (mode 0600). Existing command contained "claude-hud" → classified as **Reinstall (own config)**, no user prompt needed.
4. **Step 3 merge**: Replaced `statusLine.command` with the Git Bash template, `{RUNTIME_PATH}` → `/c/nvm4w/nodejs/node.exe`, `{SOURCE}` → `dist/index.js`. Wrote UTF-8 without BOM (verified first 4 bytes = `7B 0A 20 20` = `{` + LF + indent, no `EF BB BF`).
5. **Step 2 test**: `bash -c <command>` with `echo '{}' |` stdin → exit 0, HUD rendered:
   ```
   [Unknown] │ token消耗
   Context ░░░░░░░░░░ 0%
   1 CLAUDE.md
   ```
6. **Step 4 optional features**: User picked Tools activity, Agents & Todos, Session info + name, and a custom line "token消耗" (via Other). Most were already `true` in existing `config.json`; only `display.customLine: "token消耗"` was a real new key. Did not flip any `false` → `false`.

**Files changed:**
- `C:\Users\20906\.claude\settings.json` — statusLine.command updated
- `C:\Users\20906\.claude\settings.json.bak.20260714-131343` — backup
- `C:\Users\20906\.claude\plugins\claude-hud\previous-statusline.txt` — old command
- `C:\Users\20906\.claude\plugins\claude-hud\config.json` — added `display.customLine: "token消耗"`
- `C:\Users\20906\OneDrive - University of Illinois - Urbana\文档\test-gpt-local\scripts\claude-hud-apply.cjs` — helper script (re-runnable)
- `C:\Users\20906\OneDrive - University of Illinois - Urbana\文档\test-gpt-local\scripts\claude-hud-merge-config.cjs` — helper script (re-runnable)

**Next step the user must do:** `/exit` Claude Code and run `claude` again — statusLine changes only take effect after a full restart. The HUD should appear below the input field showing `[model] │ token消耗` on line 1 and the context bar on line 2.

**Caveat to surface to user:** "token消耗" as a *customLine* is a static string — it always displays that literal text, not live token data. If they actually wanted live token-usage display, the right keys are `display.showUsage: true` and/or `display.showSessionTokens: true` (both currently visible: `showUsage: false`, `showSessionTokens: true`).
