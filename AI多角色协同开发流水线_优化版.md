# AI 多角色协同开发流水线架构方案（优化版）

> 基于 v1.0 方案的 6 项关键优化：角色精简、并行度提升、目录冲突规避、Token 管控务实化、数据传递标准化、复杂度分层路由

---

# 一、整体架构总览

## 核心架构链路

**用户端 QQBot 入口 → 复杂度判断路由 → OpenClaw 全局中枢调度 → 多角色专业流水线串行+并行协作 → GitHub 代码仓库托管部署 → 熔断机制兜底 + 人工介入卡点**

> 🔄 **优化点 1**：新增复杂度判断分支，简单需求（修Bug/加字段/改样式）跳过 PE+PM+Arch 长链路，直接路由至开发角色执行。

## 架构核心增强能力

| 能力模块 | 核心落地细节 |
|---|---|
| 本地全量日志持久化 | 分级日志目录（`ai_dev_logs/task_logs/token_logs/error_logs` 等）、结构化日志格式、自动落盘不丢失，支持QQ实时查阅与GitHub归档 |
| 智能定时进度汇报 | 自定义间隔（15/30/60分钟）、多场景差异化推送、停滞检测（30分钟无进度额外提醒） |
| 全局熔断重试体系 | 单任务独立重试计数（持久化至 `._pipeline_private/retry_stats.db`）、3次阈值强制暂停 + QQ标准化告警 |
| 全局Token精细化管控 | 三层监控（单轮/单角色/项目累计）、趋势预警（连续3个子任务超预算才告警）、上下文自动裁剪、分片下发任务 |
| 敏感信息隔离机制 | OpenClaw 独立加密存储密钥/Token/密码（`._pipeline_private/secrets.enc`）、全程不透传至下游角色 |
| 强制人工关键卡点 | 需求定稿/架构定稿/版本上线/熔断恢复四大节点，无人工【确认】指令永久停滞 |
| 全局唯一事实源管控 | `docs/` 目录由 OpenClaw 内置文档模块直接维护（非独立角色），所有角色必须遵从，变更由 OpenClaw 统一同步 |

> 🔄 **优化点 2**：Claude-Doc 由独立角色降级为 OpenClaw 内置文档管理模块，减少一次不必要角色调用的链路延迟和 Token 消耗。

## 架构核心设计原则

- **角色解耦**：每个角色职责单一、边界绝对清晰，无跨岗、越权操作
- **中枢统一调度**：所有消息/任务/变更/流转经 OpenClaw 中转，角色间禁止直接通信
- **复杂度分层**：简单需求走短链路，复杂需求走完整流水线，避免小任务过度流程化
- **规范先行**：先标准化、再拆分任务、再开发测试
- **可观测可追溯**：全流程日志、Token 消耗、任务状态、重试记录全链路可查
- **容错可兜底**：自动重试、熔断暂停、人工介入三重保障
- **契约驱动**：前后端以 OpenAPI 3.0 规范文件为唯一接口契约，杜绝理解偏差

> 🔄 **优化点 3**：新增复杂度分层原则和契约驱动原则。

---

## 任务复杂度判断路由

```
用户需求 → OpenClaw 复杂度判断
├─ 🟢 简单任务（改文案/修Bug/加单字段/调样式）
│   └→ 直接路由至对应开发角色（Backend/Frontend）
│       └→ QA → SCM → Release → DevOps（精简链路，5个角色）
│
├─ 🟡 中等任务（新增单模块/新增CRUD接口）
│   └→ PE 拆分子任务（跳过 PM+Arch，基于现有规范直接开发）
│       └→ Backend/Frontend → QA → SCM → Release → DevOps（中等链路，7个角色）
│
└─ 🔴 复杂任务（新功能体系/架构变更/跨模块重构）
    └→ PE → PM + Arch（并行）→ 文档同步 → Backend/Frontend → QA → SCM → Release → DevOps
        （完整链路，9个角色）
```

### 复杂度自动判定规则

| 判定维度 | 🟢 简单 | 🟡 中等 | 🔴 复杂 |
|---|---|---|---|
| 涉及角色数 | ≤2 | 3-4 | ≥5 |
| 新增接口数 | 0 | 1-3 | ≥4 |
| 新增表数 | 0 | 0-1 | ≥2 |
| 是否需要架构变更 | 否 | 否 | 是 |
| 预估 Token 消耗 | <20K | 20K-80K | >80K |

> 💡 复杂度判断由 OpenClaw 根据 PE 的初步分析结果自动判定，同时支持人工通过 QQ 指令覆盖：`强制完整流水线` / `快速开发模式`。

---

# 二、标准化项目目录结构（优化版）

