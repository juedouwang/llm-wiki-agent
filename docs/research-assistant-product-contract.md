# 科研助手最终产品与项目扫描产物约定

- 状态：**已确认，作为后续开发的产品基线**
- 决策编号：`P-05 Agent 原生执行闭环`
- 确认日期：2026-07-15
- 适用分支：`research-assistant`
- 当前实现状态：R1 确定性基础链已于 2026-07-16 验收；R2 的 G-01 Research Core service facade 与 G-07 minimal MCP server 已实现。G-07 query, reconcile, and plan remain explicit unavailable contracts; they do not represent completed later Core capabilities. 本文其余内容仍描述目标产品，不代表后续能力已经全部实现。

## 1. 产品定义

`llm-wiki-agent` 的目标产品不是另一个与 Codex、Claude Code 并列的聊天机器人，也不只是一个 `AGENTS.md`、Skill、Plugin 或 MCP Server。

最终定义是：

> 一个可嵌入主流宿主 Agent、本地优先、Evidence-first 的科研工作台内核。它负责项目登记、完整扫描、来源与证据、长期知识、项目状态、任务规划、执行核验、增量更新和本地网页；Codex、Claude Code 等宿主 Agent 负责用户对话、推理与实际执行。

核心设计原则：

1. **用户体验上集成，工程实现上解耦。** 用户只在当前宿主 Agent 中工作，不在两个聊天窗口之间手工搬运计划和结果。
2. **Research Core 独立于模型和宿主。** 更换 Codex、Claude Code 或模型 Provider 时，不丢失项目状态、知识和任务。
3. **Markdown 是长期科研知识源。** HTML 网站是从 Markdown 和机器状态生成的管理视图，可随时重建。
4. **原始科研项目默认只读。** 扫描、提取和总结不得修改源项目；只有宿主 Agent 在用户任务授权下才可以修改项目文件。
5. **重要结论必须可回源。** 摘要不是最终证据，关键事实要能够重新定位并核验原文件。
6. **一键完成是产品契约。** 技术上可以分阶段、缓存和重试，但用户第一次导入项目时只需要一次操作。

## 2. 产品形态与职责边界

```text
Codex / Claude Code / 其他兼容 Agent
└─ 宿主适配包
   ├─ Plugin：安装和分发边界
   ├─ Skills：科研工作流与使用规则
   ├─ Hooks：会话和工具生命周期信号
   ├─ MCP：调用 Research Core 的标准接口
   └─ 可选 AGENTS.md / CLAUDE.md：仓库级轻量引导
                    ↓
          llmwiki Research Core
          ├─ project registry / scan / classify
          ├─ extract / evidence / source resolution
          ├─ knowledge / claims / relations
          ├─ tasks / runs / status / verification
          ├─ incremental reconciliation
          ├─ MCP / local API / CLI
          └─ local web renderer
                    ↓
        Markdown 知识 + 机器状态 + 原科研项目
```

### 2.1 宿主 Agent

宿主 Agent 是用户的主要交互和执行入口，负责：

- 理解用户当下目标；
- 调用 Research Core 获取项目背景、证据、状态和任务；
- 制定计划并直接执行代码、文档、实验或分析任务；
- 运行测试、命令和实验；
- 将执行结果、产物和失败原因交回 Research Core 核验。

### 2.2 Research Core

Research Core 是产品本体，负责：

- 维护稳定的 `project_id`、`source_id`、文件版本和 Evidence；
- 确保扫描范围内没有静默遗漏；
- 保存长期知识、当前状态、目标、任务和运行记录；
- 在源文件变化后传播 `stale` 状态并选择性刷新；
- 为宿主 Agent 提供可验证上下文，而不是替宿主再开一个聊天会话；
- 根据 diff、测试、日志、实验产物或用户确认核验任务完成情况；
- 生成本地科研管理网站。

### 2.3 MCP、Plugin、Skill、Hook 与指导文件

