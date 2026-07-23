

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

### 5.1 本节目标和范围

本节先解决两个具体问题：

1. Server 内部的 `wait=True` 不再通过 `time.sleep(0.5)` 轮询 `get_result()`。
2. 异步调用方可以阻塞等待“有新事件或超时”，不再固定间隔调用
   `get_process_result()` 和 `read_process_output()`。

第一版不直接实现 WebSocket，也不依赖 MCP transport 的 server push。先实现：

* SQLite 持久化的 append-only 事件表；
* 进程内 `Condition`/`Event` 唤醒；
* MCP 工具 `get_events(..., wait_seconds=...)`，即 long polling；
* `ProcessManager.wait_for_terminal()`，供 `run_pwsh`、`run_command` 和
  `run_check` 的 `wait=True` 共用。

第一版事件只携带元数据和小型摘要。`process.output` 只说明哪个 stream
从哪个 offset 增长到哪个 offset，不把任意长度的 stdout/stderr 复制到 SQLite。
调用方收到事件后，继续用 `read_process_output()` 按 cursor 读取内容。

以下内容明确放到第二阶段：

* WebSocket/SSE 或 MCP 原生通知；
* 跨多实例的外部消息总线；
* 全量 OpenTelemetry exporter；
* 将每一小段 stdout/stderr 都写入数据库。

### 5.2 事件数据模型

增加 `events` 表：

```sql
CREATE TABLE events (
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id     TEXT,
    workspace_id   TEXT,
    process_id     TEXT,
    event_type     TEXT NOT NULL,
    sequence       INTEGER,
    payload_json   TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL
);

CREATE INDEX idx_events_workspace_event
    ON events(workspace_id, event_id);
CREATE INDEX idx_events_process_event
    ON events(process_id, event_id);
CREATE UNIQUE INDEX idx_events_process_sequence
    ON events(process_id, sequence)
    WHERE process_id IS NOT NULL AND sequence IS NOT NULL;
```

事件 envelope：

```json
{
  "event_id": 184,
  "request_id": "req_x",
  "workspace_id": "ws_x",
  "process_id": "pr_x",
  "type": "process.output",
  "sequence": 42,
  "created_at": "ISO-8601 timestamp",
  "payload": {
    "stream": "stdout",
    "offset_start": 4096,
    "offset_end": 8192
  }
}
```

规则：

* `event_id` 是全局、单调递增且持久化的 cursor。
* `sequence` 只在一个 process 内单调递增，用于检测重复或丢失。
  分配 sequence 时使用 `BEGIN IMMEDIATE` 包住“读取当前值 + 插入”，或使用独立的
  process counter row；不能在事务外用 `MAX(sequence) + 1`。
* cursor 对调用方是 opaque string；当前即使编码自 `event_id`，也不能要求调用方解析。
* `payload_json` 必须有每类事件的固定 schema 和大小上限，建议第一版 16 KiB。
* 同一数据库事务中先更新业务状态，再插入对应事件，最后 commit，避免“事件存在但状态未更新”。
* 事件写入失败时，进程收尾不能悬挂；应记录 error log，并由最终状态读取作为兜底。

### 5.3 第一版事件类型

第一版只实现能替代进程轮询的核心类型：

* `tool.queued`
* `tool.started`
* `process.output`
* `process.exited`
* `artifact.created`
* `check.completed`

各类型的最小 payload：

```text
tool.queued       tool_name, priority
tool.started      tool_name, pid, working_directory_redacted
process.output    stream, offset_start, offset_end
process.exited    status, exit_code, reason, stdout_offset, stderr_offset
artifact.created  artifact_id, kind, relative_path, size_bytes
check.completed   check_id, status, process_id
```

后续再增加：

* `policy.decision`
* `workspace.changed`
* `plan.evidence_attached`

禁止放入事件 payload：

* 完整脚本或 command；
* 环境变量值；
* API key、cookie、authorization header；
* workspace 外的绝对路径；
* 未经过长度限制的 stdout/stderr。

### 5.4 EventStore 和唤醒机制

新增 `app/services/event_store.py`，不要让各工具直接拼 SQL：

