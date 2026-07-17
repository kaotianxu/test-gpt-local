你当前 `gpt-local-code-operator 0.2.0` 已经不缺基础代码操作工具。现有 22 个工具覆盖了：

* 仓库导航与读取：`get_repo_map`、`search_code`、`read_files`
* 修改：`apply_patch`、`replace_text`
* 执行：`run_pwsh`、`run_check`
* 异步进程：`get_process_result`、`cancel_process`、`read_process_output`
* Git 验收：`git_status`、`git_diff`、`get_workspace_report`
* 一致性：SHA-256、expected HEAD、幂等键、artifact

事实上，你在“结构化代码操作”方面比 Codex 的基础工具更细。Codex 的核心工具主要依靠通用 shell、`apply_patch`、图片查看、权限控制和若干 Agent Runtime 工具完成任务。([GitHub][1])

## 最值得增加的工具

### 1. `view_image`：最高优先级

Codex 有专门的 `view_image`，接受本地图片路径，可以按高分辨率或原始分辨率返回图片给模型检查。([GitHub][2])

你的项目目前只能读取文本，无法让 ChatGPT 直接查看工作区里的：

* UI 截图
* Playwright 截图
* 测试 snapshot
* Matplotlib 图表
* 架构图
* CV 模型输出
* 游戏画面和贴图
* 扫描件或错误对话框截图

建议接口：

```text
view_image(
    workspace_id,
    path,
    detail="high" | "original"
)
```

返回内容：

```json
{
  "path": "artifacts/test-failure.png",
  "mime_type": "image/png",
  "width": 1920,
  "height": 1080,
  "sha256": "...",
  "image": "<MCP ImageContent>"
}
```

需要限制：

* 只能读取 workspace 内文件
* 检查真实 MIME 类型，而不是只看扩展名
* 设置像素数和文件大小上限
* `.svg` 最好先安全栅格化
* 默认缩放，必要时才返回原图

**这是当前最明显、投入产出比最高的缺口。**

---

### 2. `write_stdin` + PTY：最高优先级

Codex 的统一执行工具支持：

* TTY
* 指定 shell
* 启动后短暂等待并返回
* 继续向运行中的进程写入 stdin
* 对交互式进程持续操作

其 `exec_command` 参数中明确包含 `tty`、shell、yield time、权限和输出预算，并另外注册了 `write_stdin`。([GitHub][3])

你的 `run_pwsh(wait=false)` 可以启动后台进程，但目前没有真正的交互输入能力。因此难以可靠操作：

* Python/Node REPL
* `gdb`、`pdb`
* 要求确认的 CLI
* 长期运行的 dev server
* `npm create` 等交互式生成器
* 数据库控制台
* watch mode
* 需要 Ctrl+C、EOF 或按键输入的程序

建议不是增加一个全新的执行系统，而是扩展现有进程管理：

```text
run_pwsh(
    ...,
    tty=true
)

write_process_input(
    process_id,
    text,
    append_newline=true
)

resize_terminal(
    process_id,
    columns,
    rows
)

send_process_signal(
    process_id,
    signal="interrupt" | "eof" | "terminate"
)
```

Windows 上需要使用 **ConPTY**，普通的 stdin pipe 无法正确支持很多终端程序。

考虑到你也经常使用 WSL，可以进一步改为：

```text
run_command(
    shell="pwsh" | "cmd" | "wsl-bash",
    command="..."
)
```

但 PowerShell 仍可以作为默认 shell。

---

### 3. 本地图片/文件 artifact 统一查看

这不是 Codex 中单独命名的一个工具，但可以基于 `view_image` 和你现有的 process artifact 体系扩展成：

```text
list_artifacts(workspace_id)
read_artifact(artifact_id, offset, max_chars)
view_artifact(artifact_id)
```

例如测试运行后自动发现：

```json
{
  "artifacts": [
    {
      "id": "artifact_123",
      "kind": "image",
      "path": "playwright-report/failure.png"
    },
    {
      "id": "artifact_124",
      "kind": "html",
      "path": "coverage/index.html"
    }
  ]
}
```

这样 ChatGPT 不需要先猜截图路径。