| 形态 | 在本项目中的职责 | 不应承担的职责 |
|---|---|---|
| MCP | 跨宿主调用 Core 的标准工具和上下文接口 | 不单独充当完整产品或长期状态数据库 |
| Plugin | 面向 Codex、Claude Code 的安装、配置和分发包 | 不复制 Core 的业务数据和知识库 |
| Skill | 告诉宿主何时以及按什么流程调用科研能力 | 不保存项目事实和任务真相 |
| Hook | 提供会话开始、工具执行后、停止边界等增量信号 | 不作为唯一变更来源或唯一安全边界 |
| `AGENTS.md` / `CLAUDE.md` | 仓库级约定、命令和轻量入口说明 | 不承担状态数据库、任务队列或证据索引 |
| Web | 阅读、管理、审计和回源界面 | 不成为第二个要求重复对话的 Agent |

Hooks 可能被禁用、未信任、配置错误或因宿主版本不同而不可用。因此 Hook 事件只作为低延迟信号；Git diff、文件指纹和 Manifest reconciliation 才是最终一致性的依据。

### 2.4 最终用户的安装和使用路径

最终用户不需要把科研项目复制进 `llm-wiki-agent` 源码仓库，也不需要同时打开两个项目：

1. 在本机安装一次 Research Core；
2. 为当前宿主安装 Codex 或 Claude Code 适配包；
3. 配置机器状态根目录、`knowledge_root` 和隐私策略；
4. 直接在真实科研项目目录中打开宿主 Agent；
5. 触发“一键理解这个项目”，后续在同一 Agent 中计划、执行和复盘；
6. 需要更直观管理时打开本地 Web 驾驶舱。

宿主适配包只保存连接配置和工作流，不在每个科研项目中复制一套 Core 或知识库。

## 3. 数据边界与最终目录

### 3.1 原科研项目

- 默认只读扫描；
- Core 不在源项目中写入 Wiki、缓存、索引或状态文件；
- 宿主 Agent 修改源文件时，仍遵循宿主自身的权限和用户授权；
- 模型权重、大型数据集、缓存和海量图片通常只登记元数据，不做完整语义读取；
- 架构图、流程图、关键结果图、论文 PDF 等可以被提升为深读对象。

### 3.2 机器状态

以下目录是默认逻辑布局。机器状态根目录可由 Core 配置集中管理，但不得默认写入被扫描科研项目：

```text
.llmwiki/projects/<project_id>/
├─ project.yaml
├─ manifest.jsonl
├─ sources.jsonl
├─ evidence.jsonl          # 规划项；由后续 D 阶段实现
├─ relations.jsonl         # 规划项；可从知识与 Evidence 重建
├─ events.jsonl            # 规划项；宿主事件与增量同步账本
├─ extracted/
├─ indexes/
└─ runs/
```

机器状态保存路径、hash、提取结果、Evidence locator、运行事件和派生索引。它不应成为人类长期阅读科研结论的主要位置。

### 3.3 人类可读的 Markdown 知识

`knowledge_root` 必须可配置到用户的个人知识库；下面以仓库内默认的 `wiki/` 为例。无论配置到哪里，都不得强制写入被扫描科研项目：

```text
wiki/
├─ projects/
│  └─ <project_id>/
│     ├─ index.md
│     ├─ overview.md
│     ├─ project-map.md
│     ├─ reproduction.md
│     ├─ architecture.md
│     ├─ papers/
│     │  └─ index.md
│     ├─ methods/
│     │  └─ index.md
│     ├─ datasets/
│     │  └─ index.md
│     ├─ experiments/
│     │  └─ index.md
│     ├─ results/
│     │  └─ index.md
│     ├─ claims/
│     │  └─ index.md
│     ├─ open-questions.md
│     ├─ status.md
│     ├─ risks.md
│     ├─ goals.md
│     ├─ plans/
│     │  ├─ backlog.md
│     │  └─ daily/
│     │     └─ YYYY-MM-DD.md
│     ├─ decisions/
│     └─ sources/
└─ library/
   ├─ papers/
   ├─ methods/
   ├─ datasets/
   ├─ metrics/
   ├─ concepts/
   └─ lessons/
```

