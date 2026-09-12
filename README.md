# software-engineering-hub

软件工程技能中心 —— 一个基于 Claude Code skill 机制的**分层技能库**,服务软件工程全流程(需求 / 设计 / 编码 / 测试 / 交付)各类参与者。

> **安装**：把本仓库地址发给任何智能体，说一句“安装这个技能库”，它会自行克隆仓库并将 `skills/` 下的技能复制到其技能目录（如 `.claude/skills/`）并启用，无需其他人工步骤。
>
> **通用性说明**：本仓库是**技能开发库**（通用类型）——在这里开发、维护软件工程方向的技能，不绑定任何具体业务项目。仓库即技能的唯一源（master）：技能以标准 skill 目录结构（`SKILL.md` + `references/` + 可选 `scripts/`）组织在顶层 `skills/` 下，可被任何采用 Claude Code skill 机制的智能体/项目直接引用或复制使用；新技能开发也按同一结构在本仓库内进行，随 git 版本化。

## 是什么

一个 **skill 路由中心 + 垂直技能库**。主 skill `se-hub` 作为路由入口,判断当前所处工程阶段与角色,渐进披露对应的垂直技能文档。所有调用统一从 hub 进,由 hub 分派。

## 为什么这么设计

**解决技能爆炸问题。** 若每个能力都注册成独立的一等公民 skill,技能列表会随能力增加线性膨胀——每条 description 都进上下文,既占 token 又让选路变难。

本仓库采用 **hub 路由 + references 渐进披露**:

- 只有 `se-hub` 一条注册进技能列表。无论下挂多少垂直能力,对外始终一个入口,列表不膨胀。
- 垂直内容平时不在上下文,hub 判断出阶段后才 `Read` 进来,按需加载。一次会话通常只涉及一两个阶段,只付那部分的开销。

## 目录结构

```
skills/se-hub/
├── SKILL.md                              # 1级 · 路由入口(唯一注册技能)
└── references/
    ├── requirement/SKILL.md              # 2级 · 需求阶段
    ├── design/SKILL.md                   # 2级 · 设计阶段
    ├── coding/SKILL.md                   # 2级 · 编码阶段(子hub)
    │   └── references/
    │       ├── frontend/SKILL.md         # 3级 · 前端
    │       ├── backend/SKILL.md          # 3级 · 后端
    │       ├── devops/SKILL.md           # 3级 · DevOps
    │       ├── refactor/SKILL.md         # 3级 · 重构
    │       └── code-review/SKILL.md      # 3级 · 代码审查
    ├── integration/SKILL.md              # 2级 · 集成出包子hub(编码后、测试前)
    │   └── gitlab-release-train/         # 3级 · GitLab 发布分支治理(SKILL.md + scripts/)
    ├── testing/SKILL.md                  # 2级 · 测试阶段
    └── delivery/.gitkeep                 # 2级 · 交付阶段(项目经理交付管理,待填充)
```

> 阶段顺序对应工程流：需求 → 设计 → 编码 → **集成出包** → 测试 → 交付。集成出包阶段为子 hub 结构（与编码同构）：`integration/SKILL.md` 路由，下方 `gitlab-release-train/` 承载内网 GitLab 发布分支全流程（三分支模型、游离态扫描、闭环清理）。`gitlab-release-train` 原为独立技能，整体搬入，内容未改写。交付阶段定位为**项目经理视角的交付管理**（发布计划/上线协调/风险与回滚决策/发布清单/复盘），区别于集成出包的技术流程。

## 设计原则

- **级数由内容体积倒推**:阶段内容小就 2 级到底(如需求 / 交付);内容大、子域界限清楚的才拆 3 级(如编码)。不强求统一层级——混合结构是对的。
- **嵌套 `references/` 里的 `SKILL.md` 不单独注册**:只有顶层 `se-hub` 是注册技能,子 `SKILL.md` 是 hub 内部 `Read` 的文档载体。
- **3 级必须自己挣位置**:某级若不拆,父级 `SKILL.md` 会大到一次性加载不划算时才加层;否则是废层,徒增路由错误级联放大的风险。

## 状态

骨架已建。集成出包阶段已落地首个垂直子技能 `gitlab-release-train`（GitLab 发布分支治理，编码后、测试前的出包班车流程）；其余阶段 `SKILL.md` 内容待填充。
