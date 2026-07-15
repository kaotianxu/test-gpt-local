
## P0：优先实施

### 1. 为所有工具统一结构化返回格式

不要让工具自由返回大段文本。统一使用 envelope：

```json
{
  "ok": true,
  "request_id": "req_123",
  "workspace_id": "ws_ab12",
  "revision": 7,
  "result": {},
  "warnings": [],
  "truncated": false,
  "next_cursor": null
}
```

错误统一为：

```json
{
  "ok": false,
  "error": {
    "code": "PATCH_CONFLICT",
    "message": "Target file changed since it was read.",
    "retryable": true,
    "suggested_next_tool": "read_files"
  }
}
```

推荐稳定错误码：

```text
WORKSPACE_NOT_FOUND
STALE_WORKSPACE
FILE_CHANGED
PATCH_CONFLICT
PATH_DENIED
PROCESS_TIMEOUT
PROCESS_CANCELLED
OUTPUT_TRUNCATED
CHECK_FAILED
TOOL_RETRYABLE
```

这样 ChatGPT 更容易自动恢复，而不是根据自然语言猜测下一步。

---

### 2. 加入乐观并发控制

目前最危险的问题之一是：

```text
GPT 读取文件
→ 过了一段时间
→ 文件被其他进程修改
→ GPT 根据旧内容应用 patch
```

建议 `read_files` 返回文件 hash：

```json
{
  "path": "src/service.py",
  "sha256": "83ac...",
  "content": "..."
}
```

`apply_patch` 要求：

```json
{
  "workspace_id": "ws_ab12",
  "patch": "...",
  "expected_head": "d13f...",
  "expected_files": {
    "src/service.py": "83ac..."
  },
  "idempotency_key": "patch-turn-14"
}
```

若 HEAD 或文件 hash 不匹配，拒绝补丁并返回 `FILE_CHANGED`。这相当于把 Codex 内部较紧密的 turn 状态，转化为适合远程 MCP 的显式一致性协议。

---

### 3. 大幅减少工具调用次数

远程 MCP 最需要优化的是 **round-trip 数量**。

改进工具：

```text
read_files
- 支持一次读取多个文件
- 支持每个文件独立 line range
- 返回 hash
- 返回 import / symbol 摘要

search_code
- 一次接受多个 query
- 支持 filename、literal、regex、symbol 模式
- 支持按文件聚合
- 支持 cursor 分页

git_diff
- 支持 paths
- 支持 context_lines
- 支持 stat_only
- 支持 staged / unstaged
```

例如不要执行：

```text
read_file A
read_file B
read_file C
```

而应该执行：

```json
{
  "files": [
    {"path": "a.py", "start": 1, "end": 200},
    {"path": "b.py", "start": 40, "end": 160},
    {"path": "c.py", "start": 1, "end": 120}
  ]
}
```

---

### 4. 将长输出存成 artifact，而不是全部返回模型

`run_pwsh` 和 `run_check` 建议返回：

```json
{
  "exit_code": 1,
  "duration_ms": 18342,
  "stdout_tail": "...最后 100 行...",
  "stderr_tail": "...最后 100 行...",
  "output_artifact_id": "proc_123_full_log",
  "summary": {
    "tests_passed": 84,
    "tests_failed": 2,
    "failed_tests": [
      "tests/test_order.py::test_duplicate_submit"
    ]
  },
  "truncated": true
}
```

再提供：

```text
read_process_output(
  process_id,
  stream,
  offset,
  max_chars
)
```

这样模型只在需要时读取日志的某一段，避免一次把完整构建日志塞进上下文。



## P1：提高长任务可靠性

### 6. 增加明确的 task/session 状态

ChatGPT conversation 是模型会话，但本地还需要自己的执行会话：

```text
task_id
workspace_id
project_id
base_commit
current_head
revision
created_at
last_activity_at
running_processes
last_patch
last_check
changed_files
```

这样即使：

* 页面刷新
* MCP 临时断开
* ChatGPT 开始新 turn
* 命令执行很久
* 工具结果传输失败

也能通过 `get_workspace` 恢复当前状态。

---

### 7. 所有修改操作支持幂等

MCP 或 Tunnel 可能出现“服务端执行成功，但客户端没有收到结果”。

例如 `apply_patch` 重试时，不应重复应用同一个 patch。

所有 mutation 工具应接受：

```text
idempotency_key
```

服务端持久化：

```text
(idempotency_key, input_hash, result)
```

重复请求直接返回第一次结果。

这对以下工具尤其重要：

* `create_workspace`
* `apply_patch`
* `discard_workspace`
* `run_pwsh`
* `run_check`
* `cancel_process`


---

### 9. 加入项目指令发现

Codex 会考虑项目级指令和工具配置。你的项目也应该确定性地查找：

```text
AGENTS.md
README.md
CONTRIBUTING.md
pyproject.toml
package.json
Cargo.toml
*.sln
*.csproj
Makefile
```

`create_workspace` 可以返回一个 `project_manifest`：

```json
{
  "languages": ["python"],
  "instructions": ["AGENTS.md"],
  "test_commands": ["unit_tests", "lint"],
  "package_manager": "uv",
  "entrypoints": ["src/app.py"],
  "git_head": "..."
}
```

---

### 10. 完善工具元数据和版本协商

你的计划已经提到 `readOnlyHint`、`destructiveHint` 和 `openWorldHint`，这应该真正落实到所有 MCP 工具。

再增加：

```text
schema_version
server_version
capabilities
max_read_chars
max_output_chars
supports_async_process
supports_expected_hash
supports_artifacts
```

提供：

```text
get_capabilities()
```

避免 ChatGPT 根据旧工具说明调用当前版本不支持的参数。