```python
class EventStore:
    def append(
        self,
        event_type: str,
        *,
        request_id: str | None = None,
        workspace_id: str | None = None,
        process_id: str | None = None,
        payload: Mapping[str, JSONValue] | None = None,
    ) -> Event: ...

    def list_after(
        self,
        cursor: str | None,
        *,
        workspace_id: str | None = None,
        process_id: str | None = None,
        limit: int = 100,
    ) -> EventPage: ...

    def wait_after(
        self,
        cursor: str | None,
        *,
        workspace_id: str | None = None,
        process_id: str | None = None,
        limit: int = 100,
        timeout_seconds: float = 25,
    ) -> EventPage: ...
```

实现要求：

* `append()` commit SQLite 后，在同一个 `Condition` lock 下递增内存 generation，
  再调用 `notify_all()`。
* `wait_after()` 先查库；无结果时获取 `Condition` lock，记下 generation，再查一次库；
  仍无结果且 generation 未变化才调用 `Condition.wait(remaining)`。唤醒后用 `while`
  循环重新查询 predicate。这样同时避免查询与 wait 之间丢失通知和 spurious wakeup。
* server shutdown 时唤醒所有 waiter，并返回明确的结束原因。
* 工具 handler 是 async；阻塞的 `Condition.wait()` 必须通过
  `asyncio.to_thread()` 或等效方式执行，不能阻塞 MCP event loop。
* 因 `to_thread()` 等待会占用 worker，第一版设置 `max_waiters=32`；超过上限返回
  retryable `RATE_LIMITED`，不能无限创建 waiter/thread。
* 单次 long poll 建议最多 25 秒；`wait_seconds=0` 表示立即返回。
* `limit` 默认 100，最大 500；超过一页时返回 `next_cursor` 和
  `has_more=true`，不能等待。

SQLite 是第一版事实来源，`Condition` 只负责同一 server process 内降低延迟。
服务重启后 waiter 会断开，但已提交事件仍可从 cursor 继续读取。

### 5.5 MCP 工具契约

第一版提供一个通用读取工具：

```text
get_events(
    workspace_id,
    cursor=null,
    process_id=null,
    event_types=null,
    limit=100,
    wait_seconds=0
)
```

返回示例：

```json
{
  "events": [],
  "cursor": "opaque_cursor_for_last_observed_event",
  "has_more": false,
  "timed_out": true
}
```

契约细节：

* `workspace_id` 必填，防止跨 workspace 订阅。
* 若传 `process_id`，必须验证 process 属于该 workspace。
* `cursor=null` 的第一版语义固定为“从调用时的最新位置开始”，避免首次调用
  意外返回整个历史；如需历史，另加显式 `from_beginning=true`。
* 空结果但超时是成功响应，不是错误。
* 无效或过期 cursor 返回稳定错误码 `INVALID_CURSOR` 或
  `EVENT_CURSOR_EXPIRED`。
* `event_types` 只能选择已注册事件类型。
* `get_events` 加入 `ToolSpec` 并标记为 read-only；不要持有普通 workspace
  concurrency lock 完成整段 long poll，否则会阻塞同 workspace 的写入和事件产生。
  workspace 隔离由 handler 在进入等待前完成验证。
* `get_capabilities()` 增加
  `supports_event_stream`、`supports_event_long_poll` 和 retention/limit 信息。

可以提供 `subscribe_process(process_id, cursor, wait_seconds)` 作为方便调用的薄封装，
但它不应有第二套存储或 cursor 语义。

### 5.6 ProcessManager 集成点

给 `ProcessManager` 注入同一个 `EventStore`，并在以下位置发事件：

```text
申请 scheduler lease 前          tool.queued
lease 成功且 DB 状态为 running 后 tool.started
检测到 stdout/stderr 长度增加时   process.output
_finalize() 成功提交终态后         process.exited
_terminate_running() 提交终态后    process.exited
cancel() 提交 cancelled 后         process.exited
shutdown/recovery 状态确定后        process.exited
artifact registry 创建记录后       artifact.created
run_check 得到终态后                check.completed
```