对于你的架构，这甚至比单纯复制 `view_image` 更实用。

---



### 5. `update_plan`：中高优先级

Codex 有 `update_plan`，保存任务步骤及 `pending`、`in_progress`、`completed` 状态，并限制同时最多一个步骤处于进行中。([GitHub][5])

ChatGPT 本身可以制定计划，所以你不需要复制它的推理功能。但**把计划持久化到 workspace** 很有价值：

* 页面刷新后恢复任务
* 新 ChatGPT 对话接管旧 workspace
* 判断任务做到哪一步
* 在 `get_workspace_report` 中显示完成状态
* 避免模型重复执行已完成步骤

建议增加：

```text
update_workspace_plan(
    workspace_id,
    explanation,
    steps=[
        {"id": "inspect", "text": "Inspect implementation", "status": "completed"},
        {"id": "patch", "text": "Apply fix", "status": "in_progress"},
        {"id": "test", "text": "Run checks", "status": "pending"}
    ]
)

get_workspace_plan(workspace_id)
```

还可以给每一步记录证据：

```json
{
  "id": "test",
  "status": "completed",
  "evidence": {
    "process_id": "proc_123",
    "check_id": "unit_tests"
  }
}
```

这比只保存自然语言计划更可靠。

---



其中前两项会直接扩展 ChatGPT 能完成的任务类型；计划和权限工具主要提升长任务的可靠性与可控性。

[1]: https://raw.githubusercontent.com/openai/codex/main/codex-rs/core/src/tools/spec_plan.rs "raw.githubusercontent.com"
[2]: https://raw.githubusercontent.com/openai/codex/main/codex-rs/core/src/tools/handlers/view_image_spec.rs "raw.githubusercontent.com"
[3]: https://raw.githubusercontent.com/openai/codex/main/codex-rs/core/src/tools/handlers/unified_exec.rs "raw.githubusercontent.com"
[4]: https://raw.githubusercontent.com/openai/codex/main/codex-rs/core/src/tools/handlers/request_permissions.rs "raw.githubusercontent.com"
[5]: https://raw.githubusercontent.com/openai/codex/main/codex-rs/core/src/tools/handlers/plan_spec.rs "raw.githubusercontent.com"
[6]: https://raw.githubusercontent.com/openai/codex/main/codex-rs/core/src/tools/handlers/mcp_resource_spec.rs "raw.githubusercontent.com"
[7]: https://raw.githubusercontent.com/openai/codex/main/codex-rs/core/src/tools/handlers/tool_search_spec.rs "raw.githubusercontent.com"
[8]: https://raw.githubusercontent.com/openai/codex/main/codex-rs/core/src/tools/handlers/get_context_remaining_spec.rs "raw.githubusercontent.com"
[9]: https://raw.githubusercontent.com/openai/codex/main/codex-rs/core/src/tools/handlers/test_sync_spec.rs "raw.githubusercontent.com"


# GPT Local Code Operator 新增工具验收标准

## 1. 验收目标

本次迭代新增以下能力：

1. `view_image`
2. PTY 交互式进程能力
3. Artifact 统一管理
4. Workspace Plan 持久化

本次验收不以“工具能够被调用”为通过标准，而以以下结果为最终标准：

* ChatGPT 能稳定调用新增工具完成真实代码任务。
* 工具返回结果结构清晰、可恢复、可审计。
* 所有路径和进程操作受 workspace 边界约束。
* 新功能不破坏现有文件、Git、进程和检查工具。
* 异常、超时、重复请求和断线场景均有明确行为。

---

# 2. 总体通过条件

本次迭代只有同时满足以下条件才可验收通过：

* 所有 P0 验收项通过。
* 自动化测试全部通过。
* 不存在 Critical 或 High 级缺陷。
* Medium 级缺陷不超过 2 个，且均有明确修复计划。
* 新增工具均被 `get_capabilities` 正确声明。
* 工具 schema、错误码和返回结构已文档化。
* 现有工具回归测试通过。


任何一项 P0 条件失败，整个迭代判定为不通过。

---

# 3. 通用工具验收要求

以下要求适用于所有新增工具。

## 3.1 Schema 验收

