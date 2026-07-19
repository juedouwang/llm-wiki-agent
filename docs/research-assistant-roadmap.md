# 科研助手改造路线图（P-05 / P-08 对齐版）

- 状态：**后续开发的执行计划**
- 更新日期：2026-07-19
- 基线分支：`research-assistant`
- 产品约定：[`research-assistant-product-contract.md`](research-assistant-product-contract.md)
- 任务原则：每个编号尽量对应一个独立任务分支、一个原子提交和一个检查点标签。

## 1. 为什么需要重排原计划

A-01～A-03 的工程基线、测试基线和存储边界保持不变，且已经完成。用户确认 `P-05 Agent 原生执行闭环`，并补充确认 `P-08 Agent-native 科研工具架构` 后，B～J 阶段需要做以下调整：

1. **科研助手不再被设计成第二个聊天机器人。** Core 成为长期状态与科研能力内核，Codex/Claude Code 是主要交互和执行入口。
2. **MCP 和宿主集成不能留到最后。** Core 服务边界和最小 MCP 垂直切片必须在可信来源链路形成后尽早实现，否则后续规划仍会形成手工交接。
3. **一键导入和网站从“后期增强”提升为第一版契约。** 内部仍按阶段开发，但 R3 必须一次生成全部 15 类资料和可阅读网页。
4. **文件状态拆成两个维度。** `processing_status` 描述成功/失败，`read_depth` 描述深读/抽样/元数据等，避免一个枚举同时表达两种含义。
5. **Hooks 只是事件信号。** 真正的一致性依赖 Manifest、hash、Git diff 和 reconciliation；禁用 Hook 时也必须可恢复。
6. **初始计划与成熟规划器分开。** 一键扫描必须输出第 14、15 项草案；依赖、时间盒、执行核验和每日复盘由 I 阶段逐步增强。
7. **Codex 与 Claude Code 使用两个适配包。** 先用 Codex 做参考实现，再完成 Claude Code 等价能力；两者共享 Core 和知识库。
8. **Markdown 与网站写入边界前置。** 用户确认内容必须在第一次支持网页编辑时就具备防覆盖机制，不能等到后期补救。
9. **Roadmap 建设领域能力，不与 MCP 接口一一对应。** 宿主 Agent 负责目标理解、语义判断、规划和综合；Core 负责确定性能力、状态、Evidence、Locator、安全、版本与执行校验；MCP 只暴露少量面向任务的工具。

## 2. 已完成阶段

| 任务 | 状态 | 提交 | 检查点 |
|---|---|---|---|
| A-01 对齐目标仓库 | 已完成 | `96f43f0` | `checkpoint/a-01-repo-aligned` |
| A-02 建立基线测试 | 已完成 | `3084d7d` | `checkpoint/a-02-baseline-tests` |
| A-03 划分机器状态与知识内容 | 已完成 | `da4022c` | `checkpoint/a-03-storage-layout` |
| P-05 最终产品与 Agent 原生闭环约定 | 已完成 | `272c693` | `checkpoint/p-05-product-contract` |
| P-08 Agent-native 科研工具架构约定 | 已完成（2026-07-17 验证） | `815df6e` | `checkpoint/p-08-agent-native-architecture` |
| B-01 项目注册 | 已完成 | `15d317a` | `checkpoint/b-01-project-register` |
| B-02 扫描策略 | 已完成 | `07be63d` | `checkpoint/b-02-scan-policy` |
| B-03 目录盘点与基础 Manifest | 已完成 | `be4b825` | `checkpoint/b-03-project-inventory` |
| B-04 文件指纹与增量 Manifest | 已完成 | `f6fcd43` | `checkpoint/b-04-file-fingerprints` |
| B-05 格式、语言与科研角色识别 | 已完成 | `7ed9196` | `checkpoint/b-05-file-classification` |
| B-06 Manifest 文件状态双轴 | 已完成 | `9c6ba5d` | `checkpoint/b-06-manifest-file-state` |
| B-07 确定性自适应阅读优先级 | 已完成（2026-07-17 验证） | `248b231` | `checkpoint/b-07-adaptive-reading-priority` |
| B-08 覆盖率与失败报告 | 已完成 | `5c864a2` | `checkpoint/b-08-coverage-report` |
| C-01 提取 Schema | 已完成 | `e4b3fed` | `checkpoint/c-01-extraction-schema` |
| C-02 文本族提取器 | 已完成 | `5093f80` | `checkpoint/c-02-text-extractors` |
| C-03 Notebook extractor | 已完成 | `946d05a`, `daf1330` | `checkpoint/c-03-notebook-extractor`, `checkpoint/c-03-notebook-payload-bounds` |
| C-04 PDF page extractor | 已完成 | `66a7ba7` | `checkpoint/c-04-pdf-extractor` |
| C-05 visual/OCR extraction | complete (2026-07-17 validation) | `bb045cf` | `checkpoint/c-05-visual-ocr` |
| C-06 locator-preserving Office/tabular extraction | complete (2026-07-17 validation) | `7ebe966` | `checkpoint/c-06-office-tabular-extraction` |
| C-08 定位保真分块 | 已完成 | `1eb64a2`, `6f01136` | `checkpoint/c-08-locator-chunking`, `checkpoint/c-08-locator-chunking-hardening` |
| D-01 persistent source identity | complete | `a11fe6d` | `checkpoint/d-01-source-identity` |
| D-02 source versions and path history | complete | `1230ac2` | `checkpoint/d-02-source-versions` |
| D-03 precise Evidence schema | complete | `00f036c`, `8e13c6f` | `checkpoint/d-03-evidence-schema`, `checkpoint/d-03-evidence-version-hardening` |
| D-04 source locate/open | complete | `917475c` | `checkpoint/d-04-source-open` |
| D-05 source relocation recovery | complete | `a49c6a8` | `checkpoint/d-05-source-relocation` |
| D-06 source and Evidence health | complete | `9b38353` | `checkpoint/d-06-source-health` |
| J-01 fixed-fixture end-to-end acceptance | complete for R1 and R3-minus-C-07 (2026-07-19 validation) | `73070f1`, `9e2eba2` | `checkpoint/j-01-r1-e2e`, `checkpoint/j-01-r3-end-to-end` |
| J-03 local read-only product research cockpit | complete (2026-07-19 validation; development-supervision dashboard remains separate) | `15ae906` | `checkpoint/j-03-product-cockpit` |
| J-04 fixed-target controlled Web editing | complete (2026-07-19 validation; default cockpit remains read-only) | `cf5cfcd` | `checkpoint/j-04-controlled-web-editing` |
| G-01 Research Core service facade | complete | `baa9625` | `checkpoint/g-01-core-service` |
| G-07 minimal MCP server | complete | `7b0eef9` | `checkpoint/g-07-mcp-server` |
| G-08 budget-bounded Host Context Pack | complete | `3721fb5` | `checkpoint/g-08-host-context-pack` |
| E-01 resumable staged run orchestration | complete | `146fc0f` | `checkpoint/e-01-run-orchestrator` |
| E-02 deterministic project map | complete (2026-07-18 validation) | `c3ab7e0` | `checkpoint/e-02-deterministic-project-map` |
| E-06 experiment chains | complete (2026-07-18 validation) | `c1227f8` | `checkpoint/e-06-experiment-chains-final` |
| E-07 complete project knowledge rendering | complete (2026-07-18 validation) | `9f4194b` | `checkpoint/e-07-knowledge-rendering` |
| E-08 complete one-action project understand (R3 slice) | complete | `2da9540` | `checkpoint/e-08-complete-understand` |
| F-01 项目知识页面契约 | complete after F-01A/F-01B validation on 2026-07-17 | `829cb54` | `checkpoint/f-01b-canonical-knowledge-layout` |
| F-02 Claim-Evidence directional binding and currentness | complete after F-02A/F-02B validation, F-02C stable-object plus registry-coordination hardening, and F-02D result-integrity repair on 2026-07-18 | `ba3846c`, `564cf0a`, `41e347e`, `6c9016a`, `d84f128`, `4521be9` | `checkpoint/f-02a-claim-evidence-schema-v2`, `checkpoint/f-02b-claim-evidence-currentness`, `checkpoint/f-02c-stable-file-access`, `checkpoint/f-02c-stable-file-access-review-fix`, `checkpoint/f-02c-registry-read-write-coordination`, `checkpoint/f-02d-claim-evidence-result-integrity` |
| F-03 project research entities and directed relations | complete after F-03A validation on 2026-07-18 | `dcfa1d7`, `04856ce` | `checkpoint/f-03a-research-relations-final` |
| F-04 Claim lifecycle and explicit conflict coexistence | partial after bounded F-04A validation on 2026-07-18; persistence and controlled Markdown mutation remain outside this unit | `738e086` | `checkpoint/f-04a-claim-lifecycle` |
| F-05 controlled Markdown writing | complete after bounded F-05A planning and F-05B persistence/audit validation on 2026-07-18 | `642ab9e`, `53f4b3c` | `checkpoint/f-05a-controlled-markdown-plan`, `checkpoint/f-05b-controlled-markdown-persistence`, `checkpoint/f-05b-controlled-markdown-persistence-final` |
| H-04 host-neutral event ledger | complete | `5c2a888` | `checkpoint/h-04-host-event-ledger` |
| H-07 conservative project reconciliation | complete after validation on 2026-07-16 | `cfb7274` | `checkpoint/h-07-project-reconciliation` |
| J-05 Codex reference adapter package | complete after validation on 2026-07-16 | `62ccbdc` | `checkpoint/j-05-codex-plugin` |
| I-01 strict Goal/Milestone Schema | complete after validation on 2026-07-18 | `ddd5261` | `checkpoint/i-01-goal-schema` |
| I-02 strict internal task protocol | complete after validation on 2026-07-18 | `76a81e3` | `checkpoint/i-02-task-protocol` |
| I-03 deterministic project-state snapshot | complete after validation on 2026-07-19 | `5988961` | `checkpoint/i-03-project-state` |
| I-04 deterministic initial Goal/backlog/daily-plan drafts | complete after validation on 2026-07-19 | `c42d9a7` | `checkpoint/i-04-initial-planning` |

