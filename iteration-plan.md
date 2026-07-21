

## 当前检查结果

Gpt-Local 当前分支：

* 应用代码约 **9,843 行**
* 测试代码约 **4,037 行**
* Ruff：通过
* mypy strict：通过
* pytest：**204 passed，10 failed**

10 个失败全部集中在 `tests/unit/test_pty_process.py`。根因是测试直接访问 SQLite，但没有稳定调用 `init_db()`；同时 `ProcessManager`、数据库路径和 thread-local connection 都是模块级全局状态。这是目前最明确、最应该先修复的工程问题。

---



## 1.  先修复数据库和单例的测试隔离

- [x] done

当前问题来自：

* `app/storage/database.py` 使用可变全局 `_DB_PATH`
* SQLite connection 存在 `threading.local()` 中
* `ProcessManager` 是进程级 singleton
* 部分测试文件有自己的 autouse DB fixture，但 `test_pty_process.py` 没有
* `_write_process_input()` 在验证空输入前就访问数据库

对应位置：

* `app/storage/database.py`
* `app/services/process_manager.py`
* `tests/unit/test_pty_process.py`
* `app/tools/pty_process.py`

建议不要只在失败测试里补一句 `db.init_db()`，而是进行轻量依赖注入：

```python
class Database:
    def __init__(self, path: Path): ...
    def connect(self) -> sqlite3.Connection: ...

class ProcessManager:
    def __init__(
        self,
        database: Database,
        config: ProcessConfig,
        clock: Clock = time.monotonic,
    ): ...
```

测试中创建独立的 `Database(tmp_path / "operator.db")` 和 `ProcessManager`，生产环境再由 app factory 创建 singleton。

同时调整参数验证顺序：

```python
if not text and not append_newline:
    return INVALID_INPUT

record = database.get_process(process_id)
```

而不是为了判断一个明显的输入错误，先依赖数据库正常初始化。

**验收标准：**

* 214 个测试全部通过
* 测试执行顺序随机化后仍通过
* 测试可以并行运行
* 单个测试不依赖其他测试残留的 DB connection 或 singleton


---

## 2. 建立统一的 ToolSpec 和中央执行中间件

- [x] done

现在各工具的行为仍有明显漂移：

* `reader.py` 使用统一 envelope
* `search.py` 部分错误直接返回裸 `{error: ...}`
* 实际使用了 `PROCESS_NOT_RUNNING`、`PTY_NOT_ACTIVE`，但没有列入 `envelope.py` 的稳定错误码说明
* audit、幂等、异常映射、输出截断分别由各工具自行实现
* `get_capabilities()` 是手工维护，容易和实际注册工具不一致

建议定义统一工具契约：

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str
    effects: frozenset[Effect]
    concurrency_key: Callable[[Input], str | None]
    idempotency: IdempotencyPolicy
    interrupt_behavior: Literal["cancel", "block"]
    max_result_chars: int
    permission_profile: str
    retry_policy: RetryPolicy
```

所有工具统一经过：

```text
schema validation
→ workspace resolution
→ policy evaluation
→ permission/approval
→ concurrency scheduling
→ execution
→ result persistence
→ audit/telemetry
→ envelope normalization
```

这样可以避免每个工具分别处理异常和状态。

### 两个应顺手修复的具体问题

`run_pwsh` 的幂等 fingerprint 当前包含脚本、timeout 和 wait，但没有包含 `working_directory`。同一个 idempotency key 在不同目录执行同一脚本，可能错误复用结果。

`capabilities.py` 中版本为 `0.2.0`，而 `pyproject.toml` 中是 `0.1.0`。版本应只有一个来源，例如：

```python
from importlib.metadata import version