项目知识先保留在项目目录中；只有具有明确复用价值且来源可靠的内容，才显式提升到 `wiki/library/`。

### 3.4 HTML / 本地网站

- 第一版仅绑定回环地址，默认在 `localhost` 提供服务；
- 网站内容由配置的 `knowledge_root`、机器状态和运行报告生成，可重建；
- 网站可编辑目标、任务、当前状态和用户确认结论；
- 编辑必须经过受控写入器，落回对应 Markdown，并记录更新时间和来源；
- 自动生成内容与用户确认内容必须区分，重新生成时不得覆盖用户确认内容；
- 网站不是新的知识真相来源，也不是第二个聊天窗口。

## 4. 一键项目理解契约

用户入口应当是一个动作，例如：

```text
llm-wiki project understand <project-path> --open
```

或宿主 Agent 中语义等价的“一键理解这个科研项目”。

内部流水线允许拆成可重试阶段：

```text
register
→ inventory
→ classify
→ extract
→ adaptive-read
→ synthesize
→ evidence
→ status
→ plan
→ index
→ web-render
```

产品要求：

1. 用户只触发一次；
2. 每个阶段可单独重试、断点续跑和复用缓存；
3. 某些文件失败不能让整个项目静默消失，最终报告必须列出失败项；
4. 一次运行产生唯一 `run_id`，记录阶段、耗时、输入版本、模型/Provider、费用估算、错误和产物；
5. 第 14、15 项在用户未填写目标信息时仍要生成，但标为 `DRAFT / 待确认`。

首次登记可选信息：

```text
项目最终目标
当前阶段
当前最重要的问题
截止时间
每天可用时间
```

这些字段可以跳过，不应阻止项目扫描和知识生成。

## 5. 一键扫描后必须得到的 15 类资料

| # | 资料 | Markdown 主要落点 | 最低内容要求 |
|---:|---|---|---|
| 1 | 项目总体介绍 | `overview.md` | 目标、领域、主要能力、当前成熟度、关键入口 |
| 2 | 项目目录和模块结构 | `project-map.md` | 目录树、语言统计、模块职责、关键文件 |
| 3 | 运行和复现方法 | `reproduction.md` | 环境、依赖、数据准备、命令、已知缺失条件 |
| 4 | 代码执行流程和数据流 | `architecture.md` | 入口、主要调用链、数据输入输出、关键图示 |
| 5 | 关联论文总结 | `papers/index.md` 与论文页面 | 论文主张、方法、与项目代码的关系、Evidence |
| 6 | 方法和创新点 | `methods/index.md` | 方法组成、相对基线、创新点、证据与不确定性 |
| 7 | 数据集说明 | `datasets/index.md` | 来源、划分、预处理、格式、风险和缺失信息 |
| 8 | 实验清单 | `experiments/index.md` | 实验 ID、配置、代码版本、输入、运行状态 |
| 9 | 指标和结果对比 | `results/index.md` | metric、数值、实验条件、基线、冲突结果 |
| 10 | 已确认科研结论 | `claims/index.md` 与 Claim 页面 | 结论状态、支持/反对 Evidence、最后核验时间 |
| 11 | 未解决问题 | `open-questions.md` | 问题、影响、所需证据、相关任务 |
| 12 | 当前项目状态 | `status.md` | 已完成、进行中、阻塞、最近变化、stale 知识 |
| 13 | 风险和缺失资料 | `risks.md` | 未读文件、失败提取、复现缺口、隐私和技术风险 |
| 14 | 初始目标与任务建议 | `goals.md`、`plans/backlog.md` | 目标、里程碑、任务依赖、完成标准、依据 |
| 15 | 今日任务计划 | `plans/daily/YYYY-MM-DD.md` | `why_now`、时间盒、输入、产出、验证和阻塞 |