## 3. 后续最小改动计划

优先级：`P0` 为第一版硬门槛，`P1` 为完整第一版重要能力，`P2` 为后续增强。工作量 `S/M/L` 只表示相对大小。

### B. 项目注册、扫描与覆盖率

| ID | 最小改动 | 独立验证 | 改动前 → 改动后 |
|---|---|---|---|
| B-01 `P0/S` | 实现项目注册；规范化根路径，生成并持久化安全 `project_id`，解析可配置 `knowledge_root`，记录名称、Git 信息和可选首次登记字段；本任务不扫描文件 | 同一路径重复注册得到同一 ID；非法路径/ID 被拒绝；`project.yaml` 可按 Schema v1 读取 | Core 不知道项目身份 → 项目有稳定身份和配置入口 |
| B-02 `P0/M` | 定义扫描策略：`.llmwikiignore`、显式 include/exclude、文件大小、符号链接、敏感路径和外部发送策略 | fixture 覆盖默认排除、用户覆盖、符号链接循环、冲突规则和配置错误 | 固定白名单 → 每项目有可解释扫描边界 |
| B-03 `P0/M` | 只做目录盘点，生成所有“扫描范围内文件”的 Manifest 记录；范围外目录生成排除摘要，不静默消失 | Manifest 数量与独立文件遍历器一致；不支持格式也有记录；源目录零写入 | 只看支持文件 → 所有范围内文件都有账 |
| B-04 `P0/M` | 增加 scan generation、SHA-256、大小、mtime 和快速复用逻辑 | 内容变化 hash 改变；仅触碰 mtime 不误报内容变化；重复扫描幂等 | 依赖路径/时间 → 能确定内容是否真正变化 |
| B-05 `P0/M` | 确定性识别格式、语言和科研角色；扩展名、magic/MIME、路径规则优先，LLM 只作可选补充 | 伪扩展名、无扩展名和常见科研文件分类 fixture 通过；输出分类理由 | 文件同等处理 → 可按科研价值选择策略 |
| B-06 `P0/S` | Manifest 拆分 `processing_status` 与 `read_depth`，并要求 `reason` | Schema 枚举、非法组合、序列化和旧记录兼容测试通过 | 单一模糊状态 → 同时知道是否成功和读了多深 |
| B-07 `P0/M` | 实现确定性阅读优先级与“引用提升队列”；当前实现从 v4 分类角色、路径/名称和有界引用生成独立建议，不修改 Manifest | README/配置引用关键图或文件后进入提升队列；selected/deferred 预算语义、策略限制和大型数据集保护通过测试 | 按格式一刀切 → 获得可审计、非目标感知的确定性阅读建议 |
| B-08 `P0/S` | 生成覆盖率与失败报告，按数量、体积、角色、状态、读取深度和原因汇总 | 各分类之和可与 Manifest 对账；失败列表可定位；输出稳定快照 | 无法证明读全 → 可量化审计覆盖情况 |

### C. 确定性内容提取

| ID | 最小改动 | 独立验证 | 改动前 → 改动后 |
|---|---|---|---|
| C-01 `P0/M` | 定义 `ExtractedDocument`、`Block`、`Locator` 与提取器结果 Schema | JSON round-trip、Schema 版本、非法 locator 和 future version 测试 | 每种格式输出不同 → 后续统一处理和回源 |
| C-02 `P0/M` | 文本、源码、LaTeX、配置和结构化文本提取器，保留行号、编码和截断原因 | 多语言源码、UTF-8/常见编码、超长行和二进制误判 fixture | 源码覆盖有限 → 主要科研代码可确定性读取 |
| C-03 `P0/M` | Notebook 提取器，保留 cell ID/index、类型、执行顺序和输出摘要 | 修改单个 cell 只影响对应块；输出大对象被安全截断 | Notebook 当普通 JSON → 可按实验步骤理解 |
| C-04 `P0/L` | PDF 页级文本提取，保留页码并检测扫描型/低文本页 | 文本 PDF 页码准确；扫描 PDF 明确标记需 OCR；失败不伪造文本 | 模糊全文 → 结论能定位具体页 |
| C-05 `P1/L` | 关键图片、架构图、结果图和扫描 PDF 的视觉/OCR 管线；只处理被规则或引用提升的对象 | 架构图/结果图 fixture 深读；训练图片集只抽样；视觉失败有状态 | 图片全部忽略或全部读取 → 只深读科研关键视觉材料 |
| C-06 `P1/L` | CSV/TSV/XLSX、DOCX、PPTX 提取，保留 sheet/cell、paragraph、slide 定位 | 表格单元格、PPT slide、DOCX 段落可回源 | Office 位置丢失 → 能定位具体表格或页面 |
| C-07 `P1/M` | `.mat/.npy/.npz/.h5/.parquet/.pt/.ckpt` 等科研二进制元数据提取器 | 读取 keys/shape/dtype 等安全元数据；超大权重不加载张量 | 二进制静默忽略 → 至少知道内容结构和处理理由 |
| C-08 `P0/M` | 按章节、符号、页、cell 或表格范围分块，每个 chunk 保留原 locator | 合并块可覆盖原文；无定位的任意切割被拒绝；边界快照稳定 | 长文件直接塞入提示词 → 可分层理解且不丢定位 |

### D. 来源、Evidence 与原文件回溯

| ID | 最小改动 | 独立验证 | 改动前 → 改动后 |
|---|---|---|---|
| D-01 `P0/M` | 首次发现文件时分配持久 `source_id`，由 Core 生成而非 LLM 生成 | 重复扫描 ID 不变；新增文件不影响旧 ID；并发写入无重复 | 来源靠路径字符串 → 来源身份稳定 |
| D-02 `P0/M` | 建立 source version、当前路径、历史路径别名和 content hash 关系 | 内容修改新增版本但 ID 不变；历史版本可查询 | 修改文件覆盖旧来源 → 来源演化可审计 |
| D-03 `P0/M` | 定义 `Evidence = source_id + content_hash + locator + excerpt_hash` | 任意 locator 可校验/序列化；内容变化使旧 Evidence 失效 | 只说“来自某文件” → 证据精确到片段 |
| D-04 `P0/S` | 提供 `source locate/open` Core 能力和 CLI，输出当前路径、版本与原文片段 | 代码行、PDF 页、Notebook cell 和表格单元格 fixture 可重开 | 只能看 Wiki 摘要 → 可直接回到原文件 |
| D-05 `P0/M` | 当前路径失效时按路径别名、hash、Git 记录顺序恢复来源 | 文件重命名/移动后保留 ID；同 hash 多候选时不擅自绑定 | 移动后链接失效 → 内容不变可恢复来源 |
| D-06 `P0/M` | 来源健康检查：`valid/stale/missing/ambiguous`，校验 hash 与 locator | 删除、修改、截断和多候选场景返回正确状态 | 证据失效难发现 → 查询前可判断证据健康 |

### E. 项目级理解与 15 类产物

| ID | 最小改动 | 独立验证 | 改动前 → 改动后 |
|---|---|---|---|
| E-01 `P0/M` | 建立 `register→inventory→classify→extract→...→render` 运行编排器、stage 状态和 `run_id`；先用空实现贯通 | 阶段失败可从断点重试；已完成阶段不重复；运行报告完整 | 单次脚本耦合 → 每阶段可验证又可一键调用 |
| E-02 `P0/M` | 确定性生成项目地图：目录、语言、入口、依赖、配置、运行脚本和关键候选 | 不调用 LLM 也能生成稳定项目轮廓；快照测试通过 | 先随机读文件 → 先建立全局结构 |
| E-03 `P0/L` | 实现 chunk→file→module→project 分层理解，每层保存 Evidence 输入 | 大文件和多模块 fixture 不超预算；上层可追踪到下层 Evidence | 单文件一次总结 → 大项目可分层覆盖 |
| E-04 `P1/L` | 生成代码执行流程、主要调用链和数据流，允许确定性分析加 LLM 综合 | fixture 入口、关键模块和输入输出关系正确；不确定路径被标记 | 单文件摘要 → 能说明系统如何运行 |
| E-05 `P1/L` | 关联论文、方法、创新点和数据集，并区分项目实现、论文主张和推断 | 论文引用、代码实现和数据配置能互相链接；推断不冒充事实 | 文献与代码孤立 → 能解释研究依据与实现关系 |
| E-06 `P1/L` | 识别实验、配置、运行、指标和结果，建立 config→run→result→claim | 两组冲突实验不互相覆盖；结果含实验条件和 Evidence | 实验文件散落 → 可按实验链路管理 |
| E-07 `P0/L` | 实现 15 类 Markdown 产物渲染器和统一 `index.md`；缺资料也生成明确占位与风险 | 固定 fixture 15 项全部存在；缺失项显示原因而非空白成功 | 只生成零散页面 → 一次得到完整科研资料包 |
| E-08 `P0/M` | 实现面向用户的一键 `project understand` 命令/API，串联全部阶段并可 `--open` 网站 | 从空状态一次命令得到运行报告、15 项和网页；中断可恢复 | 用户手工串命令 → 一次操作完成项目理解 |

