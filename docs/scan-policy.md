# B-02 项目扫描策略约定

- 状态：已实现扫描策略内核，尚未执行目录盘点
- 实现：`tools/scan_policy.py`
- 测试：`tests/test_scan_policy.py`
- Schema：`llmwiki-scan-policy` v1

## 1. 本任务解决什么

B-02 把“哪些路径属于扫描边界、哪些内容可以本地读取、哪些原始内容可以发送给外部模型”变成确定性、可解释、可测试的策略。

它只做策略判断，不遍历项目目录，不生成 `manifest.jsonl`，不计算源文件 hash，不提取内容，也不调用 LLM。B-03 将使用本策略进行全量目录盘点。因此，当前调用 `load_scan_policy()` 不等于完成项目扫描。

策略加载和判断都是源项目只读操作，不会创建、修改或删除被扫描项目中的文件。项目内的 `.llmwikiignore` 只有在用户或项目本身已经提供时才会被读取。

## 2. Python API

```python
from tools.scan_policy import ScanPolicyConfig, load_scan_policy

config = ScanPolicyConfig(
    include_patterns=("node_modules/research-tool/**",),
    exclude_patterns=("private-drafts/**",),
    sensitive_patterns=("internal-notes/**",),
    max_content_file_bytes=100 * 1024 * 1024,
    max_raw_external_send_bytes=5 * 1024 * 1024,
    follow_symlinks=False,
    external_send_mode="local-only",
)
policy = load_scan_policy("/path/to/research-project", config=config)

boundary = policy.decide_path("results/ablation.pdf", is_directory=False)
access = policy.decide_file("models/model.ckpt", size_bytes=900_000_000)
```

`policy.as_dict()` 返回带 `schema_version`、`kind`、有效配置和全部规则来源的机器可读快照。B-03/B-04 后续可以把该快照与扫描代次关联，从而判断扫描边界是否发生变化。

## 3. `.llmwikiignore` 语法

项目根目录可以放置可选文件：

```text
<project-root>/.llmwikiignore
```

支持的稳定子集：

- 空行和以 `#` 开头的注释；
- `*`、`?` 和字符集合 glob；
- 作为完整路径段的 `**`，表示零个或多个目录；
- 末尾 `/` 表示目录规则，并作用于该目录下的内容；
- 开头 `/` 表示从项目根锚定；
- 不含 `/` 的模式匹配任意层级的文件名或目录名；
- 开头 `!` 表示重新包含；
- `\#`、`\!` 可匹配字面量开头；
- Windows 风格反斜杠会规范化为 `/`。

示例：

```gitignore
# 大型中间产物默认不进入边界
artifacts/tmp/

# 但保留关键结果
!artifacts/tmp/final_metrics.csv

*.scratch
/root-only-cache/
```

`.llmwikiignore` 必须是项目根下不超过 1 MiB 的普通 UTF-8 文件，不能是符号链接。绝对路径、盘符路径、`..`、空模式和损坏编码会直接报配置错误，不会带着不确定策略继续扫描。

## 4. 规则优先级和冲突规则

从高到低：

1. **受保护排除**：`.git/`、`.hg/`、`.svn/`、`.llmwiki/`，任何用户规则都不能覆盖；
2. **显式 exclude**：调用方提供的排除规则；
3. **显式 include**：调用方提供的重新包含规则；
4. **`.llmwikiignore`**：同一路径最后一条匹配规则生效；
5. **可覆盖的默认排除**；
6. **默认包含**：没有任何排除规则命中的路径进入扫描边界。

确定性冲突约定：

- 完全相同的显式 include/exclude 模式属于配置错误；
- 模式存在范围重叠时，显式 exclude 胜出；
- 受保护排除始终胜出；
- `.llmwikiignore` 按行号后者胜出；
- 被排除目录若可能存在后续重新包含项，`PathDecision.traverse` 会保持为 `true`，使 B-03 能进入必要的祖先目录，而不是错误剪枝；
- 每个决定都包含 `reason_code`、可读 `reason` 和命中规则来源/行号。

默认排除只覆盖明显的依赖副本、缓存和编辑器/操作系统噪声，例如 `node_modules/`、虚拟环境、Python 缓存、Notebook checkpoint 和 `*-wiki/` 旧生成目录。`data/`、`results/`、`build/`、`dist/` 等可能包含科研证据的目录不会被宽泛默认排除。
## 5. 文件大小与本地读取

默认阈值：

