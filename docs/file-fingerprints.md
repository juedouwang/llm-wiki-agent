# B-04 文件指纹与增量 Manifest

- 状态：已实现
- 实现：`tools/project_inventory.py`
- 命令：`python tools/project.py inventory <project_id> --json`
- 测试：`tests/test_project_inventory.py`
- Schema：`llmwiki-project-manifest` Schema v1 / `project-inventory-v2`

## 1. 本任务解决什么

B-04 在 B-03 的完整目录账本上增加确定性文件指纹和扫描代次。它仍然只接受 B-01 已注册的 `project_id`，继续使用 B-02 的扫描边界、排除对账和符号链接安全判断，并只替换：

```text
.llmwiki/projects/<project_id>/manifest.jsonl
```

所有扫描范围内的普通文件仍然都有记录，不受扩展名或后续格式支持情况影响；被排除的文件、剪枝目录、符号链接和特殊目录项继续按 B-03 账本语义记录。B-04 不修改源科研项目，也不把 Research Core 变成独立聊天机器人。

## 2. CLI 与返回结果

```powershell
python tools/project.py inventory <project_id> `
  --workspace-root <research-core-root> `
  --json
```

JSON 结果在 B-03 计数之外增加：

- `scan_generation`：本次成功 Manifest 的扫描代次；
- `fingerprint_summary.algorithm`：固定为 `sha256`；
- `fingerprint_summary.cache_strategy`：固定为 `file-identity-change-v1`；
- `fingerprint_summary.hashed_files`：本次实际读取并计算 SHA-256 的普通文件数；
- `fingerprint_summary.reused_files`：本次安全复用上一代 SHA-256 的普通文件数。

非 JSON CLI 也会显示上述代次、重新计算数和复用数。

## 3. Manifest artifact v2

全局机器记录 Schema 仍为 v1；`manifest_version` 是独立的 artifact 版本。本任务把当前写入版本从 `project-inventory-v1` 升级为 `project-inventory-v2`。

JSONL 第一行仍是 `summary`。它新增：

```json
{
  "schema_version": 1,
  "kind": "llmwiki-project-manifest",
  "manifest_version": "project-inventory-v2",
  "record_type": "summary",
  "project_id": "my-study-0123456789ab",
  "scan_generation": 2,
  "fingerprint_summary": {
    "algorithm": "sha256",
    "cache_strategy": "file-identity-change-v1",
    "hashed_files": 1,
    "reused_files": 3
  }
}
```

每条扫描范围内的普通 `file` 记录新增：

```json
{
  "record_type": "file",
  "path": "results/custom-output.unknown",
  "content_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "size_bytes": 123,
  "mtime_ns": 1784119106320512400,
  "fingerprint_cache": {
    "strategy": "file-identity-change-v1",
    "identity": "device:inode",
    "change_token": "filesystem-specific-change-marker"
  }
}
```

只有 `file` 记录拥有这些字段。`excluded_file`、`excluded_directory`、`symlink`、`special_entry` 和 `skipped_directory` 不计算或保存内容指纹。

`fingerprint_cache` 只属于本机增量复用元数据，不是跨机器稳定标识，也不是后续 Evidence 或 `source_id`。当当前文件系统不能提供可靠的文件身份或变更标记时，该字段为 `null`，扫描器会保守地重新计算 SHA-256。

## 4. scan generation

代次语义固定如下：

1. 没有旧 Manifest 时，首次成功扫描写入 generation 1；
2. 读取有效 B-03 `project-inventory-v1` 时，把它视为 generation 0，升级后的首次成功扫描写入 generation 1；
3. 读取有效 B-04 `project-inventory-v2` 时，下一次成功替换写入旧代次加一；
4. 扫描、指纹或写入失败不会替换旧 Manifest，也不会消费代次；
5. `scan_generation` 只在 summary 中出现，避免仅因代次增长而改写每条文件记录。

因此，未变化项目的下一代 Manifest 会因 summary 中的代次和复用统计而变化；B-03 的“整个文件字节稳定”不再是当前契约。

## 5. 安全增量复用

上一代 SHA-256 只有在以下条件同时成立时才复用：

- Manifest 相对路径相同；
- `size_bytes` 相同；
- `mtime_ns` 相同；
- 文件系统身份相同；
- 文件系统变更标记相同。

仅按路径、大小和 mtime 复用是不安全的，因为内容可以在保持大小后再恢复 mtime。当前变更标记为：

- Windows：通过 `GetFileInformationByHandleEx(FileBasicInfo)` 读取文件 `ChangeTime`；
- POSIX：使用 `st_ctime_ns`；
- 其他或能力不足的文件系统：不复用，重新计算 SHA-256。

只触碰 mtime 会使快速复用失效并触发一次重新 hash，但相同内容仍得到相同 `content_sha256`。B-04 不输出 `content_changed` 等变化分类；变化归因和 reconciliation 属于 H-01。重命名后的路径不会按旧路径复用，即使内容相同也允许重新 hash。

## 6. 文件变化与原子失败

为避免在文件正被修改时提交不一致指纹，每次计算会检查：

- 路径在计算前后仍指向普通文件；
- 打开的文件描述符与路径快照身份一致；
- 大小、mtime、身份和变更标记在计算前后保持一致。

检测到并发变化时最多重试 3 次。持续变化、权限拒绝或其他读取错误会使整个盘点失败，而不是写入缺少文件或错误 hash 的“部分成功” Manifest。

记录先写入机器状态目录中的临时文件，全部遍历、指纹和摘要对账成功后才通过 `os.replace()` 原子替换当前 Manifest。任何失败都保留旧 Manifest，并清理临时文件；源项目始终零写入。

## 7. 旧 Manifest 与损坏数据

读取器兼容有效的 B-03 `project-inventory-v1`，但旧版本没有指纹，因此升级扫描会重新 hash 所有当前普通文件。

对当前 v2，读取器会校验 Schema、kind、项目身份和根路径、artifact 版本、summary 首行、记录计数、总数、重复路径、generation、SHA-256、size/mtime、缓存策略以及指纹统计对账。以下情况 fail closed，不会静默覆盖：

- JSONL 损坏或存在空行；
- future Schema；
- 未知 `manifest_version`；
- 项目身份、根路径或记录计数不一致；
- 指纹字段或摘要无法对账。

## 8. 本地 hash 与内容访问边界

SHA-256 必须读取文件字节，但 B-04 只把字节流送入本地单向摘要状态：

- 不提取文本或二进制内容；
- 不持久化原始字节或内容片段；
- 不把原始内容发送给外部模型；
- 不调用 LLM；
- 不把任何内容写回源项目。

B-02 的 `local_content_access` 继续约束后续“打开、提取和语义处理”能力。B-04 的本地完整性 hash 是独立的确定性账本操作：对所有边界内普通文件执行，包括被标记为敏感或后续只能元数据处理的文件。Manifest 位于默认忽略 Git 的本机 `.llmwiki/` 状态目录；其中的路径、hash 和文件系统缓存键不应复制到 curated Markdown 或公开提交。

## 9. 明确非目标

B-04 不实现：

- 格式、MIME、语言或科研角色分类（B-05）；
- `processing_status`、`read_depth` 或最终原因枚举（B-06）；
- 文件内容提取、Evidence locator、`source_id` 或知识页生成；
- 新增/修改/删除等 reconciliation 事件分类（H-01）；
- LLM、MCP、Hook、Web、本地 API 或宿主适配；
- 旧数据的自动迁移或源项目写入。