* [ ] 工具名称稳定，不与已有工具冲突。
* [ ] 所有必填字段在 MCP schema 中标记为 required。
* [ ] 所有枚举字段只接受声明值。
* [ ] 所有路径必须为 workspace-relative path。
* [ ] schema 中包含清晰的参数说明。
* [ ] schema 中明确说明文件大小、输出大小和超时限制。
* [ ] 不接受未声明字段，或对未声明字段有明确兼容策略。
* [ ] 错误输入不会导致 MCP Server 崩溃。

## 3.2 返回结构验收

每个工具必须返回统一的结构化结果：

```json
{
  "ok": true,
  "request_id": "req_xxx",
  "workspace_id": "ws_xxx",
  "result": {},
  "warnings": [],
  "truncated": false
}
```

错误结果必须满足：

```json
{
  "ok": false,
  "request_id": "req_xxx",
  "error": {
    "code": "STABLE_ERROR_CODE",
    "message": "Human-readable message",
    "retryable": false
  }
}
```

验收项：

* [ ] 成功和失败均返回合法 JSON。
* [ ] 错误码稳定，不依赖异常类名称。
* [ ] 错误信息不泄露 API Key、Token、Cookie 或敏感环境变量。
* [ ] 可重试错误正确标记 `retryable=true`。
* [ ] 输出截断时必须返回 `truncated=true`。
* [ ] 返回结果能够被 ChatGPT 直接理解，不需要解析自由格式日志。



---

# 4. `view_image` 验收标准

## 4.1 接口要求

建议接口：

```text
view_image(
    workspace_id,
    path,
    detail="high" | "original"
)
```

## 4.2 基础功能

* [ ] 能读取 PNG。
* [ ] 能读取 JPEG。
* [ ] 能读取 WebP。
* [ ] 能读取 GIF 的首帧，或明确拒绝 GIF。
* [ ] 返回真实 MIME 类型。
* [ ] 返回宽度和高度。
* [ ] 返回文件 SHA-256。
* [ ] 返回 MCP ImageContent，而不是仅返回 base64 文本字符串。
* [ ] `detail="high"` 会在保持宽高比的前提下缩放。
* [ ] `detail="original"` 返回原始分辨率，前提是未超过安全限制。
* [ ] 图片方向信息正确处理 EXIF orientation。
* [ ] 透明 PNG 不出现异常背景或解码错误。

## 4.3 文件验证

* [ ] MIME 类型由文件内容检测，不只依赖扩展名。
* [ ] 将文本文件重命名为 `.png` 时必须拒绝。
* [ ] 将可执行文件重命名为 `.jpg` 时必须拒绝。
* [ ] 损坏图片返回 `IMAGE_DECODE_FAILED`。
* [ ] 超出文件大小限制返回 `FILE_TOO_LARGE`。
* [ ] 超出最大像素数量返回 `IMAGE_DIMENSIONS_TOO_LARGE`。
* [ ] 零宽度或零高度图片被拒绝。

## 4.4 SVG 验收

若本阶段支持 SVG：

* [ ] SVG 必须先经过安全栅格化。
* [ ] 禁止 SVG 加载外部网络资源。
* [ ] 禁止 SVG 读取本地文件。
* [ ] 禁止脚本执行。
* [ ] 禁止无限递归引用。
* [ ] 栅格化进程有超时和内存限制。

若本阶段不支持 SVG：

* [ ] 返回明确的 `UNSUPPORTED_FILE_TYPE`。
* [ ] 文档中明确说明 SVG 不受支持。



## 4.6 端到端场景

必须完成以下真实任务：

### 场景 A：Playwright 失败截图

1. 运行一个会产生截图的 Playwright 测试。
2. 通过 artifact 工具找到截图。
3. 通过 `view_image` 查看截图。
4. ChatGPT 根据截图识别明显 UI 错误。
5. ChatGPT 修改对应代码。
6. 重新运行测试并通过。

通过条件：

* [ ] ChatGPT 不需要用户手动上传图片。
* [ ] 图片能够直接进入模型上下文。
* [ ] ChatGPT 能根据图片完成至少一次有效代码修改。