| 设置 | 默认值 | 含义 |
|---|---:|---|
| `max_content_file_bytes` | 100 MiB | 超过后仍保留在扫描账本中，但原始内容只允许元数据级处理 |
| `max_raw_external_send_bytes` | 5 MiB | 超过后禁止把整个原始文件作为一次外部模型载荷 |

文件大小不会把文件从目录账本中静默删除。B-02 返回的是访问资格：

- `allowed`：在边界内、路径不敏感且未超过本地原始内容阈值；
- `metadata_only`：敏感路径或超过本地阈值；
- `blocked`：文件本身处于扫描边界之外。

这里的 `metadata_only` 是 B-02 的访问保护结果；B-05～B-07 还会根据格式、科研角色、引用关系和目标决定最终阅读深度。例如，小型模型权重即使未触发通用大小阈值，后续格式策略仍可将它设为元数据读取。

## 6. 敏感路径

默认敏感路径包括 `.env`、凭证文件、私钥/证书容器、SSH 私钥以及常见云服务凭证位置。用户只能通过 `sensitive_patterns` 增加敏感范围，不能取消内置保护。

敏感路径仍进入“发现了该文件”的账本，但：

- 不读取原始内容，只保留安全元数据；
- 永远不会被批准发送原始内容到外部模型；
- 显式 include 只能改变扫描边界，不能绕过敏感内容保护。

路径规则不等于完整的 secret scanner。文件内容中的 token 检测、脱敏和 prompt injection 防护属于 J-02；在这些能力完成前，B-02 对已知敏感容器采取 fail-closed 行为。

## 7. 外部模型发送策略

`external_send_mode` 有三种模式：

| 模式 | 行为 |
|---|---|
| `local-only` | 默认模式；所有项目原始内容都禁止由 Research Core 发送到外部模型，直到用户配置隐私策略并明确放行 |
| `safe` | 显式启用后，仅允许边界内、非敏感、未命中外部排除且不超过 5 MiB 的原始文件 |
| `allowlist` | 只有命中 `external_include_patterns` 的安全路径可发送；外部 exclude 仍优先 |

无论选择哪种模式，受保护/敏感路径和超过原始发送大小阈值的文件都不能被规则绕过。这里控制的是 **Research Core 主动提交给另外配置的外部模型 API 的原始文件载荷**。它不能替代宿主 Agent 自身的权限边界：如果用户已经授权 Codex、Claude Code 等宿主直接读取工作区，宿主的文件访问还需由宿主沙箱和用户授权控制。

后续提取器可以在不发送整个原始文件的情况下生成小型、安全、可定位的文本块；这些块仍必须经过外部发送和预算检查，不能因为原文件已本地提取就自动放行。

## 8. 符号链接

默认 `follow_symlinks=False`：符号链接条目可以被 B-03 记录，但不会被跟随。

显式启用后仍有不可绕过的保护：

1. 目标必须位于规范化后的项目根目录内；
2. 目标已出现在当前祖先链时判定为循环；
3. 目标已通过其他路径访问时不重复跟随；
4. 损坏、不可解析或解析时形成循环的链接 fail closed；
5. 每次拒绝都返回稳定 `reason_code`，而不是静默跳过。

B-02 提供 `assess_symlink_target()` 和 `decide_symlink()`，但不自行遍历目录。B-03 的目录盘点器负责维护祖先集合和全局已访问目标集合。

## 9. B-02 完成前后差异

| 改动前 | 改动后 |
|---|---|
| 旧流程主要依赖支持格式白名单 | 所有后续盘点可先使用项目级边界策略 |
| 默认排除和用户覆盖没有统一语义 | 规则优先级、冲突和重包含行为已固定 |
| 大文件可能被直接跳过 | 保留发现记录，内容访问降为元数据级并给出理由 |
| 符号链接行为未定义 | 默认不跟随；启用时限制根目录、循环和重复目标 |
| `.env` 等可能混入模型上下文 | 敏感路径原始内容本地不读且禁止外发 |
| 无法解释为什么排除某文件 | 每个决定都有规则来源、行号、reason code 和说明 |

## 10. 明确非目标

B-02 不实现：

- 项目注册更新或扫描配置持久化 UI；
- 目录遍历、文件数量统计和排除摘要（B-03）；
- 源文件内容 hash、scan generation 和增量复用（B-04）；
- 格式、语言和科研角色识别（B-05）；
- Manifest 最终双轴状态（B-06）；
- 内容提取、LLM 调用、MCP、Hook 或 Web 页面。

最终用户仍会通过“一键理解项目”触发完整流水线；`ScanPolicyConfig` 是内部可组合协议，不要求用户在最终产品中手写 Python。