### F. Markdown 知识资产与跨项目学习

| ID | 最小改动 | 独立验证 | 改动前 → 改动后 |
|---|---|---|---|
| F-01 `P0/M` | 定义 15 类页面 frontmatter、目录契约和生成/用户所有权字段 | 所有页面 Schema 校验；错误类型和 future version fail closed | 页面格式松散 → 长期知识可升级和审计 |
| F-02A `P0/S` | Add strict Schema v2 directional `evidence_refs`; verified key Claim details require supporting Evidence and verification time; retain v1 read-only parsing | Focused v2/v1/future-version, duplicate-ID, Claim-index, and pathless-Claim tests | Undirected Evidence IDs -> structurally auditable directional references |
| F-02B `P0/M` | Add a deterministic read-only Claim/project/Source/Evidence currentness validator over exact Source version/hash, Locator, excerpt, verification time, and report-only relocation inspection | Missing/stale Evidence, post-verification edits, A → B → A source versions, and relocation ambiguity cannot retain a current verified state; success and failure paths write nothing | Structural gate → current-version Evidence closure without semantic inference or persistent status mutation |
| F-02C `P0/M` | Harden Source/Evidence/currentness access around stable filesystem objects with a pinned root lease, persistent OS locks, explicit post-replace commit states, exact-CAS relocation rollback, and Windows read sharing that denies concurrent write/delete | Root/symlink swaps, concurrent registry replacement/recovery, rollback races, and Windows write/delete sharing fail closed; repeated concurrency and full regression tests pass | Path-based read/write windows → stable, accountable registry transactions without adding research semantics or a public interface |
| F-02D `P0/S` | Bind each F-02B result to the exact normalized Knowledge Schema v2 Claim frontmatter revision and provide a deterministic read-only downstream integrity gate that recomputes declared closure without reopening Source bytes | Claim edits and tampered project/path/status/role/Source/Evidence/aggregate fields reject replay; malformed constructed frontmatter fails closed; the integrity helper performs no Source access; focused and full regression tests pass | Auditable currentness object -> revision-bound lifecycle handoff without Markdown-body protection, semantic inference, persistence, or a public interface |
| F-03A `P1/M` | Add explicit project-local paper/method/dataset/experiment/metric/result/claim/decision/question/source entities plus caller-declared directed relations; validate current Schema v2 page bindings and deterministic backlinks/project-index projection | Stable identity, canonical JSONL, same-project/duplicate/self-loop constraints, relation direction, Evidence-ID referential integrity, page binding, reverse links, and project-index tests | Generic entity/concept pages → deterministic scientific workflow entities and reversible directed links without semantic inference or persistence |
| F-04A `P1/M` | Add a deterministic Core-internal validator for host-declared `draft/verified/stale/conflicting/rejected` Claim transitions and explicit coexistence of conflicting Claim/Result variants | All 25 host-declared status directions remain structurally composable; exact F-02D proof, F-03 identity/path binding, monotonic time, coordinated title rename, mandatory conflict proof, shared Results, deterministic IDs, and no-I/O boundaries are tested | Ad hoc status fields -> auditable revision/proof-bound lifecycle validation without inventing scientific transition semantics or persisting Markdown |
| F-04 `P1/M` | 支持 `draft/verified/stale/conflicting/rejected` 和冲突并存 | 冲突实验产生两个结果与冲突状态，不覆盖旧结论 | 新结论覆盖旧结论 → 历史与冲突清晰 |
| F-05A `P0/M` | Add a deterministic Core-internal in-memory planner over exact caller-supplied Schema v2 page bytes, immutable page identity, advancing timestamps, and explicit generated/user/mixed body ownership | Generated/user intent isolation, exact mixed-region preservation, malformed marker rejection, body-free typed errors, deterministic result integrity, no-I/O checks, and downstream regressions pass | Unbounded body replacement -> auditable ownership-safe candidate bytes without claiming live persistence or semantic authorization |
| F-05 `P0/M` | 受控 Markdown 写入器，保护用户确认区和网站编辑内容 | 重新生成不覆盖用户确认；冲突写入产生审计记录 | 生成器可覆盖人工知识 → 人机协作内容可长期保留 |
| F-06 `P1/L` | 建立 `wiki/library/` 显式提升流程、规范标识和跨项目来源关系 | 同名不同对象不自动合并；提升后仍能回到原项目 Evidence | 项目彼此孤立或错误合并 → 可安全跨项目学习 |

### G. Core 服务、可信查询与 MCP

| ID | 最小改动 | 独立验证 | 改动前 → 改动后 |
|---|---|---|---|
| G-01 `P0/M` | 建立与 CLI/宿主无关的 Research Core service facade，先暴露 register、scan、coverage、source-open | CLI 与直接 Python 调用得到相同结果；无宿主依赖 | 脚本直接操作文件 → 有稳定可嵌入内核 |
| G-02 `P0/M` | 可解释的关键词/字段候选检索，支持 project、role、type、status 过滤 | fixture 召回基线和排序快照；结果说明命中原因 | 只选少量 Wiki 页 → 可按项目和证据类型检索 |
| G-03 `P0/M` | 查询链路中的原文件重开服务，核验当前 hash 和 locator | stale/missing Evidence 不返回为已核验原文 | 检索摘要后直接回答 → 回答前能检查当前原文 |
| G-04 `P0/L` | Verified Query：检索→选择 Evidence→重开原文→回答 | 已知问题带定位回答；证据缺失时明确拒绝推断 | 基于旧摘要回答 → 基于当前证据回答 |
| G-05 `P0/M` | 引用、不确定性和冲突策略，区分 fast/verified/auto | stale、conflicting、insufficient 场景输出符合契约 | 容易过度确定 → 回答状态透明 |
| G-06 `P1/S` | Query Trace 记录候选、证据、模型、时间和输出状态 | 可用 trace 重现使用了哪些来源；敏感内容不泄漏 | 不知道答案怎么形成 → 查询过程可复查 |
| G-07 `P0/M` | 实现最小 MCP Server，映射 Core 的 project context、coverage、source-open、query、reconcile、plan 能力 | MCP 客户端契约测试；错误映射稳定；同一数据与 CLI 一致 | 需要手工搬运上下文 → 宿主可直接调用 Core |
| G-08 `P0/S` | 生成受预算约束的 Host Context Pack，包含状态、任务、风险、Evidence 引用而非整库灌入 | 大项目上下文不超预算；遗漏原因可见；敏感路径被过滤 | 宿主盲目全读 → 获得精炼且可回源上下文 |

### H. 增量 reconciliation 与知识过期

| ID | 最小改动 | 独立验证 | 改动前 → 改动后 |
|---|---|---|---|
| H-01 `P0/M` | 比较 Manifest generations，输出新增、修改、移动、删除和未变化 | 固定变更集分类准确；报告可对账 | refresh 不说明变化 → 每次更新有明确 diff |
| H-02 `P0/M` | 记录 source→extract→summary→claim→synthesis→plan 依赖边 | 给定 source 可计算完整影响范围；循环被检测 | 不知道谁依赖源文件 → 可计算受影响知识 |
| H-03 `P0/M` | 来源变化后自动传播 `stale`，但不自动删除用户结论 | 配置变更使相关 Claim/计划 stale；无关页面不受影响 | 旧知识继续冒充有效 → 过期立即可见 |
| H-04 `P0/M` | 建立 append-only 宿主事件账本和 `dirty paths` 队列，统一 Codex/Claude 事件模型 | 重复事件幂等；乱序事件可处理；事件不直接篡改知识 | Hook 事件散落 → 有宿主无关增量输入 |
| H-05 `P1/M` | 选择性 reconciliation：只提取变化文件并刷新受影响产物 | 修改一个配置只触发有限阶段；与全量结果一致 | 每次全量重扫 → 更新更快、更省费用 |
| H-06 `P1/M` | 删除、移动、多候选和外部大变更处理 | 移动恢复 ID；删除保留历史并标 missing；歧义要求确认 | 文件消失后关系断裂 → 历史和原因可追踪 |
| H-07 `P0/M` | 定义保守同步边界：Core/CLI/MCP 显式 reconciliation 对起始事件快照执行全量 `register→inventory→classify` correctness fallback，成功后只确认该快照 | Hooks 禁用或 hints 缺失/不一致时仍成功；失败和快照后事件保持 pending；严格 Schema v1 checkpoint 可核验 | 依赖 Hook 可靠性 → 有不依赖 hints 的最终一致性边界 |

### I. 目标、计划、执行和复盘

| ID | 最小改动 | 独立验证 | 改动前 → 改动后 |
|---|---|---|---|
| I-01 `P0/M` | 目标与里程碑 Schema：目标、成功标准、截止时间、阶段和依赖 | 表单/Markdown round-trip；缺字段时允许 draft | 只有知识页面 → 系统知道研究方向 |
| I-02 `P0/M` | 内部任务协议：`why_now/inputs/evidence/allowed_paths/dependencies/DoD/verification/artifacts` | 非法依赖和无完成标准任务被拒绝；可导出 Markdown | 普通 Todo → 可执行、可验收任务 |
| I-03 `P0/M` | 项目状态快照，汇总实验、结果、开放问题、阻塞、stale 和最近变更 | fixture 状态与底层实体一致；快照可重建 | 规划器不了解进度 → 计划基于真实状态 |
| I-04 `P0/M` | 首次扫描生成初始目标、backlog 和今日计划草案；缺用户信息标 `DRAFT` | 15 类产物中的第 14/15 项始终存在且状态正确 | 扫描只总结过去 → 同时给出下一步草案 |
| I-05 `P1/L` | 成熟每日规划器：优先级、期限、依赖、可用时间、风险和科研价值 | 不安排被阻塞任务；计划总时长不超过预算；说明 `why_now` | 人工挑任务 → 自动生成有依据时间盒计划 |
| I-06 `P0/L` | 宿主执行闭环与完成核验；任务自动传给当前 Agent，依据 diff/测试/日志/产物/用户确认更新 | 模型自报成功但测试失败时任务不完成；成功证据可审计 | 手工复制计划和复扫 → 当前 Agent 内自动执行和核验 |
| I-07 `P1/M` | 每日复盘更新任务、里程碑、阻塞和后续计划 | 未完成原因进入次日决策；产物与 Evidence 建立关系 | 每天计划独立 → 计划—执行—复盘连续 |
| I-08 `P2/L` | 可选独立 Provider/headless runner，用于定时或批量后台任务 | 与集成模式写入同一状态；权限、预算和停止条件可控 | 只能前台交互 → 可选无人值守处理 |