`index.md` 应提供这 15 类资料的统一导航，并显示生成状态、最后刷新时间和需要用户确认的项目。

## 6. Markdown 页面与证据约定

长期知识页面至少应包含以下机器可读信息：

```yaml
---
schema_version: 1
project_id: <project_id>
artifact_type: overview | paper | method | dataset | experiment | result | claim | plan | ...
status: draft | verified | stale | conflicting | rejected
ownership: generated | mixed | user
source_ids: []
evidence_ids: []
generated_at: <timestamp>
last_verified_at: <timestamp-or-null>
---
```

约束：

- 关键事实不得只写模糊的“来自某文件”；
- Evidence 至少包含 `source_id + content_hash + locator + excerpt_hash`；
- 定位可使用代码行号、PDF 页码/区域、Notebook cell、表格 sheet/cell range、PPT slide 或 DOCX paragraph；
- `verified` 表示当前版本原文件已经重新核验，不等同于“模型很有把握”；
- 用户确认结论必须可与机器生成结论区分；
- 源文件变化后，依赖它的页面和 Claim 应自动变为 `stale`，直到重新核验。

## 7. 自适应文件阅读策略

### 7.1 三遍式流程

1. **全量盘点**：扫描边界内的每个文件进入 Manifest；明确列出扫描边界外的目录规则。
2. **初步建模**：识别格式、科研角色、项目位置、引用关系、文件大小和潜在敏感性。
3. **自适应深读**：根据科研价值、引用关系和当前目标决定读取深度；关键文件可自动提升优先级。

### 7.2 两轴状态

不能用一个状态同时表达“处理成功与否”和“读了多深”。最终 Manifest 应至少分成两轴：

```text
processing_status:
  discovered | processed | partial | failed | missing

read_depth:
  deep_read | normal_read | sampled | metadata_only | ignored | unsupported
```

每条记录还必须给出 `reason`。例如：

```text
data/train/000001.png       → sampled       / 大型训练数据集样本
results/ablation_curve.png → deep_read     / 关键结果图
models/model.ckpt          → metadata_only / 模型权重，不做语义解析
.env                       → ignored       / 凭证风险，禁止发送内容
```

“支持所有格式”的验收含义是：

> 扫描范围内每个文件都有确定的处理状态、读取深度和理由；不承诺完整理解所有二进制格式。

当 README、代码、论文或实验配置引用了尚未深读的文件时，系统应把该文件加入提升队列，而不是继续基于缺失信息总结。

## 8. 跨项目知识约定

1. 项目内知识默认相互隔离；
2. 论文、方法、数据集、指标、概念和复盘经验可以显式提升到全局库；
3. 同名对象不得自动合并；需要 DOI、内容指纹、规范标识或人工确认；
4. 全局结论仍需保留项目、来源版本和 Evidence；
5. 当全局知识更新时，引用它的项目应显示影响范围，而不是静默覆盖项目结论；
6. Codex 和 Claude Code 访问同一个 Core、Markdown 和索引，因此切换宿主后可继续同一项目。

## 9. Agent 原生执行闭环

目标交互：

```text
打开项目
→ 宿主识别/选择 project_id
→ Core 提供状态、目标、任务、风险和相关 Evidence
→ 用户提出目标
→ 当前宿主 Agent 制定计划并直接执行
→ Hooks 记录工具和文件变化信号
→ Core 使用 diff、Manifest 和运行产物进行 reconciliation
→ 测试/日志/实验产物/用户确认共同核验完成状态
→ 增量更新 Markdown、索引和网页
→ 当前宿主继续下一任务
```

任务执行包保留为 Core 与宿主之间的内部协议：

```yaml
task_id:
why_now:
inputs:
evidence:
allowed_paths:
forbidden_paths:
dependencies:
timebox:
definition_of_done:
verification:
expected_artifacts:
status:
```

