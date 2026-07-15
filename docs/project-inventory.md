# B-03 目录盘点与基础 Manifest

- 状态：已实现
- 实现：`tools/project_inventory.py`
- 命令：`python tools/project.py inventory <project_id> --json`
- 测试：`tests/test_project_inventory.py`
- Schema：`llmwiki-project-manifest` / `project-inventory-v1`

## 1. 本任务解决什么

B-03 为 B-01 已注册的科研项目建立第一份可对账目录账本。盘点器只从 `project.yaml` 读取项目身份和源根目录，加载 B-02 的 `ScanPolicy`，然后遍历允许的目录边界，把结果原子写入：

```text
.llmwiki/projects/<project_id>/manifest.jsonl
```

源科研项目保持只读。盘点不会接受一个临时源路径来绕过注册记录，也不会把 Manifest 写回源目录。

## 2. CLI

```powershell
python tools/project.py inventory <project_id> `
  --workspace-root <research-core-root> `
  --include "node_modules/research-tool/**" `
  --exclude "private-drafts/**" `
  --follow-symlinks `
  --json
```

- `project_id` 必须已经由 B-01 注册；
- `--include` 和 `--exclude` 可重复，语义及优先级完全由 B-02 决定；
- 符号链接默认只记录、不跟随；
- `--follow-symlinks` 只启用 B-02 允许的项目根内安全目标，不会放宽受保护路径。

Python API：

```python
from tools.project_inventory import inventory_project
from tools.scan_policy import ScanPolicyConfig

result = inventory_project(
    workspace_root="/path/to/llm-wiki-agent",
    project_id="my-study-0123456789ab",
    policy_config=ScanPolicyConfig(
        include_patterns=("node_modules/research-tool/**",),
        exclude_patterns=("private-drafts/**",),
        follow_symlinks=False,
    ),
)

print(result.manifest_file)
print(result.record_counts["file"])
```

## 3. 盘点与排除规则

盘点器对目录项进行稳定排序，并遵循以下约定：

1. 每个扫描范围内的普通文件都有一条 `file` 记录，与扩展名和后续是否支持提取无关；
2. 被规则排除但其父目录仍被遍历的普通文件写为 `excluded_file`，不会静默遗漏；
3. `PathDecision.traverse=False` 的目录在剪枝点写一条 `excluded_directory`，并进入摘要；剪枝后不会枚举其内部文件；
4. 为寻找 `.llmwikiignore` 或显式规则重新包含的后代，`included=False, traverse=True` 的祖先目录仍会进入；
5. 非普通文件、不可读元数据和非符号链接 reparse point 写为 `special_entry`；
6. 重复物理目录不会重复遍历，而是写为 `skipped_directory`；
7. 项目根本身计入 `directories_scanned`，但不写目录记录。

摘要中的 `exclusion_summary` 按稳定 `reason_code` 分组。对每个分组：

```text
total_records = excluded_files + pruned_directories
```

所有分组之和可分别与 Manifest 中的 `excluded_file` 和 `excluded_directory` 行数对账。

## 4. 符号链接安全

每个符号链接都有一条 `symlink` 记录。B-03 调用 B-02 的 `decide_symlink()`，并维护：

- 当前目录的真实祖先链；
- 已排队或已扫描的真实目录集合；
- 已安全接受的其他符号链接目标集合。

普通项目路径会先于符号链接处理，使真实路径优先于别名。启用跟随后仍会拒绝：

- 项目根外目标；
- 指向祖先的循环；
- 已访问目标；
- 损坏或不可解析目标；
- 逻辑链接路径或规范化目标路径被扫描边界排除的目标。

`symlink` 行同时保存 B-02 原始决定、最终是否跟随以及最终 `follow_reason_code`；`symlink_summary` 可与这些记录对账。

## 5. Manifest Schema v1

JSONL 第一行是 `summary`，后续每行是一条目录盘点记录。所有行都包含：

```json
{
  "schema_version": 1,
  "kind": "llmwiki-project-manifest",
  "manifest_version": "project-inventory-v1",
  "record_type": "summary | file | excluded_file | excluded_directory | symlink | special_entry | skipped_directory",
  "project_id": "my-study-0123456789ab"
}
```

`summary` 包含：

- `project_root`；
- `total_records` 和按类型计数的 `record_counts`；
- `directories_scanned`；
- 按原因分组的 `exclusion_summary`；
- 按最终链接原因分组的 `symlink_summary`；
- 完整的 B-02 `policy.as_dict()` 快照。

普通 `file` 行只记录相对路径和扫描边界决定。例如：

```json
{
  "schema_version": 1,
  "kind": "llmwiki-project-manifest",
  "manifest_version": "project-inventory-v1",
  "record_type": "file",
  "project_id": "my-study-0123456789ab",
  "path": "results/custom-output.unknown",
  "boundary": {
    "path": "results/custom-output.unknown",
    "included": true,
    "traverse": false,
    "reason_code": "default-include",
    "reason": "no protected, explicit, ignore-file, or default exclusion matched",
    "matched_rule": null
  }
}
```

## 6. 原子写入与失败语义

记录先写入机器状态目录中的临时文件。只有目录遍历和摘要构建全部成功后，才使用同文件系统的 `os.replace()` 替换 `manifest.jsonl`。若任何在范围内的目录无法枚举：

- 本次盘点失败并返回明确错误；
- 已存在的 Manifest 保持不变；
- 临时文件被清理；
- 源项目不产生任何写入。

未变化项目重复盘点会得到字节稳定的基础 Manifest；B-04 才会引入 scan generation 和增量复用语义。

## 7. 明确非目标

B-03 不实现：

- 文件内容读取、文本或二进制提取；
- 内容 hash、SHA-256、文件大小、mtime 或 scan generation（B-04）；
- 格式、语言和科研角色分类（B-05）；
- `processing_status`、`read_depth` 或最终原因枚举（B-06）；
- LLM 调用、MCP、Hook、Web、知识页生成或旧数据迁移。