### J. 网站、宿主适配、安全与发布质量

| ID | 最小改动 | 独立验证 | 改动前 → 改动后 |
|---|---|---|---|
| J-01 `P0/L` | 扩展固定科研 fixture 和端到端测试，覆盖 understand→locate→query→reconcile→plan→render | 从干净临时目录可重复通过；源项目 hash 全程不变 | 只有脚本级测试 → 整条工作流可回归 |
| J-02 `P0/M` | 隐私、只读和 prompt injection 边界；敏感路径、外部发送清单和预算预览 | `.env`/密钥不进入模型上下文；源文件指令不能改变系统策略 | 可能泄密或被文档注入 → 数据与指令边界明确 |
| J-03 `P0/L` | 本地只读 Web 驾驶舱：项目、15 类资料、覆盖率、Evidence、运行历史和任务视图 | 仅监听 loopback；链接可回到对应 Markdown/Evidence | 只能翻 Markdown → 可直观管理科研项目 |
| J-04 `P0/L` | 网站受控编辑目标、任务、状态和用户确认结论 | 写入 Markdown 并保留 frontmatter；重生成不覆盖；并发冲突可见 | 网页只能看或覆盖文件 → 可安全管理长期知识 |
| J-05 `P0/M` | Codex 参考适配包：Plugin manifest、Skills、MCP 配置、Hooks 和最小指导文件 | 安装后可识别项目、调用 Core、执行后 reconcile；Hooks 未信任时有降级 | Codex 与 Core 分离 → 在当前 Agent 内直接闭环 |
| J-06 `P1/M` | Claude Code 等价适配包，共享同一 Core 和知识库 | 相同验收脚本在 Claude Code 适配层通过；不复制状态 | 只能在一个宿主使用 → 主流 Agent 可共享科研资产 |
| J-07 `P1/M` | 跨宿主连续性测试：Codex 建任务/执行一部分，Claude 继续，反向亦然 | project/task/run/source ID 全部一致；无重复知识 | 换 Agent 丢上下文 → 可无缝继续同一项目 |
| J-08 `P1/M` | 可观测性、缓存和费用：阶段耗时、成功率、失败原因、模型、Token 和缓存命中 | 运行报告字段完整；缓存失效准确；敏感内容不写日志 | 成本和故障不可见 → 可定位质量与费用 |
| J-09 `P1/M` | 统一 CLI、配置、文档和显式迁移工具；旧命令保留兼容或给出明确迁移 | CLI help/文档快照；legacy 数据只显式迁移；可回滚 | 命令分散 → 有稳定用户工作流 |
| J-10 `P0/L` | 第一版发布验收：产品约定中的 15 个最低场景全部自动或半自动通过 | 生成签名验收报告，列出版本、环境、证据和未通过项 | “感觉完成” → 有明确发布门槛 |

## 4. 推荐里程碑与实际执行顺序

任务编号按能力域组织，实际开发按“最短可信垂直链路”穿插执行，而不是机械地先做完 B 再做完 J。

### R0：安全工程基线——已完成

```text
A-01 → A-02 → A-03
```

效果：仓库正确、基线可回归、机器状态与 Markdown 知识分离。

### R0.5：产品契约——P-05 / P-08 已完成

```text
P-05 / P-08 产品约定 + B～J 路线重排
```

效果：固定产品形态、一键 15 类产物、Agent 原生闭环、Agent-native 工具职责和跨宿主边界，避免继续按“第二个聊天机器人”或“Core 重造低配 LLM 大脑”的方向开发。

### R1：可信盘点与原文件定位

建议顺序：

```text
B-01 → B-02 → B-03 → B-04 → B-05 → B-06 → B-08
→ C-01 → C-02 → C-03 → C-04 → C-08
→ D-01 → D-02 → D-03 → D-04 → D-05 → D-06
→ J-01（基础链路）
```

验收门槛：

- 扫描范围内无静默遗漏；
- 每个文件有处理状态、读取深度和理由；
- 代码、Notebook、PDF 可提取并保留定位；
- 文件移动后可找回；
- 可以用 `source_id` 重开当前原文。

R1 was accepted on 2026-07-16 by the automated chain documented in
[`r1-basic-chain-acceptance.md`](r1-basic-chain-acceptance.md). That deterministic
slice remains covered. On 2026-07-19, J-01 also completed the authorized
R3-minus-C-07 expansion covering `understand -> locate -> query(unavailable) ->
reconcile -> plan -> render`; C-07 and R4 behavior remain outside that acceptance.

### R2：Core 与 Codex 的最小原生垂直切片

建议顺序：

```text
G-01 → G-07 → G-08
→ E-01（阶段骨架） → E-08（先贯通确定性阶段）
→ H-04 → H-07
→ J-05
```

验收门槛：

- Codex 可直接调用 Core，不需用户复制计划或扫描结果；
- 会话内能够识别项目、查看 coverage、打开 Evidence；
- 执行后可触发 reconciliation；
- Hook 被禁用时仍可通过 Skill/MCP 显式完成同步。

这是架构防偏门槛：如果这一阶段仍需要两个聊天窗口手工交接，应暂停后续开发修正设计。

As of 2026-07-16, R2 is complete. J-05 packages the validated Core boundary as a Codex Plugin with a Skill, MCP configuration, portable launchers, and one optional fail-open Hook. Codex can identify registered projects, call the current host-safe Core operations, and explicitly reconcile after work. Hook signals remain optional untrusted H-04 hints, so disabled, missing, malformed, unavailable, or untrusted Hooks do not weaken the H-07 full-scan correctness path. R2 still does not claim adaptive extraction, H-05 selective refresh, curated-knowledge refresh, Verified Query, planning, or Web behavior.

### R3：一键完整项目理解与本地网站

建议顺序：

```text
B-07（已完成）
→ C-05 (complete) → C-06 (complete)
→ C-07（用户决定暂缓，保持 not_started）
→ F-01（complete：F-01A Schema/path + F-01B canonical layout）→ F-02（complete：F-02A structural + F-02B read-only currentness + F-02C stable-object hardening）→ F-03（complete：F-03A explicit entities/relations/backlinks）→ F-04 → F-05
→ E-02 → E-03 → E-04 → E-05 → E-06 → E-07 → E-08（完整）
→ I-01 → I-02 → I-03 → I-04
→ J-03 → J-04 (complete)
→ J-01（15 类产物 E2E） (R3-minus-C-07 complete)
```

验收门槛：

- 用户一次操作得到全部 15 类 Markdown；
- 本地网站可阅读、回源和查看覆盖率；
- 可编辑目标、任务、状态和用户确认结论；
- 缺失资料明确显示，不能用空白页伪装成功；
- 未提供目标时第 14/15 项标为 `DRAFT`。

As of 2026-07-19, the deliberately bounded **R3-minus-C-07** preview is accepted
at `checkpoint/j-01-r3-end-to-end`. This does not complete or implement C-07;
`llmwiki_query` remains the exact unavailable contract until G-04, and R4 has
not started.

### R4：可信查询与增量知识维护

建议顺序：

```text
G-02 → G-03 → G-04 → G-05 → G-06
→ H-01 → H-02 → H-03 → H-05 → H-06
→ I-06
```

验收门槛：

- Verified Query 重开当前原文件；
- 证据不足、冲突、过期时不输出伪确定结论；
- 修改一个源文件只刷新受影响内容；
- 宿主执行任务后自动核验并更新状态，无需用户手工全扫。

### R5：跨项目学习、成熟规划与双宿主

建议顺序：

```text
F-06
→ I-05 → I-07
→ J-06 → J-07
```

验收门槛：

- 跨项目知识显式提升且不错误合并；
- 每日计划考虑依赖、期限、可用时间、风险和项目状态；
- Codex 与 Claude Code 共享同一项目、任务、证据和历史；
- 切换宿主不需要重新扫描或复制知识。

### R6：安全、可观测性与第一版发布

建议顺序：

```text
J-02 → J-08 → J-09 → J-10
→ I-08（可选增强）
```

验收门槛：

- 隐私、外部发送和 prompt injection 测试通过；
- 成本、缓存、失败原因和运行版本可审计；
- 旧数据显式迁移、可回滚；
- 产品约定全部最低验收场景形成正式报告。

## 5. 每个任务的 Git 执行约定

后续默认采用：

```text
research-assistant
└─ task/<task-id>-<short-name>
```

例如 B-01：

```powershell
git switch research-assistant
git switch -c task/b-01-project-register
# 实现、测试、审查
git commit -m "feat(b-01): register research projects"
git switch research-assistant
git merge --ff-only task/b-01-project-register
git tag checkpoint/b-01-project-register
```