### 场景 B：图表检查

1. 执行 Python 脚本生成 Matplotlib 图。
2. 使用 `view_image` 打开输出。
3. ChatGPT 能描述主要图表内容，并发现预设的明显异常。

---

# 5. PTY 和交互式进程验收标准

## 5.1 建议接口

```text
run_command(
    workspace_id,
    shell="pwsh" | "cmd" | "wsl-bash",
    command,
    working_directory="",
    tty=false,
    wait=true,
    timeout_seconds=600
)

write_process_input(
    process_id,
    text,
    append_newline=true
)

resize_terminal(
    process_id,
    columns,
    rows
)

send_process_signal(
    process_id,
    signal="interrupt" | "eof" | "terminate"
)
```

也可以扩展现有 `run_pwsh`，但必须保持向后兼容。

## 5.2 PTY 创建

* [ ] `tty=false` 保持当前非交互式行为。
* [ ] `tty=true` 使用 Windows ConPTY 或等效真实终端。
* [ ] PTY 创建失败返回结构化错误。
* [ ] PTY 进程拥有唯一 `process_id`。
* [ ] 返回初始输出和当前进程状态。
* [ ] 终端默认尺寸有合理值。
* [ ] 支持设置初始 columns 和 rows。
* [ ] 工具不会因子进程持续运行而永久阻塞 MCP 请求。

## 5.3 输入写入

* [ ] 可以向运行中的 Python REPL 输入表达式。
* [ ] 可以向 Node REPL 输入表达式。
* [ ] `append_newline=true` 自动追加正确换行符。
* [ ] `append_newline=false` 不修改输入内容。
* [ ] Unicode 输入正确处理。
* [ ] 多行输入顺序不丢失。
* [ ] 快速连续写入不会交错或重复。
* [ ] 已完成进程收到输入时返回 `PROCESS_NOT_RUNNING`。
* [ ] 不存在的进程返回 `PROCESS_NOT_FOUND`。

## 5.4 信号与终止

* [ ] `interrupt` 对应交互式 Ctrl+C 行为。
* [ ] `eof` 能正确结束支持 EOF 的程序。
* [ ] `terminate` 终止整个进程树。
* [ ] 终止后进程进入 `cancelled` 或其他明确终态。
* [ ] 重复终止同一进程是幂等的。
* [ ] 子进程不会在父进程结束后残留。
* [ ] 超时后自动终止整个进程树。
* [ ] 终止动作记录进 audit log。

## 5.5 输出读取

* [ ] PTY 输出可通过 `get_process_result` 读取。
* [ ] 长输出可通过 `read_process_output` 分段读取。
* [ ] ANSI escape sequence 有明确处理策略。
* [ ] 输出截断时返回完整输出 artifact。
* [ ] stdout 和 stderr 的合并或分离行为有明确文档。
* [ ] 输出中敏感环境变量被脱敏。
* [ ] 中文和其他 Unicode 输出不乱码。

## 5.6 Shell 支持

### PowerShell

* [ ] 正确启动 `pwsh.exe`。
* [ ] 默认使用 `-NoLogo`。
* [ ] 非交互模式使用 `-NoProfile -NonInteractive`。
* [ ] PTY 模式不会错误添加 `-NonInteractive`。
* [ ] 当前工作目录正确设置为 workspace。

### CMD

若支持：

* [ ] 正确处理 quoting。
* [ ] 正确处理 `%VAR%`。
* [ ] 不因特殊字符造成命令注入。

### WSL Bash

若支持：

* [ ] 正确映射 Windows workspace 路径到 WSL。
* [ ] 路径含空格和中文时仍可用。
* [ ] WSL 不可用时返回明确错误。
* [ ] 可指定发行版，或有明确默认发行版策略。
* [ ] WSL 子进程也可被完整终止。

## 5.7 必须通过的交互场景

### 场景 A：Python REPL

1. 启动 `python`，设置 `tty=true`。
2. 输入 `1 + 1`。
3. 输出必须包含 `2`。
4. 输入多行函数定义。
5. 调用该函数并检查结果。
6. 发送 EOF，进程正常结束。

### 场景 B：交互确认