```
Project-Root/
├── .github/                         # GitHub 自动化与规范 (Claude-SCM 专属)
├── ._pipeline_private/              # 🔄 改名：避免与 OpenClaw 系统目录冲突
├── ai_dev_logs/                     # 全局分级日志持久化
├── config/                          # 核心运行配置（非敏感，Git可托管）
├── docs/                            # 全局唯一事实源 (OpenClaw 内置文档模块维护)
├── backend/                         # 后端源码 (Claude-Backend 产出)
├── frontend/                        # 前端源码 (Claude-Frontend 产出)
├── tests/                           # 质量管理 (Claude-QA 专属)
├── deploy/                          # 运维部署 (Claude-DevOps 专属)
├── .env.example
├── .gitignore
└── README.md
```

### 🔄 关键变更说明

| 变更项    | 原方案             | 优化方案                  | 原因                                  |
| ------ | --------------- | --------------------- | ----------------------------------- |
| 项目私有目录 | `.openclaw/`    | `._pipeline_private/` | 避免与 OpenClaw 系统目录 `~/.openclaw/` 冲突 |
| 文档管理方式 | Claude-Doc 独立角色 | OpenClaw 内置文档模块       | 减少不必要的角色调用，降低延迟和 Token              |
| 接口契约   | 文字描述            | OpenAPI 3.0 规范文件      | 机器可读，前后端统一解析，杜绝理解偏差                 |
| 角色总数   | 10 个角色          | 9 个角色                 | Claude-Doc 降级为内置模块                  |

---

# 三、九大角色专属 Prompt（优化版）

> 🔄 Claude-Doc 不再作为独立角色，其职责由 OpenClaw 内置文档模块承担。

## 1. Claude-PE 提示词工程师｜复杂度判断 + 任务拆分
流水线入口、复杂度分析中枢、上下文瘦身师、Token管控第一道关卡

## 2. Claude-PM 产品经理
业务需求翻译官、PRD唯一产出方、功能逻辑与验收标准制定者

## 3. Claude-Arch 架构师
技术总设计师、架构方案/数据库/接口/模块划分唯一负责人

## 4. Claude-Backend 后端开发工程师
纯业务编码执行者，按子任务精准落地后端代码

## 5. Claude-Frontend 前端开发工程师
页面与组件专属开发，按 OpenAPI 契约对接后端接口

## 6. Claude-QA 测试工程师
全流程质量把关者，功能/边界/安全/异常场景全覆盖测试

## 7. Claude-SCM Git & 版本控制工程师
代码版本与分支管理规范制定者，Git流程唯一标准输出

## 8. Claude-Release 发布工程师
版本管理与发版总负责人，语义化版本与变更日志管理者

## 9. Claude-DevOps 运维工程师
部署/CI-CD/环境/监控方案专属设计者

---

# 四、OpenClaw 全局调度规则（优化版）

## 核心定位
作为**中枢调度中心、消息唯一中转节点、本地执行代理、GitHub操作执行者、Token监控器、重试熔断控制器、日志持久化存储器、定时任务管理器、文档模块维护者、QQBot联动核心**。

## 🔄 关键变更

### 1. 文档模块（替代原 Claude-Doc 角色）
OpenClaw 内置文档管理模块，职责：
- 接收 PM/Arch 产出的文档，校验格式完整性
- 统一维护 `docs/` 目录结构、版本号、变更日志
- 所有角色读取 `docs/` 获取最新规范（只读）
- 变更推送时自动更新 `docs/` 对应文件
- 版本变更时自动生成 diff 对比和变更摘要

### 2. 复杂度路由（新增）

```
用户需求消息
    │
    ▼
OpenClaw → Claude-PE 复杂度分析
    │
    ├─ 🟢 简单 → OpenClaw 直接路由至开发角色
    │            （跳过 PM + Arch，节省 3-5 轮调用）
    │
    ├─ 🟡 中等 → PE 拆分 → 开发角色
    │            （跳过 PM + Arch，基于现有 docs/ 规范开发，节省 2-3 轮调用）
    │
    └─ 🔴 复杂 → PE 拆分 → PM ∥ Arch（并行） → 文档同步 → 开发角色
                 （完整链路，PM和Arch可并行启动）
```

### 3. Token 管控：从实时拦截改为事后趋势预警
| 策略层级 | 方式 | 触发条件 | 动作 |
|---|---|---|---|
| 预防层 | PE 下发任务时设置 Token 预算 | 每次任务拆分 | 在子任务中声明预算上限 |
| 统计层 | 任务完成后统计实际消耗 | 每个子任务完成 | 写入 `ai_dev_logs/token_logs/` |
| 预警层 | 趋势分析 | 连续 3 个子任务超过预算 120% | QQ 推送预警通知 |
| 阻断层 | 项目累计预算 | 项目累计消耗接近阈值 90% | QQ 推送预算警告 + 建议裁剪 |

---