原则：

1. 一个任务尽量只解决一个可验证问题；
2. 一个任务尽量一个原子 commit；
3. 任务分支验证完成后 fast-forward 合并；
4. 当前无法连接 GitHub 时只做本地 commit/tag，不 push；
5. 回滚优先使用 `git revert <commit>`，不破坏历史；
6. 若任务必须拆成多个 commit，提交信息都带同一任务 ID，并在完成报告列出顺序；
7. 不修改用户个人知识库中的历史规划文件；仓库内本文作为后续实现的执行基线。

## 6. Authorized stop boundary

The final authorized R3-minus-C-07 unit, **J-01**, is complete at implementation
commit `9e2eba2` and `checkpoint/j-01-r3-end-to-end`. No further unit is
authorized in this run. **C-07 remains `deferred/not_started`, Query remains the
exact `capability-unavailable` contract until G-04, and R4 has not started.**

R1 and the G-01 Core facade remain accepted at their checkpoints. G-07 is
complete at `checkpoint/g-07-mcp-server`; its stdio transport, seven-tool catalog,
honest unavailable capability contracts, stable error mapping, privacy boundary,
and real MCP client validation are documented in
[`research-mcp-server.md`](research-mcp-server.md). G-08 is complete at
`checkpoint/g-08-host-context-pack`; the deterministic, exact-byte Host Context
Pack contract and its path-free omission/filtering behavior are documented in
[`host-context-pack.md`](host-context-pack.md).

E-01 is complete at `checkpoint/e-01-run-orchestrator`. Its persisted,
resumable run state machine, closed Schema v1 attempt ledger, interruption
recovery, source-read-only boundary, Core/CLI entry points, and honest
`unavailable` stage semantics are documented in
[`project-run-orchestration.md`](project-run-orchestration.md).

E-08 is complete at implementation commit `2da9540` and checkpoint
`checkpoint/e-08-complete-understand`. The path-based Core/CLI action now runs the
full deterministic `register -> inventory -> classify -> extract -> adaptive-read
-> synthesize -> evidence -> status -> plan -> index -> web-render` pipeline,
produces all fifteen curated Markdown deliverables plus a self-contained local
read-only run page, and supports bounded prefix execution and same-run resume.
The legacy E-01 public run API keeps its original three-stage/unavailable
boundary; only the explicit E-08 action opts into the complete runner set. See
[`project-understand.md`](project-understand.md).

H-04 remains complete at commit `5c2a888` and checkpoint
`checkpoint/h-04-host-event-ledger`. The closed host-neutral Schema v1 ledger,
contiguous ingestion ordering, canonical duplicate idempotency, collision
handling, deterministic dirty-path projection, crash-repair ordering,
source-read-only boundary, and Core/CLI parity are documented in
[`host-event-ledger.md`](host-event-ledger.md). H-04 records untrusted signals
only; it does not claim reconciliation, Hook reliability, or selective refresh.

H-07 is complete after validation on 2026-07-16 and is designated by
`checkpoint/h-07-project-reconciliation`. The implemented Core, CLI, and
`llmwiki_reconcile` operation snapshot the starting H-04 event boundary and
always run a fresh full `register -> inventory -> classify` scan as the
correctness fallback. Hook events and explicit dirty paths are untrusted hints;
Hooks may be disabled and hints may be absent or inconsistent. The starting
snapshot is acknowledged only after a completed run with zero coverage failures
and exact Manifest/coverage artifact hashes and bytes still current at commit.
Failures and events arriving after that snapshot stay pending. A stable
project-scoped `indexes/machine-state.lock` serializes Manifest, B-07 reading-
priority, coverage, run, and reconciliation writers; event ingestion uses the
separate stable
`events.jsonl.lock`, with lock order `machine-state.lock -> events.jsonl.lock`.
The strict Schema v1 checkpoint is stored at
`.llmwiki/projects/<project_id>/indexes/reconciliation-state.json`, while the
source project and curated knowledge remain unchanged. See
[`project-reconciliation.md`](project-reconciliation.md).

This completion activated the previously reserved MCP reconciliation contract.
At the H-07 checkpoint, query and plan were both explicit unavailable contracts;
I-04 now activates plan as a strict non-executable DRAFT operation, while query
remains `capability-unavailable` until G-04. H-07 itself stops at `classify` and
does not claim H-05 selective extraction, selective knowledge refresh, or any
later knowledge/rendering stage.

J-05 is complete after validation on 2026-07-16 and is designated by
`checkpoint/j-05-codex-plugin`. The package at `plugins/llmwiki-research/`
contains the validated manifest, Skill, MCP configuration, portable Core/CLI
launchers, and optional fail-open Hook. At the J-05 checkpoint both query and plan
were unavailable; the same adapter now exposes I-04 DRAFT planning while query
remains unavailable. Clean-profile installation, seven-tool MCP startup, current
Core calls, registered-root Hook normalization, and no-Hook reconciliation
fallback are covered by `tests/test_codex_plugin.py`. See
[`codex-reference-adapter.md`](codex-reference-adapter.md).

B-07 implementation landed on **2026-07-17** in `248b231`. The completed
implementation and validation state is designated by
`checkpoint/b-07-adaptive-reading-priority`. The implementation exposes
`ResearchCoreService.prioritize(project_id)` and
`python tools/project.py prioritize <project_id> --json`, consumes the exact
current `project-inventory-v4` Manifest, and writes the independent Schema v1
`indexes/reading-priority.json` artifact without modifying Manifest file state or
creating Manifest v5. It is deterministic from classification role, path/name,
and bounded incoming references; it is not goal-aware and does not perform
extraction, semantic/LLM reading, project-understand stage advancement, H-07
reconciliation, or H-05 refresh.

Focused validation recorded on 2026-07-17:

```powershell
python -B -m pytest -q -p no:cacheprovider `
  tests/test_reading_priority.py `
  tests/test_project_layout.py
```

Result: **45 passed, 3 skipped**. The added regressions cover current-grounded execution authorization, intrinsic/current-policy tamper rejection, machine-state ancestor redirection, post-read/pre-commit reference mutation, and every exact fixed-v1 boundary.

C-05 implementation landed on **2026-07-17** in `bb045cf`. The completed
implementation and validation state is designated by
`checkpoint/c-05-visual-ocr`. The implementation adds strict in-memory visual selection and
execution over only current B-07 records whose `deep_read_status` is
`selected`; deferred and limited records remain non-executable audit data.

Standalone PNG/JPEG/GIF/TIFF/BMP frames use EXIF-normalized whole-frame
`ImageRegionLocator` values and exact RGBA source hashes. PDF handling preserves
C-04 native text and metadata and renders only C-04 OCR/visual-review follow-up
pages. The default local path uses bounded Pillow decoding, optional local
Tesseract, and direct pypdf images; no network backend is configured implicitly.
External backends and renderers require both the current B-02 raw-send decision
and a point-of-send selection/registration/source reauthorization. PDF pixel
limits are document-wide, custom renderer payloads fail closed on contract
violations, and safely decoded earlier raster frames survive bounded later-frame
failures as explicit partial results. C-05 creates no extraction artifact,
Evidence, curated Markdown, Manifest mutation, or source-project write. See
[`visual-ocr.md`](visual-ocr.md).

Focused validation recorded on 2026-07-17:

```powershell
python -B -m pytest -q -p no:cacheprovider `
  tests/test_visual_selection.py `
  tests/test_visual_extractor.py `
  tests/test_extraction_schema.py `
  tests/test_chunking.py `
  tests/test_source_access.py
```

Result: **83 passed, 1 skipped**. Full validation produced **408 passed,
9 skipped**, `pip check` reported no broken requirements, UTF-8 health reported
zero structural issues, focused Ruff passed, and `git diff --check` passed.

C-06 implementation completed on **2026-07-17** in `7ebe966`; the completed
implementation and validation state is designated by
`checkpoint/c-06-office-tabular-extraction`. It adds bounded, deterministic,
source-read-only CSV/TSV/XLSX, DOCX, and PPTX extraction with exact
`TableRangeLocator`, `ParagraphLocator`, and `SlideLocator` coordinates. CSV and
TSV retain explicit-empty versus absent cells in canonical JSON matrices; XLSX
retains formulas and tagged date/time values; DOCX follows main-body paragraph
order; and PPTX follows internal presentation relationship order rather than
slide filenames.

OOXML archives fail closed on unsafe, duplicate, encrypted, non-regular,
over-limit, malformed, or DTD/entity-bearing members. Exact reopening reparses
the current immutable bytes and refuses truncated answers. C-06 creates no
extraction artifact, Evidence, curated Markdown, Manifest mutation, network
request, or source-project write. See
[`office-tabular-extraction.md`](office-tabular-extraction.md).

Focused validation recorded on 2026-07-17:

```powershell
python -B -m pytest -q -p no:cacheprovider `
  tests/test_office_tabular_extractor.py `
  tests/test_extraction_schema.py `
  tests/test_chunking.py `
  tests/test_source_access.py