SERVER_VERSION = version("gpt-local-code-operator")
```

工具能力也应由注册表自动生成，而不是手写布尔字段。

---

## 3. 改进进程调度、资源限制和服务重启恢复

当前 `ProcessManager` 使用：

* 全局 singleton
* 一个 `BoundedSemaphore`
* 默认最多同时运行 3 个任务
* watchdog thread
* 全局统一额度，没有 workspace 公平性

这会出现：

* 一个 workspace 占满全部进程槽位
* 只读检查和破坏性写入没有调度差异
* 同一 workspace 中两个修改命令可能并发执行
* 没有 CPU、内存、磁盘或子进程数量限制
* 服务重启后，所有 queued/running 记录直接被标记为 interrupted

claude-code 的调度思路值得借鉴：根据工具的 `isConcurrencySafe()` 分批执行，安全读取可以并行，写操作和未知操作串行，而且最终结果仍按原始调用顺序返回。

### Gpt-Local 可采用 concurrency key

```text
read:file:A       可与 read:file:B 并行
write:file:A      与涉及 file:A 的操作互斥
workspace:ws-123  workspace 级写锁
process:pr-123    进程控制操作串行
global:git        特定 Git 元数据操作互斥
```

增加：

* 全局和每 workspace 独立配额
* FIFO 或 weighted fairness
* command priority
* queue timeout
* CPU、内存、进程数、输出和磁盘额度
* Windows Job Object 管理整个子进程树
* shutdown 时先 graceful interrupt，再 terminate，最后 kill

### 服务恢复

当前 `server.py` 和 `supervisor.py` 在启动时把不完整任务标记为 interrupted。这是安全的，但恢复能力有限。

可以在 DB 中保存：

* PID
* process creation identity
* command hash
* stdout/stderr 路径
* job object identity
* heartbeat
* last output offset

服务重启后：

1. PID 和 creation identity 匹配：恢复为 running，并重新监测。
2. 进程不存在：标记 interrupted。
3. 身份不匹配：避免 PID reuse，标记 lost。
4. 状态不确定：返回 `RECOVERY_REQUIRED`，而不是静默重跑命令。

---

## 4. 从文本搜索升级到符号级代码智能

当前代码导航主要依赖：

* `get_repo_map`
* ripgrep
* 分段读取文件
* 简单项目 manifest

这足够完成小型项目，但大仓库会产生大量无关上下文。claude-code 源码中存在 LSP manager、被动 diagnostics、重初始化和 stale-generation 处理，说明符号级代码智能对长期 agent coding 很重要。

建议增加：

```text
list_symbols(path)
find_definition(symbol)
find_references(symbol)
find_implementations(symbol)
get_call_hierarchy(symbol)
get_diagnostics(path)
get_changed_symbols(base, head)
```

实现上可以先接入已有 language server：

* Python：Pyright
* TypeScript：typescript-language-server
* C/C++：clangd
* C#：OmniSharp 或 Roslyn LSP
* Rust：rust-analyzer

进一步可使用 tree-sitter 建立轻量索引，在 LSP 未启动时提供：

* 文件级 symbol outline
* import graph
* class/function signature
* changed-symbol summary
* 测试与实现文件关联

这会比继续强化 `rg` 的启发式逻辑更有价值。

---

## 5. 用事件流替代频繁轮询

`run_pwsh(wait=True)` 和 `run_command(wait=True)` 当前每 0.5 秒轮询一次状态。输出读取也需要调用方反复传 offset。

建议建立 append-only event stream：

```json
{
  "event_id": 184,
  "request_id": "req_x",
  "workspace_id": "ws_x",
  "process_id": "pr_x",
  "type": "process.output",
  "sequence": 42,
  "stream": "stdout",
  "offset": 8192
}
```

事件类型可以包括：

* `tool.queued`
* `tool.started`
* `policy.decision`
* `process.output`
* `process.exited`
* `artifact.created`
* `check.completed`
* `workspace.changed`
* `plan.evidence_attached`

然后提供：

```text
get_events(after_event_id)
subscribe_process(process_id)
```

如果当前 MCP transport 不适合 server push，至少使用 long polling 和 opaque cursor，而不是固定 0.5 秒循环。

同时接入可选 OpenTelemetry：

```text
interaction/request
  └── tool
       ├── policy
       ├── queue_wait
       ├── process_spawn
       └── artifact_scan
