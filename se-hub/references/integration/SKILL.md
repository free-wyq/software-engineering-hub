---
name: integration
description: 集成出包阶段子 hub（编码之后、测试之前）。覆盖集成验证分支、提测合并、发布分支切制与出包、发布分支治理。当前已内置 gitlab-release-train(GitLab 发布分支全流程)。
---

# 集成出包阶段 (integration)

本目录是 `se-hub` 的**集成/出包子 hub**（与 `coding` 同构：子 hub + 下方 `references/` 再分层）。所处阶段：**编码完成之后、测试验证之前**——把已完成的 feat/fix 集成起来、切出 release 出包送测；上线后的收口回流与分支清理也在此治理。

`se-hub` 判定请求处于本阶段后，先 Read 本文件，再按下方路由下钻到具体子技能。

## 路由表

| 场景 | 关键词信号 | 下钻 |
|---|---|---|
| GitLab 发布分支治理 | 发版/切分支/release 分支/提测合并/出包/收口/清理分支/游离态/班车 | `references/gitlab-release-train/SKILL.md` |
| 通用集成（待补充） | 合并策略/集成节奏/版本号/构建出包 | （骨架待填充，暂用通用工程能力回答） |

## 集成说明

- `gitlab-release-train` 原为一个独立技能，已**整体搬入**本阶段下（`references/gitlab-release-train/`），其 SKILL.md 与 `scripts/`（三个纯标准库 Python 脚本）保持原样，未做改写。
- 该子技能面向**内网 GitLab CE** 的三分支模型（`cxy-master` / `cxy-dev` / `release/YY_MMDD`），含游离态扫描、闭环清理与铁律约束；凭证（账号密码）只在 agent 记忆中，不落技能目录。运行方式见其 SKILL.md 的「〇、凭证引导」。
- 对外仍只有一个注册技能 `se-hub`，本目录为内部渐进披露内容，符合项目 all-in-one 设计。

## 边界

- 本 hub **不干活**：不调 GitLab API、不建合并。只判断 + 路由到子技能。
- 子技能内容**不内联**：用到才 Read，避免一次性灌入上下文。