# 五、全流程 Mermaid 流程图（优化版）

```mermaid
flowchart TD
  U[👤 用户] -->|QQ 发送开发指令| Q[QQBot 入口网关]
  Q -->|转发原始需求| O[OpenClaw 全局中枢调度]
  O -->|下发需求分析| PE[Claude-PE 复杂度判定+任务拆分]
  
  PE -->|返回判定结果| JUDGE{复杂度判定？}
  
  JUDGE -->|🟢 简单| SKIP[跳过 PM+Arch]
  JUDGE -->|🟡 中等| PE_SPLIT[PE 直接拆分开发任务]
  JUDGE -->|🔴 复杂| FULL[完整流水线]
  
  SKIP --> BACK_SIMPLE[Claude-Backend]
  SKIP --> FRONT_SIMPLE[Claude-Frontend]
  PE_SPLIT --> BACK_MID[Claude-Backend]
  PE_SPLIT --> FRONT_MID[Claude-Frontend]
  
  FULL --> PM_ARCH_PARALLEL{{PM 与 Arch 并行启动}}
  PM_ARCH_PARALLEL --> PM[Claude-PM → docs/prd/]
  PM_ARCH_PARALLEL --> Arch[Claude-Arch → docs/architecture/ + OpenAPI契约]
  PM --> DOC_SYNC[OpenClaw 文档模块同步 → docs/]
  Arch --> DOC_SYNC
  DOC_SYNC --> O
  
  BACK_SIMPLE & FRONT_SIMPLE & BACK_MID & FRONT_MID & BACK_FULL & FRONT_FULL --> O
  O --> QA[Claude-QA 测试+契约校验 → tests/]
  QA --> O
  
  O --> SCM[Claude-SCM Git规范 → docs/standard/git_standard.md]
  SCM --> O
  
  O --> Release[Claude-Release 版本发布 → docs/release/]
  Release --> O
  
  O --> DevOps[Claude-DevOps CI/CD+部署 → deploy/ + .github/workflows/]
  DevOps --> O
  
  subgraph OpenClaw 内置核心能力
    direction TB
    DOC[📄 文档模块 替代原Claude-Doc角色]
    TOKEN[📊 Token趋势监控 替代实时拦截]
    RETRY[🔄 单任务独立重试计数 → retry_stats.db]
    LOGS[📝 分级日志持久化 + GitHub归档]
  end
  
  DOC --> O
  TOKEN --> O
  RETRY --> JUDGE_INNER
  LOGS --> O
end
```

---

# 六、QQBot 指令模板 & 告警文案

| 指令内容 | 执行效果 |
|---|---|
| 开发 [项目名称]，需求：xxx | 启动指定项目，传入原始需求 |
| 强制完整流水线 | 跳过复杂度判断，强制走 🔴 复杂完整链路 |
| 快速开发模式 | 跳过复杂度判断，强制走 🟢 简单精简链路 |
| 暂停 | 暂停整个流水线所有任务 |
| 继续 | 恢复流水线执行（熔断状态需先【确认】） |
| 终止 | 终止当前项目所有任务，归档日志 |
| 进度 | 查询项目实时进度 |
| 日志 | 推送最近10条实时日志 |
| Token日志 | 推送最近10条Token消耗记录 + Token趋势 |
| 失败日志 | 推送最近10条失败/熔断日志 |
| 目录结构 | 推送当前项目标准化目录结构及各目录填充状态 |
| 确认 | 人工确认关键节点/熔断恢复 |
| 重试 [任务ID] | 手动触发失败任务重试 |

---

# 七、优化汇总：与 v1.0 方案的核心差异

| #   | 优化项      | v1.0 方案         | 优化方案                  | 预期收益                          |
| --- | -------- | --------------- | --------------------- | ----------------------------- |
| 1   | 文档管理     | Claude-Doc 独立角色 | OpenClaw 内置文档模块       | 减少1个角色，节省 30% 文档流转延迟          |
| 2   | 复杂度路由    | 所有需求走完整10角色链路   | 三分层路由（简单/中等/复杂）       | 60%+ 日常需求延迟降低 50%             |
| 3   | PE+PM 并行 | PE → PM 串行      | PM ∥ Arch 并行（复杂任务）    | 复杂任务前期延迟降低 20%                |
| 4   | 目录命名     | `.openclaw/`    | `._pipeline_private/` | 避免与 OpenClaw 系统目录冲突           |
| 5   | Token 管控 | 实时拦截            | 事后趋势预警                | 更务实，与 OpenClaw sub-agent 模型兼容 |
| 6   | 接口契约     | 文字描述            | OpenAPI 3.0 规范文件      | 机器可读，前后端统一解析，契约自动校验           |
| 7   | 角色总数     | 10              | 9                     | 精简 1 个，链路更短                   |