```

Result: **82 passed, 1 skipped**. Full validation produced **447 passed,
9 skipped**, `pip check` reported no broken requirements, UTF-8 health reported
zero structural issues, focused Ruff passed, and `git diff --check` passed.

F-01A implementation completed on **2026-07-17** in `f987dc0`; the
bounded implementation and validation state is designated by
`checkpoint/f-01a-knowledge-artifact-contract`. It adds the strict in-memory
Schema v1 frontmatter model, canonical project-relative path mapping for the 15
product artifact classes and approved auxiliary pages, safe duplicate-key-
rejecting YAML parsing, strict UTF-8 decoding, deterministic serialization, and
direct PyYAML dependency declarations. See
[`knowledge-artifact-contract.md`](knowledge-artifact-contract.md).

F-01A creates no real curated page, reads no research source or binary payload,
and adds no registration, layout initialization, ResearchCoreService, CLI, MCP,
Skill, Hook, Plugin, or Web behavior. The host Agent remains responsible for
research semantics and page bodies; Core validates only structure, identity,
time, ownership, and path/type consistency. F-02 Evidence semantics and F-05
mixed/user write protection remain open.

Focused validation recorded on 2026-07-17:

```powershell
python -B -m pytest -q -p no:cacheprovider `
  tests/test_knowledge_artifacts.py `
  tests/test_project_layout.py `
  tests/test_project_registration.py `
  tests/test_health_baseline.py
```

Result: **51 passed, 2 skipped**. Full validation produced **483 passed, 9 skipped**, `pip check` reported no broken requirements, UTF-8 health reported zero structural issues, focused Ruff and compile checks passed, 23 local Markdown links passed, and `git diff --check` passed.

This checkpoint keeps F-01 **partial**. C-07 remains `not_started` and is
intentionally deferred by the user's product decision; no binary reader or
binary dependency was added. No later Roadmap unit is authorized by this record.

F-01B implementation completed on **2026-07-17** in `829cb54`; the
completed F-01 implementation and validation state is designated by
`checkpoint/f-01b-canonical-knowledge-layout`. It connects the F-01A Schema/path
contract to `ProjectLayout` and B-01 registration. The deterministic empty
knowledge skeleton now contains `papers/`, `methods/`, `datasets/`,
`experiments/`, `results/`, `claims/`, `plans/`, `plans/daily/`, `decisions/`,
and `sources/` under either the default or selected external knowledge root.

Unregistered storage validation now understands the nested canonical tree. It
accepts an empty legacy subset and fills missing directories, while unknown
files, unknown directories, knowledge-directory symbolic links, and existing
content fail closed. Initialization remains idempotent, creates no Markdown page,
does not rewrite a registration record, and does not read or modify the research
source project. See
[`knowledge-artifact-contract.md`](knowledge-artifact-contract.md) and
[`project-storage-layout.md`](project-storage-layout.md).

Focused validation recorded on 2026-07-17:

```powershell
python -B -m pytest -q -p no:cacheprovider `
  tests/test_project_layout.py `
  tests/test_project_registration.py `
  tests/test_knowledge_artifacts.py `
  tests/test_health_baseline.py