用户不需要复制粘贴此任务包。只有在人工协作、归档或宿主不支持 MCP 时才导出给用户。

任务完成不得只依据模型自报。至少需要以下一种或多种证据：

- Git diff 或文件内容变化；
- 测试/检查命令结果；
- 实验日志和指标；
- 预期产物存在且格式正确；
- 用户显式确认；
- 受控的外部系统状态。

## 10. 增量更新与重新扫描边界

修改源文件后，技术上仍需重新读取变化内容，但必须在同一工作流内自动完成。

默认行为：

1. Hook 或文件观察信号标记 `dirty paths`；
2. 在合适边界比较 Git diff、文件 hash 和上一代 Manifest；
3. 只重新提取变化文件；
4. 计算 `source → summary → claim → synthesis → plan` 影响范围；
5. 把受影响知识标为 `stale`；
6. 重新生成并核验受影响内容；
7. 刷新 Markdown、索引和网页。

只有以下情况需要全量扫描：

- 首次导入；
- 用户请求完整性检查；
- 大规模外部变更，无法可靠获得变更集；
- Manifest 或 Schema 升级；
- 状态损坏或一致性检查失败；
- 项目根目录或扫描策略发生重大变化。

## 11. Web 驾驶舱最低功能

第一版本地网站至少提供：

1. 项目列表和一键导入；
2. 15 类扫描资料导航；
3. 覆盖率、失败文件、读取深度和原因；
4. Claim → Evidence → 原文件定位的逐级回溯；
5. 新增、修改、移动、删除和 stale 影响报告；
6. 目标、任务、今日计划和完成证据；
7. 运行历史、错误、耗时和费用估算；
8. 可编辑目标、任务、状态和用户确认结论；
9. 跨项目论文、方法、数据集和经验库；
10. 清晰区分 `DRAFT`、`VERIFIED`、`STALE`、`CONFLICTING`。

## 12. 模型调用、隐私与安全边界

- `.env`、凭证、密钥和明确的私密目录永不发送到外部模型；
- 原始大型数据集、海量图片、模型权重默认不发送；
- 代码、文档和论文文本是否发送由项目策略和用户配置决定；
- 未配置或未确认项目隐私策略时，Research Core 主动向独立外部模型发送原始内容默认采用 local-only；
- 每项目支持禁止发送目录和文件模式；
- 调用前应估算 Token/费用，并在运行报告中记录 Provider、模型和用量；
- 支持纯本地扫描；语义理解可使用宿主 Agent，也可选择本地模型或独立 Provider；
- 源文件内容一律视为不可信数据，不能把文件中的提示词当作系统指令；
- 本地网站默认只监听回环地址；远程绑定、共享和多用户认证不属于第一版默认行为。

### 12.1 模型调用模式

**集成模式（默认）**：Codex/Claude Code 的模型负责推理、计划和执行，Core 提供上下文、工具、证据、状态与持久化，不强制再调用另一套 LLM API。

**独立/无人值守模式（可选）**：后台定时论文处理、批量刷新或仅使用网站时，Core 可以配置独立 Provider 或 headless runner，但仍写入同一份项目状态和知识库。

## 13. 第一版明确不做的事情

- 不构建另一个必须与 Codex/Claude Code 手工交接的聊天机器人；
- 不承诺完整解析所有模型权重和任意私有二进制格式；
- 不自动语义理解大型训练图片集中的每一张图片；
- 不在没有完成证据时自动把任务标为完成；
- 不因同名自动合并跨项目知识；
- 不把 Hooks 当作不可绕过的安全沙箱；
- 不默认提供公网、多用户、云端协作网站；
- 不以向量数据库替代稳定来源、Evidence 和 Verified Query；
- 不要求第一版完成完整编译器级静态分析。

## 14. 最低验收场景

只有以下场景全部通过，才能称为“完整科研助手第一版”：