1. 启动测试程序，输出 `Continue? [y/N]`。
2. 使用 `write_process_input` 输入 `y`。
3. 程序继续执行。
4. 最终退出码为 0。

### 场景 C：长期服务

1. 启动本地 HTTP Server。
2. 确认进程状态为 running。
3. 使用另一个命令访问该服务。
4. 使用 `interrupt` 停止服务。
5. 确认端口已释放。
6. 确认无残留子进程。

### 场景 D：调试器

1. 启动 `pdb` 或等效调试器。
2. 输入至少两个调试命令。
3. 能读取对应输出。
4. 正常退出调试器。

---

# 6. Artifact 管理验收标准

## 6.1 建议接口

```text
list_artifacts(
    workspace_id,
    kind=null,
    path_prefix=null
)

read_artifact(
    artifact_id,
    offset=0,
    max_chars=50000
)

view_artifact(
    artifact_id,
    detail="high"
)
```

## 6.2 Artifact 数据模型

每个 artifact 至少包含：

```json
{
  "artifact_id": "artifact_xxx",
  "workspace_id": "ws_xxx",
  "kind": "image",
  "path": "playwright-report/failure.png",
  "mime_type": "image/png",
  "size_bytes": 120044,
  "sha256": "...",
  "created_at": "ISO-8601 timestamp",
  "source": {
    "type": "process",
    "process_id": "proc_xxx"
  }
}
```

验收项：

* [ ] `artifact_id` 在服务器生命周期内唯一。
* [ ] artifact 绑定 workspace。
* [ ] 不允许跨 workspace 读取 artifact。
* [ ] artifact 路径必须位于 workspace 或受控 artifact store。
* [ ] artifact 元数据可持久化。
* [ ] 服务重启后仍能读取已持久化 artifact。
* [ ] 文件被删除后返回明确的 stale 状态。
* [ ] 文件被修改后能够检测 SHA-256 变化。

## 6.3 自动发现

* [ ] `run_check` 可以返回检查产生的 artifact。
* [ ] `run_command` 可以返回命令执行期间新增的 artifact。
* [ ] Playwright screenshot 可被识别。
* [ ] coverage HTML 报告可被识别。
* [ ] pytest XML 或 JUnit XML 可被识别。
* [ ] 图片、文本、JSON、HTML 类型能够分类。
* [ ] 自动发现不会递归扫描整个大型仓库。
* [ ] 自动发现有文件数量和总大小限制。
* [ ] 不自动注册依赖目录中的大量无关文件。

建议默认排除：

```text
.git
node_modules
.venv
venv
dist
build
target
__pycache__
```

除非这些目录明确配置为 artifact 来源。

## 6.4 Artifact 查看

* [ ] 文本 artifact 支持分页读取。
* [ ] JSON artifact 返回合法文本。
* [ ] 图片 artifact 自动路由至图片查看能力。
* [ ] HTML artifact 不在服务端执行脚本。
* [ ] 二进制未知类型不直接注入模型上下文。
* [ ] 超大 artifact 只能分段读取。
* [ ] artifact 内容读取有审计记录。

## 6.5 清理策略

* [ ] discard workspace 时关联 artifact 被一并清理，或明确归档。
* [ ] 存在最大 artifact 保留时间。
* [ ] 存在最大总存储量限制。
* [ ] 清理过程不会删除 workspace 外文件。
* [ ] 正在被读取的 artifact 不会被并发删除导致服务器崩溃。
* [ ] 清理失败被记录但不影响主服务运行。

## 6.6 端到端场景

1. 运行 Playwright 测试。
2. 测试失败并生成 screenshot、trace 和 HTML report。
3. `list_artifacts` 返回上述产物。
4. `view_artifact` 成功查看 screenshot。
5. `read_artifact` 成功读取报告文本或元数据。
6. ChatGPT 根据 artifact 完成问题定位。
7. discard workspace 后 artifact 状态符合设计。

---

# 7. Workspace Plan 验收标准

## 7.1 建议接口

```text
update_workspace_plan(
    workspace_id,
    explanation,
    steps
)

get_workspace_plan(
    workspace_id
)
```

