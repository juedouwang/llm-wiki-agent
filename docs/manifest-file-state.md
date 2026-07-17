# B-06 Manifest 文件状态双轴

- 状态：已实现
- 实现：`tools/file_state.py`、`tools/project_inventory.py`
- 命令：`python tools/project.py inventory <project_id> --json`
- 测试：`tests/test_file_state.py`、`tests/test_project_inventory.py`
- Schema：`llmwiki-project-manifest` Schema v1 / `project-inventory-v4`
- B-07 downstream boundary：2026-07-17 已实现独立 `indexes/reading-priority.json`，不修改本 Schema

## 1. 本任务解决什么

B-06 在 B-05 分类账本之上，把“处理是否完成”和“实际读取了多深”拆成两个独立维度。每条扫描范围内普通 `file` 记录都新增版本化 `file_state`，并且必须有稳定原因码和非空解释。这样，策略忽略、格式不支持、读取失败和只读元数据不会再被压缩成一个模糊状态。

本任务仍然只是 Research Core 的确定性盘点状态契约，不执行内容提取，也不会把 B-05 的分类前缀读取伪装成已完成处理。

## 2. Schema v1

Manifest writer 从 B-05 `project-inventory-v3` 升级为 `project-inventory-v4`。普通文件行包含：

```json
{
  "schema_version": 1,
  "kind": "llmwiki-project-manifest",
  "manifest_version": "project-inventory-v4",
  "record_type": "file",
  "path": "src/model.py",
  "file_state": {
    "schema_version": 1,
    "kind": "llmwiki-file-state",
    "processing_status": "discovered",
    "read_depth": "sampled",
    "reason_code": "classification-sample",
    "reason": "a bounded local prefix was read for deterministic classification; content extraction has not been attempted"
  }
}
```

`file_state` 必须恰好包含 Schema v1 字段。缺字段、多字段、空理由、非法原因码、future Schema 和未知 kind 都会 fail closed，旧 Manifest 不会被静默覆盖。

## 3. 两轴枚举

`processing_status` 使用产品契约中的稳定枚举：

```text
discovered | processed | partial | failed | missing
```

`read_depth` 使用：

```text
deep_read | normal_read | sampled | metadata_only | ignored | unsupported
```

当前有效组合为：

| processing_status | 允许的 read_depth |
|---|---|
| `discovered` | `sampled`、`metadata_only`、`ignored`、`unsupported` |
| `processed` | 全部六种读取深度 |
| `partial` | `deep_read`、`normal_read`、`sampled`、`metadata_only` |
| `failed` | `deep_read`、`normal_read`、`sampled`、`metadata_only` |
| `missing` | 仅 `metadata_only` |

例如 `missing/sampled`、`discovered/deep_read`、`partial/ignored` 都是非法组合。组合校验集中在 `tools/file_state.py`，后续提取器不能绕过该契约写入任意字符串。

## 4. 当前 inventory 的诚实初始状态

B-06 不提前实现 C 阶段提取，因此当前 inventory 生成的普通文件仍全部是 `processing_status=discovered`。`read_depth` 反映 B-02/B-05 已真实发生的访问：

- B-02 允许读取且格式已知：`sampled / classification-sample`；仅表示 B-05 为分类读取过有限前缀，不表示正文已提取；
- B-02 `sensitive-path`：`ignored / sensitive-path`；不调用分类前缀读取器；
- B-02 `content-size-limit` 或其他本地内容限制：`metadata_only / <policy-reason>`；
- 确定性分类仍为 `unknown`：`unsupported / unsupported-format`；文件记录不会消失。

B-04 本地 SHA-256 流式读取不算语义读取深度；它只建立内容身份，不持久化原始字节。

B-07 读取当前 v4 中的 `classification` 与 `file_state` 作为输入，但把 `current_read_depth`、`recommended_read_depth`、优先级、selected/deferred 状态和引用信息写入独立 [`reading-priority.json`](reading-priority.md) 建议层。它不会回写 `processing_status`、`read_depth`、原因码或状态汇总，也不会为这些建议创建 `project-inventory-v5`。后续消费者必须先通过 `load_current_reading_priority(...)` 将建议与精确的当前 Manifest 和策略重新对账，再执行 `deep_read_status=selected` 项；裸 `load_reading_priority(...)` 只做结构解析，不是执行授权。只有消费者真正执行并按其自身契约持久化结果时，Manifest 真相才可能在独立任务中变化。

## 5. 汇总与对账

Manifest summary 新增 `file_state_summary`：

```json
{
  "schema_version": 1,
  "kind": "llmwiki-file-state-summary",
  "state_files": 4,
  "reused_files": 0,
  "processing_statuses": {"discovered": 4},
  "read_depths": {"ignored": 1, "metadata_only": 1, "sampled": 1, "unsupported": 1},
  "reasons": {
    "classification-sample": 1,
    "content-size-limit": 1,
    "sensitive-path": 1,
    "unsupported-format": 1
  }
}
```

`state_files` 以及三个分类计数总和都必须等于 `record_counts.file`。读取 v4 时，记录与汇总逐项对账；任何不一致都保留原 Manifest 并失败退出。当前 B-08 基于这些确定性字段生成独立 `indexes/coverage-report.json`；当前 B-07 则生成独立 `indexes/reading-priority.json`。两者都不属于 Manifest `file_state`，B-06 本身不生成这些后置 artifact。

## 6. 增量与兼容

- B-03 `project-inventory-v1`、B-04 `project-inventory-v2` 和 B-05 `project-inventory-v3` 都可兼容读取；升级时为每个普通文件补充状态；
- v2 的安全指纹复用和 v3 的安全分类复用保持不变；
- v4 内容 hash 未变且当前 B-02 访问依据兼容时，可复用已有状态，包括后续阶段写入的更深读取状态；
- 文件内容变化时状态回到当前 inventory 的诚实初始状态；
- 当前策略变为敏感或超大限制时，不会沿用与该限制冲突的旧深读状态；
- `missing` 状态不会在文件重新出现后被错误复用；
- future Manifest、future `file_state` Schema 或损坏状态 fail closed。

## 7. 安全边界与非目标

B-06 保留 B-01～B-05 的项目身份、扫描边界、排除对账、符号链接、指纹、分类、原子替换和源项目零写入保证。本任务不实现：

- B-06 本身不实现阅读优先级或引用提升队列；当前 B-07 已通过独立 artifact 实现，见 [`reading-priority.md`](reading-priority.md)；
- B-06 本身不生成覆盖率/失败报告；当前 B-08 已通过独立 artifact 实现，见 [`coverage-report.md`](coverage-report.md)；
- 内容提取、Block、Locator、chunk、`source_id` 或 Evidence；
- MCP、Hook、Web、LLM 分类补充或独立聊天入口；
- curated Markdown 生成或 reconciliation 事件。