1. 对固定科研 fixture 一键运行后生成全部 15 类资料和本地网页；
2. 扫描范围内每个文件都有处理状态、读取深度和理由，覆盖率可核对；
3. 能读取代码、PDF、Notebook、配置、主要表格和关键科研图；
4. 模型权重、大型数据和缓存有元数据记录但不会被错误深读；
5. 关键结论能回到原始页码、行号、cell、slide 或单元格；
6. 文件移动后，内容不变时能恢复同一 `source_id`；
7. 文件变化后，相关 Claim 和计划自动变为 `stale`；
8. Verified Query 会重开当前版本原文件，证据不足时拒绝给出确定结论；
9. 用户在 Codex 中执行任务后，无需手工再次全量扫描即可更新状态和知识；
10. 切换到 Claude Code 后能够看到同一项目、任务、Evidence 和运行历史；
11. 网站编辑目标、任务或用户确认结论后，Markdown 正确更新且不会被下次生成覆盖；
12. 外部模型关闭时，仍可完成登记、扫描、覆盖率、指纹和确定性提取；
13. 隐私策略阻止密钥和禁止发送路径进入模型上下文；
14. 所有关键流程有自动化测试、运行报告和可回滚 Schema 迁移策略。

## 15. 已确认的产品决策

| 决策 | 结论 |
|---|---|
| P-01 存储边界 | 机器状态放 `.llmwiki/`，长期知识放 `wiki/`，源项目默认只读 |
| P-02 展示形式 | Markdown 为长期知识源，本地 HTML 网站为可重建驾驶舱 |
| P-03 项目导入 | 用户一键完成，内部阶段化、可重试、可增量 |
| P-04 文件阅读 | 全量盘点 + 初步建模 + 自适应深读，每个文件有状态和理由 |
| P-05 执行方式 | Agent 原生闭环；计划器和执行器是同一宿主 Agent 的不同阶段 |
| P-06 跨项目知识 | 项目隔离、显式提升、来源不丢失、同名不自动合并 |
| P-07 模型边界 | 默认复用宿主模型；独立 Provider 仅用于可选后台模式 |

## 16. 官方集成依据与兼容策略

以下能力在 2026-07-15 依据官方文档核对：

- Codex Plugin 可打包 Skills、Hooks、MCP 配置和应用连接，并通过 `.codex-plugin/plugin.json` 描述；
- Codex 支持 MCP Server，并可由 CLI、IDE 和应用共享配置；
- Codex Hooks 提供会话和工具生命周期事件，但插件 Hook 需要信任，当前处理器能力也可能随版本演进；
- `AGENTS.md` 适合持久仓库指导，但不适合承担数据库职责；
- Claude Code Plugin 可打包 Skills、Agents、Hooks 和 MCP；
- Claude Code Hooks 包含 `SessionStart`、`PostToolUse`、`Stop` 等适合增量同步的事件；
- MCP 使用 client/server 架构，将宿主 Agent 与本地或远程能力解耦。

参考：

- [Codex Plugin](https://developers.openai.com/codex/plugins/build)
- [Codex MCP](https://developers.openai.com/codex/mcp)
- [Codex Hooks](https://developers.openai.com/codex/hooks)
- [Codex AGENTS.md](https://developers.openai.com/codex/guides/agents-md)
- [Claude Code Plugins](https://code.claude.com/docs/en/plugins)
- [Claude Code Hooks](https://code.claude.com/docs/en/hooks-guide)
- [Claude Code MCP](https://code.claude.com/docs/en/mcp)
- [MCP Architecture](https://modelcontextprotocol.io/docs/learn/architecture)

宿主文档和事件名称可能变化，因此：

1. Core API、Schema 和数据目录不得依赖某个宿主的事件名称；
2. Codex 与 Claude Code 使用独立适配包；
3. 适配包声明支持的宿主版本并单独测试；
4. Hook 缺失时，Skill/MCP 显式 reconciliation 必须仍能闭环；
5. 宿主升级不得要求迁移或复制长期知识库。