## 7.2 Step 数据结构

```json
{
  "id": "test",
  "text": "Run unit tests",
  "status": "pending",
  "evidence": [],
  "created_at": "ISO-8601 timestamp",
  "updated_at": "ISO-8601 timestamp"
}
```

允许状态：

```text
pending
in_progress
completed
blocked
cancelled
```

## 7.3 状态规则

* [ ] 同时最多一个 step 为 `in_progress`。
* [ ] step ID 在同一 plan 内唯一。
* [ ] 空 step ID 被拒绝。
* [ ] 空 step text 被拒绝。
* [ ] 未声明状态被拒绝。
* [ ] `completed` step 可以附加 evidence。
* [ ] `blocked` step 可以附加阻塞原因。
* [ ] 已完成步骤不会被无意覆盖。
* [ ] 删除步骤需要明确行为或显式参数。
* [ ] 更新 plan 使用 revision 或版本号防止覆盖。

推荐采用：

```json
{
  "plan_revision": 4,
  "expected_revision": 3
}
```

revision 不匹配时返回：

```text
PLAN_REVISION_CONFLICT
```

## 7.4 持久化要求

* [ ] MCP Server 重启后 plan 仍存在。
* [ ] 新 ChatGPT 对话可以通过 workspace ID 获取 plan。
* [ ] plan 与 workspace 生命周期绑定。
* [ ] discard workspace 后 plan 被删除或归档。
* [ ] plan 更新记录写入 audit log。
* [ ] plan 中不保存模型隐藏推理。
* [ ] explanation 只记录面向用户的任务说明。

## 7.5 Evidence 验收

支持的 evidence 类型至少包括：

```text
process_id
check_id
artifact_id
git_commit
git_diff
file_path
```

验收项：

* [ ] evidence 引用的对象必须存在。
* [ ] 不允许引用其他 workspace 的对象。
* [ ] evidence 对象被删除后显示 stale，而不是静默消失。
* [ ] `get_workspace_report` 包含当前 plan 和 evidence 摘要。
* [ ] step 标记为 completed 时，可以配置是否要求 evidence。
* [ ] 测试类步骤标记 completed 时必须关联成功的 check 或 process。

## 7.6 端到端场景

1. 创建 workspace。
2. 创建包含 4 个步骤的 plan。
3. 将第一个步骤设为 `in_progress`。
4. 完成代码检查后标记为 `completed` 并关联文件 evidence。
5. 将第二步设为 `in_progress`。
6. 应拒绝同时将第三步设为 `in_progress`。
7. 重启 MCP Server。
8. 再次读取 plan，内容必须完整。
9. 运行测试，并将 process evidence 绑定到测试步骤。
10. `get_workspace_report` 必须显示计划状态和验收证据。

---

# 8. 与现有工具的集成验收

## 8.1 `get_capabilities`

必须新增并返回：

```json
{
  "supports_view_image": true,
  "supports_pty": true,
  "supports_process_input": true,
  "supports_artifact_registry": true,
  "supports_workspace_plan": true
}
```

若部分能力未启用，应返回 `false`，不能省略造成歧义。

## 8.2 `get_workspace_report`

报告至少新增：

```json
{
  "plan": {
    "revision": 3,
    "completed": 2,
    "in_progress": 1,
    "pending": 2
  },
  "artifacts": {
    "count": 4,
    "kinds": {
      "image": 2,
      "html": 1,
      "text": 1
    }
  },
  "active_processes": [],
  "acceptance_ready": true
}
```

验收项：

* [ ] 报告包含 plan 状态。
* [ ] 报告包含 artifact 摘要。
* [ ] 报告包含未结束 PTY 进程。
* [ ] 存在活动进程时不能错误标记为完全验收。
* [ ] 测试步骤缺少成功 evidence 时不能标记 acceptance ready。
* [ ] Git 状态仍按原逻辑返回。

## 8.3 Audit Log

以下操作必须被审计：

* `view_image`
* `write_process_input`
* `send_process_signal`
* artifact 注册、读取和删除
* plan 创建和更新
* PTY 创建和终止

审计记录至少包含：