```

重点记录：

* 排队时间
* 工具运行时间
* 输出大小和截断次数
* permission decision
* 重试和幂等 replay
* 进程取消原因
* DB lock 等待时间

默认必须脱敏脚本、路径和用户输入。

---

## 6. 增加原子 Change Set，而不仅是单次 patch

`patcher.py` 已经很强，支持：

* unified diff 解析和路径校验
* `git apply --check`
* expected SHA-256
* expected HEAD
* 幂等调用
* exact text replacement
* 原子单文件写入

但多文件任务仍可能出现：

1. 修改前三个文件成功。
2. 第四个文件失败。
3. workspace 留在半完成状态。

建议增加事务式 API：

```text
begin_change_set(workspace_id)
stage_patch(change_set_id, ...)
stage_replace(change_set_id, ...)
validate_change_set(change_set_id)
commit_change_set(change_set_id)
rollback_change_set(change_set_id)
```

底层可通过临时 Git index、临时 tree、额外 worktree 或文件 snapshot 实现。

还可以加入：

* 修改后自动 formatter
* 只对 changed files 运行 lint
* validation 失败时自动 rollback
* 返回 before/after tree hash
* AST-aware rename/import edit

`apply_patch` 继续作为通用 fallback，不必删除。

---

## 7. 让 checks 和验收规则结构化

`get_workspace_report()` 是 Gpt-Local 的强项，但当前存在两个脆弱点。

### 测试步骤依靠自然语言正则识别

`app/tools/reports.py` 使用：

```python
_TEST_STEP_RE = re.compile(
    r"\b(test|tests|testing|check|checks|pytest|playwright)\b|测试|验收",
    re.I,
)
```

如果 plan step 写成“验证修改行为”，系统可能识别不到；写成“调查测试失败”，又可能错误要求成功测试证据。

建议 step 显式声明类型：

```json
{
  "id": "verify",
  "kind": "verification",
  "required_evidence": [
    {"type": "check", "check_id": "unit_tests", "status": "passed"}
  ]
}
```

### checks 应支持 DAG 和结果解析

```yaml
checks:
  lint:
    command: python -m ruff check .
    parser: ruff-json

  unit:
    depends_on: [lint]
    command: python -m pytest --junitxml=...
    parser: junit

  typecheck:
    command: python -m mypy app
    parser: mypy
```

进一步支持：

* changed-file-aware check selection
* JUnit、SARIF、coverage 结构化结果
* flaky retry policy
* check dependencies
* fail-fast 与 continue-on-error
* required、recommended、optional 三类检查
* acceptance policy profile

Plan evidence 也可以由事件系统自动挂载，不再完全依赖 agent 手动更新。

---

## 8. 优化文件、搜索和 artifact 的大文件行为

当前若干实现会对大文件产生不必要的内存和 I/O 开销。

### `reader.py`

当前会：

1. `read_bytes()` 读取全文件用于 SHA
2. `read_text()` 再读取全文件
3. `splitlines()` 构造完整行数组
4. 最后只返回一个小范围

建议：

* 流式计算 SHA
* 使用二进制 seek 或行索引读取范围
* 缓存 `mtime + size + hash`
* 检测 binary 和编码
* 对超大文件建立行偏移索引

### `read_process_output`

当前先读取完整输出文件，再切片：

```python
text = path.read_text(...)
content = text[offset:offset + max_chars]
```

应直接 seek 到 offset，最多读取请求长度。

### `search.py`

建议增加：

* 输出 cursor，而不是只截断
* timeout 可配置
* 多 query 并行
* 每个 query 独立预算，而不是简单平均分配
* 总匹配数和返回匹配数分开
* 对 regex 编译错误返回稳定错误码
* 全部错误统一 envelope

### Artifact registry

目前：

* 实现中硬编码发现上限 100
* 配置中又有 `max_discovery_files`
* `list_artifacts()` 可能重新哈希所有文件
* 使用 `read_bytes()` 计算完整 SHA

应读取真实配置，使用流式 hash，并基于 `size + mtime` 判断是否需要重新哈希。

---

## 9. 配置、数据库迁移和版本治理

### 不应提交个人绝对路径

`config/projects.yaml` 当前包含：

* 用户目录
* OneDrive 路径
* `E:/GPTWorktrees/...`
* PowerShell 绝对路径

建议改为：

```text
config/projects.example.yaml      提交
config/projects.local.yaml        .gitignore
```

支持：

```yaml
repository: ${GPT_PROJECT_ROOT}/test-gpt-local
worktree_root: ${GPT_WORKTREE_ROOT}/gpt-local
pwsh:
  executable: ${PWSH_PATH:-pwsh}
```

### 正式数据库迁移

不要继续依赖运行时 `PRAGMA table_info` 和分散的 `ALTER TABLE`。增加：

```text
schema_migrations
001_initial.sql
002_workspace_baseline.sql
003_process_recovery.sql
```

每次迁移使用事务，并在启动前备份 DB。

### 依赖锁定

`pyproject.toml` 目前主要使用下界版本。建议增加 lock file，并在 CI 中验证：

* 最低支持版本
* lock 版本
* 最新兼容版本

---