注意当前 `ProcessScheduler.acquire()` 是同步阻塞的，并且 process DB row 在 lease
成功后才创建。因此若要准确发出 `tool.queued`：

* 要么在进入 `_acquire_lease()` 前生成 `process_id` 并插入 queued row；
* 要么第一版将 `tool.queued` 定义为 request 级事件，允许 `process_id=null`。

建议选择前者，保证 queued、started、exited 始终可用同一个 `process_id` 关联。
这要求将 `spawn()` 和 `spawn_interactive()` 中生成 ID、插入 DB、获取 lease 的顺序统一，
并在 queue timeout 时写入明确终态和事件。

当前 `update_process_status()` 和事件插入会分别 commit。为满足状态与事件原子性，
需要新增数据库事务 helper，例如
`transition_process_with_event(process_id, status, event_type, payload)`；
`_finalize()`、`cancel()`、timeout、shutdown 和 recovery 必须走该 helper，不能先调用
现有 update 再单独 `EventStore.append()`。

增加：

```python
ProcessManager.wait_for_terminal(
    process_id: str,
    timeout_seconds: float,
) -> dict[str, Any]
```

它等待 `_RunningProcess._completed` 或 EventStore 的 `process.exited`，然后只读取一次
最终 DB/result。`run_pwsh(wait=True)`、`run_command(wait=True)` 和
`run_check(wait=True)` 全部改用这个方法，删除各自的 `_WAIT_POLL_INTERVAL`
和重复 terminal-status 集合。

将 terminal statuses 定义在一个公共常量中：

```python
TERMINAL_PROCESS_STATUSES = frozenset({
    "passed", "failed", "timed_out", "cancelled",
    "resource_exhausted", "interrupted", "lost", "recovery_required",
})
```

### 5.7 输出事件策略

当前普通进程把 stdout/stderr 直接重定向到文件，Server 不会在每次 child write
时收到回调；PTY 只有 stdout reader thread。第一版采用低风险方案：

* watchdog 每次 heartbeat 比较 stdout/stderr 的 byte size；
* 只有 size 增长时才发 `process.output`；
* 分别维护两个 stream 的最后已发布 byte offset；
* `_finalize()` 前做最后一次 scan，避免漏掉退出前最后一段输出；
* PTY reader 可在 flush 后主动通知，但要做 50–100 ms 合并，避免事件风暴。

这里的 offset 统一定义为 UTF-8 文件的 **byte offset**。因此
`read_process_output()` 也应改成 binary seek + incremental UTF-8 decode，并返回同一种
opaque cursor。不要继续混用当前的 Python character offset 和 DB 中的 byte size。

合并策略：

* 同一 process/stream 100 ms 内的多次增长合并为一个事件；
* 每个 `process.output` 只记录范围，不复制内容；
* process 退出时即使没有新内容也必须发 `process.exited`；
* output 文件被截断或轮转时发 warning payload，并重置 reader cursor；
* ANSI 处理仍属于 `read_process_output()`，事件层不修改原始输出。

### 5.8 保留、清理和恢复

增加配置：

```yaml
events:
  enabled: true
  retention_days: 7
  max_events_per_workspace: 50000
  max_payload_bytes: 16384
  max_page_size: 500
  max_wait_seconds: 25
  max_waiters: 32
  output_coalesce_ms: 100
```

清理规则：

* workspace discard 时删除或归档其 events，行为必须和 plan/artifact 一致；
* 日常清理按 retention 和 workspace 上限分批删除，不能长时间锁 DB；
* 返回的最小可用 cursor 应可检测；请求已清理历史时返回
  `EVENT_CURSOR_EXPIRED`，并附当前可恢复的 cursor；
* 进程恢复后先发 `tool.started`（payload 标记 `recovered=true`）或最终
  `process.exited`，不得静默生成第二个进程；
* 唯一约束确保 recovery/finalize race 不产生重复 process sequence。

### 5.9 OpenTelemetry（第二阶段）

同时接入可选 OpenTelemetry：

```text
interaction/request
  └── tool
       ├── policy
       ├── queue_wait
       ├── process_spawn
       └── artifact_scan
```