```text
timestamp
request_id
workspace_id
tool_name
actor/session identifier
input summary
result status
duration
error code
```

不得在 audit log 中保存：

* 原始 API Key
* 完整 Cookie
* Authorization header
* 未脱敏环境变量
* 用户输入的敏感凭据

---

# 9. 回归验收

以下现有工具必须全部通过既有测试：

* [ ] `list_projects`
* [ ] `create_workspace`
* [ ] `get_workspace`
* [ ] `list_workspaces`
* [ ] `get_repo_map`
* [ ] `search_code`
* [ ] `read_files`
* [ ] `replace_text`
* [ ] `apply_patch`
* [ ] `run_pwsh`
* [ ] `run_check`
* [ ] `get_process_result`
* [ ] `cancel_process`
* [ ] `read_process_output`
* [ ] `git_status`
* [ ] `git_diff`
* [ ] `get_workspace_report`
* [ ] `discard_workspace`
* [ ] `get_capabilities`
* [ ] `ping`

重点回归场景：

* [ ] 非 PTY `run_pwsh` 行为与旧版本兼容。
* [ ] `wait=true` 和 `wait=false` 语义不变。
* [ ] 已有 process artifact 仍可读取。
* [ ] `apply_patch` 的 SHA-256、expected HEAD 和 idempotency 仍有效。
* [ ] 工作区最大数量限制仍有效。
* [ ] 并发任务数限制仍有效。
* [ ] discard workspace 不会误删主仓库。
* [ ] 新增 artifact 扫描不会显著拖慢普通命令。

---

# 10. 并发与可靠性验收

## 10.1 并发场景

* [ ] 同时启动 3 个普通进程符合当前并发限制。
* [ ] 第 4 个进程被排队或明确拒绝。
* [ ] PTY 和非 PTY 进程共享一致的并发限制。
* [ ] 同一进程的并发输入保持顺序。
* [ ] plan 并发更新使用 revision 检测冲突。
* [ ] artifact 注册使用唯一约束避免重复。
* [ ] 同一图片并发读取不会造成文件锁死。

## 10.2 重启恢复

模拟 MCP Server 在以下时间崩溃：

* PTY 进程运行中
* artifact 注册过程中
* plan 更新过程中
* 图片读取过程中

验收要求：

* [ ] 数据库不出现损坏。
* [ ] plan 更新具有原子性。
* [ ] 半完成 artifact 不被当作有效 artifact。
* [ ] 重启后能够识别 orphan process。
* [ ] orphan process 有明确清理策略。
* [ ] 服务重启后 `ping` 和现有工具正常。

## 10.3 幂等性

* [ ] 重复提交相同 plan 更新不会产生重复步骤。
* [ ] 重复终止进程不会报未处理异常。
* [ ] artifact 重复注册不会创建无界重复记录。
* [ ] 相同 request/idempotency key 与不同输入组合必须被拒绝。
* [ ] 重试后返回与第一次调用一致的核心结果。

---



# 13. 自动化测试要求

至少新增以下测试类别：

```text
tests/
  unit/
    test_view_image.py
    test_image_validation.py
    test_workspace_plan.py
    test_artifact_registry.py
    test_pty_process.py
    test_path_security.py

  integration/
    test_pty_python_repl.py
    test_pty_long_running_server.py
    test_artifact_from_process.py
    test_plan_persistence.py
    test_workspace_report_extensions.py
    test_server_restart_recovery.py

  regression/
    test_existing_tools.py
    test_run_pwsh_backward_compatibility.py
    test_workspace_isolation.py

  e2e/
    test_playwright_failure_workflow.py
    test_chatgpt_mcp_image_workflow.md
```

通过要求：

* [ ] Unit tests 全部通过。
* [ ] Integration tests 全部通过。
* [ ] Regression tests 全部通过。
* [ ] E2E 测试有记录和证据。
* [ ] 测试不依赖开发者机器上的偶然状态。
* [ ] 测试创建的进程、workspace 和 artifact 自动清理。
* [ ] Windows 测试覆盖 PowerShell 和 ConPTY。
* [ ] WSL 支持若纳入本阶段，必须单独有条件测试。

---

