# B-05 确定性文件分类

- 状态：已实现
- 实现：`tools/file_classification.py`、`tools/project_inventory.py`
- 命令：`python tools/project.py inventory <project_id> --json`
- 测试：`tests/test_file_classification.py`、`tests/test_project_inventory.py`
- Schema：`llmwiki-project-manifest` Schema v1 / `project-inventory-v3`

## 1. 本任务解决什么

B-05 在 B-04 可审计目录账本与文件指纹之上，为每条扫描范围内普通 `file` 记录增加本地、确定性、可解释的分类结果：

- 文件格式；
- 稳定 MIME/media type；
- 主要文件语言；
- 科研角色；
- 每个维度的规则来源、稳定原因码和解释。

分类主流程不依赖 LLM、网络或外部服务。源科研项目仍然只读。只有 B-02 策略判定 `local_content_access=allowed` 的文件才会读取有限前缀，并且该前缀只在进程内用于 signature、shebang 和文本结构判断，不写入 Manifest，也不复制到 curated Markdown。敏感路径和超大文件在完成 B-04 规定的本地 SHA-256 后保持 path-only 分类，不会调用分类前缀读取器。

## 2. Manifest artifact v3

全局机器记录 Schema 仍为 v1；`manifest_version` 从 B-04 的 `project-inventory-v2` 升级为：

```json
{
  "schema_version": 1,
  "kind": "llmwiki-project-manifest",
  "manifest_version": "project-inventory-v3",
  "record_type": "file",
  "path": "src/model.py",
  "classification": {
    "schema_version": 1,
    "kind": "llmwiki-file-classification",
    "format": "python",
    "media_type": "text/x-python",
    "language": "python",
    "research_role": "source_code",
    "reasons": {
      "format": {
        "source": "content-or-path",
        "code": "extension-match",
        "detail": "recognized extension .py"
      },
      "language": {
        "source": "format",
        "code": "language-from-format",
        "detail": "language is implied by format python"
      },
      "research_role": {
        "source": "format",
        "code": "source-code-format",
        "detail": "source format is python"
      }
    }
  }
}
```

所有 `file` 行都必须有合法分类。无法识别的二进制不会消失，而是显式写入：

```text
format=unknown / language=unknown / research_role=unknown
```

排除文件、剪枝目录、符号链接和特殊目录项仍沿用 B-03/B-04 记录语义，不伪造普通文件分类。

## 3. 确定性优先级

格式判断按强信号优先：

1. 已知 magic/signature（例如 PDF、PNG、JPEG、HDF5、NumPy、Parquet、SQLite）；
2. ZIP 容器签名与已知容器扩展名组合（DOCX/PPTX/XLSX/NPZ）；
3. shebang；
4. 文件内容结构（JSON、Notebook JSON、XML、HTML、SVG、LaTeX）；
5. 已知扩展名；
6. 已知无扩展名文件（README、LICENSE、Makefile、Dockerfile 等）；
7. 可解码文本或显式未知二进制 fallback。

强 magic 会覆盖伪装成源码的扩展名；当 `.pdf` 等二进制扩展缺少必要 signature 且内容明显是文本时，不会把它误报为 PDF。无扩展名脚本可由 shebang 识别，无扩展名 Notebook 可由 JSON 结构识别。

科研角色优先使用项目相对路径和已知文件名，再使用格式 fallback。当前稳定角色包括项目文档、普通文档、源码、测试、自动化、配置、依赖清单、Notebook、论文、参考文献、数据集、实验、结果、运行日志、图、模型产物、项目元数据和 `unknown`。

### B-02 内容访问门控

分类前缀读取受当前扫描策略逐文件门控：

- 只有逻辑路径和解析后的项目内物理路径都允许 `local_content_access=allowed` 时，才调用 `_read_classification_sample`；
- `metadata_only` 或 `blocked` 文件只使用相对路径、文件名和扩展名分类；敏感文件保留 `sensitive-path`，超大文件保留 `content-size-limit` 等 B-02 原因码与解释；
- 受限分类的 `reasons.format.source` 固定为 `policy-path`，因此审计者可以区分内容信号和仅路径信号；
- `sample=None` 明确表示策略不允许读取分类前缀，与允许读取但文件为空时的 `sample=b""` 不同；
- 复用旧分类不仅要求内容 hash 相同，还要求分类与当前 B-02 内容访问依据兼容：path-only 与 sampled 模式发生变化时必须重新分类，不能沿用不符合当前策略的结果。

## 4. 分类汇总与对账

`summary.classification_summary` 是 Schema v1 机器记录，包含：

- `classified_files`：必须等于 `record_counts.file`；
- `reused_files`：内容 hash 未变且安全复用上一代分类的文件数；
- `formats`、`languages`、`research_roles`：按稳定值计数，各自总和必须等于普通文件数。

读取现有 v3 Manifest 时，分类对象和汇总会 fail closed 校验。格式、语言、角色枚举或 reason 结构损坏时，不会静默覆盖现有 Manifest。

## 5. 增量与兼容

- B-03 `project-inventory-v1` 可继续读取，并从 generation 0 重新 hash、分类；
- B-04 `project-inventory-v2` 的 generation 和 SHA-256 缓存可继续复用，只补分类；
- B-05 `project-inventory-v3` 在内容 hash 相同时复用已有分类；
- future Schema 或未知 artifact 版本 fail closed；
- 扫描、hash 或分类中途失败时保留上一份 Manifest，并清理临时文件。

分类只依赖项目相对路径以及在 B-02 允许时读取的有限本地字节前缀。受限文件只依赖路径信号；允许读取的有限前缀不持久化、不发送外部模型，也不进入 `wiki/projects/`。

## 6. 安全边界与非目标

B-05 保留 B-01～B-04 的注册、路径边界、排除对账、符号链接、原子替换、文件指纹和源项目零写入保证。本任务不实现：

- `processing_status`、`read_depth` 和最终处理理由（B-06）；
- 阅读优先级或引用提升队列（B-07）；
- 内容提取、chunk、Locator、`source_id` 或 Evidence；
- LLM 补充分类、MCP、Hook、Web 或独立聊天入口；
- reconciliation 变化事件或知识页面生成。