OTel 必须是 optional dependency 和可选配置；exporter 不可用时不能影响工具执行。
span/event attributes 只记录低基数、已脱敏的字段。重点记录：

* 排队时间
* 工具运行时间
* 输出大小和截断次数
* permission decision
* 重试和幂等 replay
* 进程取消原因
* DB lock 等待时间

默认必须脱敏脚本、路径和用户输入。

推荐指标：

```text
tool.duration_ms                  histogram
process.queue_wait_ms             histogram
process.runtime_ms                histogram
process.output_bytes              counter
process.output_truncations        counter
events.long_poll_wait_ms          histogram
events.returned                   counter
events.cursor_expired             counter
database.lock_wait_ms             histogram
```

不要把 `workspace_id`、`process_id`、路径或 request ID 作为 metrics label；
这些高基数字段只适合 trace/log。

### 5.10 实施顺序

建议拆成以下可独立 review 的提交：

1. **Schema + EventStore**：events 表、cursor codec、append/list/wait、retention 单元测试。
2. **Process lifecycle events**：queued/started/exited，统一 terminal status 和
   `wait_for_terminal()`。
3. **替换内部轮询**：迁移 `run_pwsh`、`run_command`、`run_check`，保留公开
   `get_process_result` 向后兼容。
4. **输出 cursor 统一**：byte-offset reader、`process.output` coalescing、长输出测试。
5. **MCP API**：`get_events`、ToolSpec、capabilities、workspace isolation。
6. **Artifact/check/recovery 事件**：补全跨服务集成点和 restart 测试。
7. **可选 telemetry**：OTel spans/metrics、脱敏测试和 exporter failure 测试。

第一版完成 1–6 即可验收；第 7 步不阻塞事件流 MVP。

### 5.11 测试要求

单元测试：

* event ID 全局递增，process sequence 单调且不可重复；
* cursor encode/decode、非法 cursor、过期 cursor；
* append 后 waiter 被唤醒；
* 查询与 wait 交界处插入事件不会丢失；
* spurious wakeup 不产生虚假事件；
* timeout 返回成功空页；
* workspace/process filter 不泄漏其他 workspace 事件；
* payload 大小限制和敏感字段拒绝/脱敏；
* retention 清理和 discard 清理；
* terminal event 幂等，cancel/finalize race 只产生一个最终事件。

集成测试：

* `run_pwsh(wait=True)`、`run_command(wait=True)`、`run_check(wait=True)`
  不再调用 `time.sleep(0.5)`；
* `wait=False` 后用一次 long poll 收到 `process.exited`；
* 长进程产生多个 output range，范围连续且最终 offset 与文件一致；
* PTY 快速小块输出被合并而不丢数据；
* server restart 后用旧 cursor 继续读取已提交事件；
* queue timeout、resource exhausted、cancel、shutdown、recovery 均有最终事件；
* 32 个并发 waiter 不阻塞 MCP event loop；第 33 个收到 retryable
  `RATE_LIMITED`，且没有 DB busy loop；
* 旧的 `get_process_result` 和 `read_process_output` 调用继续工作。

### 5.12 验收标准

* [ ] 三个 `wait=True` 工具不再包含固定间隔轮询循环。
* [ ] 异步进程从启动到终态至少产生 queued、started、exited 事件。
* [ ] 调用方可以用一个 cursor 和 long poll 连续消费事件。
* [ ] long poll 超时、server restart、cursor 过期都有确定语义。
* [ ] process output cursor 使用统一 byte-offset，不会切坏 UTF-8 字符。
* [ ] 事件按 workspace 隔离，不能订阅其他 workspace 的 process。
* [ ] 事件 payload 不保存脚本、环境变量值或未脱敏绝对路径。
* [ ] output/event 数量受 coalescing、page limit 和 retention 控制。
* [ ] cancel、timeout、resource limit、shutdown 和 recovery 都有唯一终态事件。
* [ ] 现有 process、PTY、artifact、check 和 report 测试全部通过。
* [ ] OpenTelemetry 关闭或 exporter 故障时核心功能不受影响。

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