# 14. ChatGPT MCP 实际验收脚本

必须由 ChatGPT 网页端通过 MCP 完成以下完整任务，不能直接在本地手工替代。

## 任务


1. 创建 workspace。
2. 获取 repo map。
3. 创建 workspace plan。
4. 启动开发服务器。
5. 运行 Playwright 测试。
6. 测试失败并生成截图。
7. 通过 artifact 工具发现截图。
8. 通过 `view_image` 查看截图。
9. 根据截图和代码定位问题。
10. 修改代码。
11. 重新运行测试。
12. 测试通过。
13. 停止开发服务器。
14. 更新 plan，将所有步骤标记完成。
15. 获取 workspace report。
16. 检查最终 Git diff。

最终证据必须包含：

* workspace ID
* plan revision
* process IDs
* screenshot artifact ID
* 修改文件列表
* 测试通过结果
* 最终 Git diff
* 无残留运行进程
* 主仓库未被修改

以下任一情况发生则 E2E 验收失败：

* 用户需要手动上传截图。
* 用户需要手动向进程输入内容。
* ChatGPT 无法识别 artifact。
* 开发服务器无法被停止。
* 测试虽然通过，但 workspace report 缺少证据。
* 主仓库产生非预期修改。
* MCP Server 崩溃或失去响应。

---

# 15. 缺陷等级

## Critical

* 主仓库或 workspace 外文件被误删、误改。
* 可通过路径逃逸读取敏感文件。
* 跨 workspace 数据泄露。
* MCP Server 持续崩溃。
* 进程无法终止并持续占用系统资源。
* 凭据出现在工具响应或日志中。

## High

* `view_image` 无法稳定读取常规 PNG/JPEG。
* PTY 无法完成基本输入输出。
* plan 重启后丢失。
* artifact 无法与 workspace 隔离。
* 现有核心工具出现回归。
* 进程树终止不完整。
* 错误操作导致数据库状态不一致。

## Medium

* 非核心图片格式不支持。
* 错误信息不够清晰。
* artifact 分类不准确。
* 性能略低于目标。
* plan UI 或摘要展示不足。

## Low

* 文档问题。
* 非关键字段命名不一致。
* 边缘日志格式问题。

---

# 16. 最终验收清单

## 功能

* [ ] `view_image` 可用。
* [ ] PTY 可用。
* [ ] 可向进程写入输入。
* [ ] 可发送 interrupt、EOF 和 terminate。
* [ ] Artifact 可注册、列出、读取和查看。
* [ ] Workspace Plan 可创建、更新、读取和恢复。
* [ ] `get_workspace_report` 已集成新增能力。
* [ ] `get_capabilities` 已声明新增能力。


## 可靠性

* [ ] Server 重启恢复测试通过。
* [ ] 并发测试通过。
* [ ] 幂等测试通过。
* [ ] 长输出测试通过。
* [ ] 长期运行进程测试通过。
* [ ] Artifact 清理测试通过。

## 回归

* [ ] 原有 22 个工具全部通过。
* [ ] 非 PTY `run_pwsh` 保持兼容。
* [ ] Git worktree 隔离保持有效。
* [ ] 主仓库未被污染。
* [ ] 现有 acceptance report 未被破坏。

## 端到端

* [ ] ChatGPT 完成 Playwright 截图定位任务。
* [ ] ChatGPT 完成交互式进程任务。
* [ ] 最终 report 包含完整证据。
* [ ] 不需要人工补充本地操作。
* [ ] 所有资源和进程均被正确清理。

---

# 17. 验收结论模板

```text
Release:
Commit:
Tester:
Date:
Environment:

Automated tests:
- Unit:
- Integration:
- Regression:
- E2E:

P0 passed:
P0 failed:

Critical defects:
High defects:
Medium defects:
Low defects:

Performance result:
Security result:
Backward compatibility result:
ChatGPT MCP E2E result:

Final decision:
[ ] PASS
[ ] PASS WITH CONDITIONS
[ ] FAIL

Conditions or unresolved issues:
1.
2.
3.

Evidence:
- Test report:
- Workspace report:
- Git diff:
- Process log:
- Artifact IDs:
```