```

Result: **53 passed, 3 skipped**. Full validation produced **485 passed,
10 skipped**; the development-dashboard suite produced **18 passed**. `pip
check` reported no broken requirements, UTF-8 health reported zero structural
issues, focused Ruff and compile checks passed, 11 relevant local Markdown links
passed, and `git diff --check` passed.

F-01 is now **complete**. F-02 is also **complete** after three bounded units.
F-02A added structural Knowledge Schema v2 directional references, duplicate-free
Evidence IDs, the verified key-Claim gate, strict v1 read-only parsing, and
future-version fail-closed behavior. F-02B added the Core-internal, deterministic,
read-only Claim/project/Source/Evidence currentness closure in `564cf0a`, designated
by `checkpoint/f-02b-claim-evidence-currentness`; it validates exact Source
version/hash, Locator/excerpt fidelity, verification time, all declared Evidence,
and report-only relocation without writing Markdown, status, or registries.

F-02C then hardened the underlying stable-object transactions in `41e347e` and
`6c9016a`, designated by `checkpoint/f-02c-stable-file-access` and
`checkpoint/f-02c-stable-file-access-review-fix`. Root-bound persistent OS locks,
same-lease load/commit/rollback, explicit post-replace states, exact preimage CAS
rollback, and Windows no-write/no-delete stable reads close the scoped filesystem
race findings without adding research semantics or a public interface.

A fresh integrated regression later exposed that ordinary Windows registry readers
could still hold a read-only descriptor outside the adjacent lock protocol and deny
a concurrent atomic replacement. Repair `d84f128`, designated by
`checkpoint/f-02c-registry-read-write-coordination`, makes all ordinary Source and
Evidence registry reads join their persistent adjacent locks, establishes the
canonical nested order **Source registry lock → Evidence registry lock**, retains
one Source snapshot through Evidence binding, and makes Claim currentness consume
that bound snapshot. Readers do not change registry content.

Final repair validation produced **101 passed, 17 skipped** across the focused
stable-file, Claim currentness, Source, Evidence, recovery, access, and health
suites. Source and Evidence subprocess readers both waited for their adjacent lock;
the previously reproducible five-process recovery case passed **30 consecutive**
isolated runs; full regression produced **574 passed, 24 skipped**. Changed-file
Ruff, manual High/Medium review, and `git diff --check` passed.

A downstream lifecycle integration review then exposed that an otherwise
self-consistent F-02B result object could be replayed after the Claim frontmatter
revision changed. F-02D repair `4521be9`, designated by
`checkpoint/f-02d-claim-evidence-result-integrity`, upgrades the structured result
to `claim-evidence-validation-v2`, fingerprints the exact normalized current
Schema v2 Claim frontmatter, and adds a read-only integrity gate that revalidates
the path-bound Claim and recomputes project/status/role, ordered Source and
directional Evidence declarations, per-reference closure, aggregate reasons,
supporting count, and verified-state outcome. The integrity check does not reopen
Source bytes; fresh currentness still requires a new F-02B validation.

F-02D validation produced **134 passed, 17 skipped** across the focused Claim
Evidence, Evidence Schema, Source registry/version/access/health/recovery,
stable-file, and Knowledge Schema suites; full regression produced **600 passed,
24 skipped**. Changed-file Ruff, manual High/Medium review, and `git diff --check`
passed. The fingerprint covers normalized frontmatter only and does not claim
F-05 Markdown-body protection.

F-05 controlled mixed/user Markdown writing and C-07 remain `not_started`; these
F-02 units read no research-binary content, perform no external send, and do not
infer stance, conflict semantics, or research conclusions.

F-03 is now **complete** through the bounded F-03A Core primitive implemented in
`dcfa1d7` and hardened in `04856ce`, with final integration designated by
`checkpoint/f-03a-research-relations-final`. The host Agent explicitly supplies
project-local identity keys, titles, canonical locations, directed relation labels,
and optional Evidence IDs. Core validates stable IDs, strict canonical JSONL,
same-project endpoints, duplicate/self-loop constraints, current Knowledge Schema
v2 page bindings, optional Evidence-ID existence, and deterministic incoming/
outgoing backlinks plus project-index projection. Same titles never auto-merge.

F-03A performs no filesystem I/O or persistence, reads no Source or research-binary
content, writes no Markdown or body anchors, makes no external send, adds no
CLI/MCP/Web surface, and does not infer entities, relation semantics, Evidence
stance/currentness, conflicts, or research conclusions. F-04 lifecycle/conflict
state and F-05 controlled mixed/user Markdown writing remain separate.

Final F-03A validation produced **53 passed** across the focused research-relation
and Knowledge Schema/path suites. The unrelated Windows concurrent source-recovery
case passed ten consecutive isolated runs after one recorded transient subprocess
capture failure; the final full regression produced **572 passed, 24 skipped**.
Changed-file Ruff and `git diff --check` passed, and independent review found no
remaining High or Medium issue.

F-04 is now **partial** through the bounded F-04A Core validator implemented in
`738e086` and designated by `checkpoint/f-04a-claim-lifecycle`. The host Agent
continues to decide why a Claim changes status, whether Claims conflict, and which
Claim/Result entities belong to a coexistence set. Core validates all 25 declared
directions in the closed five-status set without a scientific transition matrix,
keeps the F-03 project/type/identity/path binding stable, permits a coordinated
display-title rename to the current registry title, requires strictly advancing
`updated_at`, and preserves prior `last_verified_at` history on non-verified
transitions.

A target `verified` Claim must set `last_verified_at == updated_at` and carry an
exact F-02D/F-02B integrity proof bound to the complete proposed Schema v2 Claim
frontmatter revision. A target `conflicting` Claim must carry an exact deterministic
coexistence proof that includes the target Claim path and fingerprint. Coexistence
requires at least two distinct current Claim entities already marked `conflicting`,
one or more duplicate-free Result references per variant, and at least two distinct
Results overall; Results may be shared across competing interpretations and need not
themselves be marked `conflicting`. Transition identity closes over before/after
Claim fingerprints plus supplied Evidence and conflict proof hashes.

F-04A validation produced **98 passed** across the focused lifecycle, Knowledge
Schema, Claim Evidence, and research-relation suites; the integrated full regression
produced **600 passed, 24 skipped**. Changed-file Ruff, `py_compile`, manual
High/Medium review, and `git diff --check` passed. F-04A performs no persistence,
Markdown or registry write, Source or research-binary semantic read, stale
propagation, conflict/winner inference, CLI/MCP/Web publication, or external send.
F-04 therefore remains `partial`; controlled persistence and mixed/user Markdown
protection belong to later bounded units, especially F-05.

F-05 is now **partial** through the bounded F-05A in-memory planner implemented in
`642ab9e` and designated by `checkpoint/f-05a-controlled-markdown-plan`. The host
Agent supplies the complete proposed Knowledge Schema v2 page, update intent, and every
semantic or user-authorization decision. Core binds the proposal to exact
caller-supplied base bytes, canonical path/type/project identity, immutable ownership
and creation time, and a strictly advancing `updated_at`.

`generated` and `user` pages use whole-body ownership. `mixed` pages use bounded,
duplicate-free, LF/CRLF-delimited `llmwiki:user-region` markers: regeneration takes
the generated skeleton from the proposal while restoring exact user bytes from the
caller-supplied base snapshot, and `user-edit` cannot change generated text, marker
structure, or ordered region IDs. Successful plans return canonical candidate bytes
plus content-free hashes/change metadata; typed rejection views do not copy Markdown or
YAML bodies. A plan ID is deterministic correlation metadata, not authenticated actor
identity, semantic authorization, filesystem currentness, or permission to persist.

F-05A validation produced **127 passed** across the focused controlled-Markdown,
Knowledge Schema, Claim Evidence, Claim lifecycle, and research-relation suites; the
integrated full regression produced **629 passed, 24 skipped**. Changed-file Ruff,
`py_compile`, manual High/Medium review, and `git diff --check` passed. F-05A performs
no filesystem I/O, lock, audit persistence, Source or research-binary read, external
send, stale propagation, or CLI/MCP/Web publication.

F-05B completes the bounded F-05 Core contract on **2026-07-18**. Under the stable
per-project `indexes/machine-state.lock`, it reloads the exact registration, opens the
already-existing knowledge parent, reads the live page, independently recomputes F-05A
from live bytes plus the raw proposal, and requires a structured host/actor/session/
decision authorization bound to every exact revision and plan field. Authorization IDs
and trusted decision tuples are single-use. Stable-file publication uses a pinned
knowledge directory, non-clobbering creation, an immediate exact-hash recheck before
atomic replace, and post-publication output-hash verification; conflicts never rebase.

The strict bounded body-free audit ledger records adjacent `prepared -> committed`,
`conflict`, safe pre-publication `failed`, or `commit-unknown` transactions. The
prepared phase must reserve both a terminal record slot and maximum terminal-record
capacity before page publication; the persistence boundary also revalidates the full
frozen authorization and nested trusted host/session context before any project,
ledger, or page access. Once publication may be visible, terminal-audit read,
validation, capacity, CAS, re-read, and durability failures are all reported as
`commit-audit-unknown` without rollback. Malformed, legacy/future, noncanonical,
duplicate-key, replayed, or dangling-prepared history fails closed. F-05B creates no
Markdown directory, decides no semantics, reads no Source or research binary, sends
nothing externally, and exposes no CLI/MCP/Hook/Web or `ResearchCoreService` operation.
Product rendering and Web editing remain later R3 units.

E-02 is complete after validation on **2026-07-18** at implementation
`c3ab7e0`, designated by `checkpoint/e-02-deterministic-project-map`. The
strict Schema v1 `project-map-v1` artifact is derived exclusively from the exact
current B-06 Manifest under `machine-state.lock`; it records the Manifest
generation/count/bytes/SHA-256 binding, bounded directory aggregates and
classification distributions, and ranked entrypoint, dependency, configuration,
run-script, and key-file candidates with explicit omission counts.

Structural loading rejects malformed, legacy/future, unknown, duplicate-key, and
noncanonical documents. Current loading independently rebuilds the full expected
payload, so stale or validly shaped tampered maps fail closed. Atomic generation
rechecks Manifest bytes immediately before publication, replaces only stale maps,
and never silently repairs a map that claims the current Manifest identity. The
Core facade is `ResearchCoreService.project_map(project_id)`; E-02 adds no CLI,
MCP, Hook, or Web operation. Validation produced **36 passed, 2 skipped** across
the focused project-map/layout/Core suites and **679 passed, 27 skipped** in the
full regression. Changed-file Ruff, `py_compile`, and `git diff --check` passed.
No registered source bytes, curated Markdown, LLM, external send, semantic
inference, or research-binary extraction were used. C-07 remains
`deferred/not_started`. See [`project-map.md`](project-map.md).

E-03 is complete after validation on **2026-07-18** at implementation
`4fcd413`, designated by `checkpoint/e-03-hierarchical-understanding`. The
strict Schema v1 `llmwiki-hierarchical-understanding` artifact uses
`understanding_version=hierarchical-understanding-v1` and stores bounded,
deterministic `chunk -> file -> module -> project` closure. Explicit host
observations carry canonical source-chunk/module identities, summaries, input
budgets, and duplicate-free Evidence IDs; when semantic observations are absent,
E-03 emits an explicit Manifest metadata-only fallback rather than pretending to
have read source content.

Every level preserves lower-level IDs, Evidence closure, input-byte/token closure,
and a bounded deterministic summary. Stable identities bind chunk observations,
current file hashes, module identity, project ID, and exact current Manifest hash.
Strict loading rejects malformed, legacy/future, unknown, duplicate-key,
noncanonical, over-budget, closure-inconsistent, summary-tampered, and stable-ID-
tampered artifacts. Current loading runs under `machine-state.lock`, validates the
exact current Manifest generation/hash/count/bytes and represented-file
classification projection, and deterministically rebuilds metadata-only truth.
Atomic publication rechecks exact Manifest bytes before replace.

Validation produced **36 passed, 2 skipped** across the focused E-03/layout/Core
suites and **692 passed, 28 skipped** in the full regression. Changed-file Ruff,
`py_compile`, and `git diff --check` passed. E-03 opens no registered source file,
calls no LLM, writes no curated Markdown, and performs no external send or
research-binary extraction. C-07 remains `deferred/not_started`. See
[`hierarchical-understanding.md`](hierarchical-understanding.md).

E-04 is complete after validation on **2026-07-18** at implementation
`c94e122`, designated by `checkpoint/e-04-execution-flow`. The strict Schema v1
`llmwiki-execution-flow` artifact (`execution-flow-v1`) records bounded directed
nodes and call/data-flow edges. Hosts may supply grounded node/edge observations
with Evidence IDs and `observed`/`inferred`/`uncertain` certainty; uncertainty
reasons remain explicit and are never promoted to facts. Without observations,
E-04 emits a deterministic Manifest-metadata fallback with inferred entrypoint
candidates and unresolved paths marked uncertain.

The artifact binds to the exact current Manifest generation, ordinary-file
count/bytes, and SHA-256. Strict canonical JSON, closed fields, stable IDs,
current file-path projection, shared `machine-state.lock`, atomic publication,
and pre-replace Manifest revalidation are enforced. Validation produced **30
passed, 1 skipped** in focused suites and **699 passed, 28 skipped** in full
regression; changed-file Ruff, `py_compile`, and `git diff --check` passed. E-04
opens no source bytes, calls no LLM, writes no curated Markdown, performs no
binary semantic extraction, and sends nothing externally. C-07 remains
`deferred/not_started`. See [`execution-flow.md`](execution-flow.md).

E-05 is complete after corrective review on **2026-07-18**. The original v1
implementation `23736d9` / `checkpoint/e-05-research-linkage` and documentation
checkpoint `c2fe5b4` / `checkpoint/e-05-research-linkage-final` remain as
historical checkpoints but are superseded. The reviewed implementation is
`ee00b4a`, designated by `checkpoint/e-05-research-linkage-v2`, and is integrated
on `research-assistant` as `0f81d8b`.

The strict Schema v1 `llmwiki-research-linkage` envelope now requires
`research-linkage-v2`. It records paper, method, innovation, dataset,
configuration, implementation, claim, experiment, result, and unknown entities.
The host declares bounded lowercase kebab-case directed relations instead of
Core pretending a fixed relation vocabulary is scientifically complete.
`implementation`, `paper-claim`, `inference`, and `metadata` provenance remain
separate; inference entities and relations cannot claim observed certainty. A
relation identity binds project, endpoints, relation label, and assertion class,
not mutable prose, Evidence, certainty, or uncertainty.

Every Evidence-bearing build and current load now requires each referenced ID to
exist in the actual current project's Evidence registry. This is registry
referential integrity only, not F-02B Source-version/Locator/excerpt currentness
or scientific verification. Without host semantics the deterministic fallback
emits only uncertain metadata candidates, including configuration and
implementation candidates, and never labels a source-code candidate as observed
implementation provenance or invents a relation. Generation/current loading
bind the exact Manifest, use the Source/Evidence lock order outside
`machine-state.lock`, and recheck the machine artifact before return. Legacy v1
artifacts fail closed without migration.

Validation produced **11 passed** in the focused E-05 suite, **109 passed, 5
skipped** across E-06/E-05/E-04/E-03/E-02/layout/Core/Evidence suites, and **718
passed, 28 skipped** in integrated full regression. Changed-file Ruff,
`py_compile`, and `git diff --check` passed. E-05 opens no registered Source
bytes, calls no LLM, writes no curated Markdown, performs no research-binary
extraction, adds no CLI/MCP/Hook/Web operation, and sends nothing externally.
C-07 remains `deferred/not_started`. See
[`research-linkage.md`](research-linkage.md).

E-06 is complete after validation on **2026-07-18** at implementation
`c1227f8`, designated by `checkpoint/e-06-experiment-chains` and integrated
with `checkpoint/e-06-experiment-chains-final`. The strict Schema v1
`llmwiki-experiment-chains` artifact (`experiment-chains-v1`) records explicit
configuration, run, result, Claim, and result-to-Claim observations as a stable
`config -> run -> result -> claim` closure. Results retain non-empty conditions,
metrics, and at least one Evidence ID. Two experiments may coexist even when
one host-declared result supports a Claim and another contradicts it; Core
preserves both chains, result conditions, and the Evidence closure without
inferring a scientific conflict or selecting a winner.

With no host observations, E-06 emits only bounded Manifest-classification
candidates marked `inferred` with explicit uncertainty and no semantic chain.
Generation and current loading bind to the exact current B-06 Manifest, share
`machine-state.lock`, atomically publish and recheck Manifest bytes, and fail
closed on malformed, legacy/future, unknown-field, duplicate-key,
noncanonical, stale, invalid-endpoint, or stable-identity-tampered artifacts.
Validation produced **8 passed** in the focused E-06 suite, **45 passed, 1
skipped** across dependent E-06/E-05/E-04/layout/Core suites, and **714 passed,
28 skipped** in full regression. Changed-file Ruff, `py_compile`, and
`git diff --check` passed. E-06 opens no registered source bytes, reads no
research binary, calls no LLM, writes no curated Markdown, and sends nothing
externally. C-07 remains `deferred/not_started`. See
[`experiment-chains.md`](experiment-chains.md).

E-07 is complete after validation on **2026-07-18** at implementation
`9f4194b`, designated by `checkpoint/e-07-knowledge-rendering`. The deterministic
renderer produces the fifteen required product entries, collection detail pages,
a unified index, and a dated daily plan. Missing or ungrounded inputs become
explicit `DRAFT` placeholders with stable reason codes and next actions. It
validates Schema v2/path identity and routes persistence through F-05A/F-05B,
protecting Schema v1/future/malformed, user-owned, mixed, and non-draft pages while
preserving mixed user bytes. The Core facade is
`ResearchCoreService.knowledge_render(...)`; it does not read source bytes, call an
LLM, send externally, or process C-07 research binaries. Focused validation
produced **14 passed**, dependent suites **106 passed, 1 skipped**, and full
regression **732 passed, 28 skipped**; Ruff, `py_compile`, and `git diff --check`
passed. C-07 remains **deferred/not_started**. See
[`knowledge-rendering.md`](knowledge-rendering.md).


### R3-minus-C-07 progress (2026-07-19)

E-08 full one-action understand is implemented and validated. The complete
pipeline is deterministic/local-only, keeps the registered source project
read-only, writes machine state only below `.llmwiki/projects/<project_id>/`,
and routes curated Markdown through the controlled writer.

I-01 is now complete at implementation commit `ddd5261` and checkpoint
`checkpoint/i-01-goal-schema`. The strict Goal/Milestone Schema v1 rejects
unknown/future/malformed records, round-trips through a Schema v2 `goals.md`
projection, records explicit draft reasons, and writes only the registered
machine artifact under the shared mutation lock. No C-07 research binary
extraction is introduced; C-07 remains `deferred/not_started`.

I-02 is complete at implementation commit `76a81e3` and checkpoint
`checkpoint/i-02-task-protocol`. Current tasks require explicit `why_now`,
inputs, bounded project-relative write scopes, DoD, verification, artifacts,
canonical Evidence IDs, and acyclic same-collection dependencies. A completed
task requires controlled completion references; title-only and legacy Todo
records remain non-executable draft compatibility views and cannot be silently
serialized. The registered store writes only `indexes/tasks.json` under the
shared mutation lock, while the optional Schema v2 `plans/backlog.md` projection
remains a separate F-05 controlled-write concern. Focused validation produced
19 passed and dependent validation 79 passed, 13 skipped; Ruff, `py_compile`,
and `git diff --check` passed. No source-project content or C-07 research binary
was read or changed; C-07 remains `deferred/not_started`.

I-03 is complete at implementation commit `5988961` and checkpoint
`checkpoint/i-03-project-state`. The strict `project-state-v1` artifact binds the
safe registration projection and exact current Manifest, records version/hash
bindings for available machine/Knowledge inputs, and deterministically summarizes
experiments, results, open questions, blockers, stale Knowledge, stale Evidence,
recent changes, and explicit missing-input gaps. Current loaders fail closed on a
stale registration or Manifest, and all publication uses the shared machine-state
lock with exact pre-replace revalidation. Focused validation produced 9 passed;
dependent validation produced 104 passed, 3 skipped; Ruff, `py_compile`, and
`git diff --check` passed. The implementation reads no registered source bytes,
writes no curated Markdown, and performs no C-07 research-binary extraction.
C-07 remains `deferred/not_started`. See
[`project-state-snapshot.md`](project-state-snapshot.md).

I-04 is complete at implementation commit `c42d9a7` and checkpoint
`checkpoint/i-04-initial-planning`. The strict `initial-plan-v1` artifact binds a
DRAFT Goal and I-02 task collection to the exact current I-03 project-state
revision and publishes only `indexes/initial-plan.json` under the shared
machine-state lock. Explicit caller objectives take precedence over onboarding
`final_goal`; missing intent remains an explicit draft, and onboarding questions
are not inferred as success criteria. Existing Goal/task bytes remain unchanged,
and every automatically proposed task is non-executable.

`ResearchCoreService.plan(...)`, the E-08 plan stage, E-07 Goal/backlog/daily-plan
rendering, and the real `llmwiki_plan` MCP operation share this boundary. Curated
Markdown still flows only through F-05A/F-05B, including byte-preserved mixed user
regions. `llmwiki_query` remains `capability-unavailable` with
`available_after=G-04`. Focused validation produced 71 passed; full regression
produced 794 passed, 28 skipped. Ruff, isolated `py_compile`, UTF-8 health, and
`git diff --check` passed. No source-project or research-binary bytes were read or
changed, no LLM/external send occurred, and no task execution was authorized.
C-07 remains `deferred/not_started`. See
[`initial-planning.md`](initial-planning.md).

J-03 is complete at implementation commit `15ae906` and checkpoint
`checkpoint/j-03-product-cockpit`. The product cockpit is separate from the
repository development-supervision dashboard and provides loopback-only,
read-only views for registered projects, all fifteen canonical Knowledge
entries, Manifest/coverage and file-state details, Claim -> Evidence -> Source
-> Locator traces, Goal/task/plan state, and bounded run usage/cost/error data.
Strict Knowledge Schema v2/path validation, regular-file and redirection checks,
absolute-path redaction, restrictive browser headers, and request/Host/method
bounds preserve the local read-only surface. At the J-03 checkpoint,
`llmwiki_query` remained `capability-unavailable` with `available_after=G-04`,
and Web editing was deliberately deferred to J-04. Focused validation produced
15 passed, 3 skipped;
dependent validation produced 234 passed, 12 skipped. Ruff check/format,
`py_compile`, UTF-8/LF checks, and `git diff --check` passed. No source-project
content or research binary was read or changed, no LLM/external send occurred,
and C-07 remains `deferred/not_started`. See
[`research-cockpit.md`](research-cockpit.md).

J-04 is complete at implementation commit `cf5cfcd` and checkpoint
`checkpoint/j-04-controlled-web-editing`. The J-03 cockpit remains read-only by
default; `serve --enable-editing` creates a trusted local session for exactly
four renderer-bound mixed user regions: Goal, backlog, project status, and
user-confirmed conclusions. There is no arbitrary path or frontmatter editor.
Each save validates the strict current Knowledge Schema v2 page and fixed mixed
skeleton, uses the full-page SHA-256 as an optimistic revision, advances only
`updated_at`, independently recomputes the F-05A plan, and persists through
F-05B exact CAS plus its body-free audit ledger. Stale/racing writes return 409
without overwrite, commit/audit-unknown states return body-free 503, browser
conflicts retain the unsaved draft, and E-07 regeneration preserves the edited
mixed user bytes.

Focused J-03/J-04 validation produced **30 passed, 3 skipped**; dependent F-05,
renderer, Core, understanding, Goal/task/state/planning validation produced
**161 passed, 2 skipped**; full regression produced **824 passed, 31 skipped**.
Ruff check/format, `py_compile`, UTF-8/LF checks, and `git diff --check` passed.
The source project remained unchanged, and no LLM, external send, source reopen,
research-binary read, Claim verification, task execution, Query, or R4 behavior
was introduced. Query remains exactly `capability-unavailable` with
`available_after=G-04`; C-07 remains `deferred/not_started`. See
[`research-cockpit.md`](research-cockpit.md).

J-01 R3-minus-C-07 acceptance is complete at implementation commit `9e2eba2`
and checkpoint `checkpoint/j-01-r3-end-to-end`. A standalone fixed-fixture test
runs two independent clean temporary workspaces through `project_understand ->
source registry/Evidence/Locator binding -> source open -> MCP
query(unavailable) -> H-07 full-scan reconciliation -> I-04 DRAFT planning ->
E-07/F-05 controlled rendering`. Each run validates all fifteen canonical
Knowledge paths, the unified index and dated daily plan, strict Knowledge Schema
v2 parsing, canonical body-free prepared/committed F-05B audit pairs, current
Source/Evidence/hash/excerpt fidelity, and byte-identical path-independent
structured outcomes.

The integration defect exposed by that chain is fixed narrowly: after H-07
advances the Manifest generation, I-04 may rebuild only a well-formed I-03 state
whose registration/Manifest binding is stale. Malformed, legacy/future,
unknown-field, or Goal/task-revision drift still fails closed, so this refresh is
not a general stale-propagation claim. The reconciled plan remains DRAFT and all
automatically proposed tasks remain non-executable.

The acceptance uses a binary canary classified only as a limited
`model_checkpoint`/`model_artifact`; it confirms no semantic extraction or
canary leakage. It also guards LLM, network, URL, and browser access and verifies
that source hashes, sizes, mtimes, modes, and directory metadata remain
unchanged. Focused validation produced **19 passed**; dependent validation
produced **339 passed, 12 skipped**; final full regression produced **826 passed,
31 skipped**. Ruff, `py_compile`, and `git diff --check` passed. MCP Query remains
exactly `capability-unavailable` with `available_after=G-04`; C-07 remains
`deferred/not_started`; no R4 behavior was introduced.
