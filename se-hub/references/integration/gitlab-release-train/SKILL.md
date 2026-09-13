---
name: gitlab-release-train
description: 内网 GitLab MaaS 发布流程(release/YY_MMDD)全链路规范——创建发布分支、提测合并、dev/master 不相交核验、收口回流、失效分支清理。当用户提到发版/切分支/release 分支/合并/收口/清理分支时使用。
---

# GitLab 发布分支全流程规范

内网 GitLab CE 16.9.1,`http://172.16.168.245:28080`。认证统一用**账号密码**:环境变量 `GITLAB_USER` + `GITLAB_PASSWORD`,脚本先试 OAuth 密码授权拿 Bearer,失败退回 HTTP Basic。

**凭证获取与存储铁律(见下方「〇、凭证引导」)**:账号密码**只能存于 agent 记忆**,**严禁写入本技能目录任何文件**(SKILL.md / scripts/ / 模板)。项目清单、权限约束见项目 CLAUDE.md 与记忆(gitlab-project-groups / gitlab-constraints-no-delete-no-merge)。

## 〇、凭证引导(首次运行必须,之后从记忆读取)

本技能**不持有任何账号密码**。运行前必须先解决认证凭证,流程如下:

### 1. 判断记忆中是否已有凭证
- 检查 agent 记忆中是否存在键 `gitlab-credentials`(记录 `user` 与 `password`)。
- **已有** → 直接跳到第 3 步,从记忆读出后注入运行,**不要再次向用户索要**。
- **没有** → 执行第 2 步(首次引导)。

### 2. 首次运行:向用户索取并存入记忆(仅此一次)
- 用提问/对话明确索取两项:**GitLab 账号(用户名)** 与 **GitLab 密码**。
- 取得后**立即**写入 agent 记忆,键名建议 `gitlab-credentials`,内容形如:
  `user=<用户名>; password=<密码>`(仅此两条,不要存 host 以外的多余信息)。
- ⚠️ **严禁**把账号密码写进本技能目录任何文件(SKILL.md、scripts/ 下脚本、模板 docx 等)。记忆是唯一存储位置。

### 3. 后续每次运行:从记忆读取并注入脚本
- 从记忆读出 `user` / `password`,运行脚本时以**环境变量**注入,例如:
  ```bash
  GITLAB_USER=<记忆中的user> GITLAB_PASSWORD=<记忆中的password> \
    python3 gitlab-scan-youli.py --release release/YY_MMDD
  ```
- 也可用脚本参数 `--user` / `--password`(等效于环境变量,仍从记忆取值,不要硬编码)。
- 脚本 stderr 首行会打印 `[认证] 使用方式:OAuth-Bearer / HTTP-Basic`,据此确认认证生效。

### 4. 凭证失效处理
- 若运行报 401/403,提示用户密码可能已改;从记忆清除 `gitlab-credentials` 并重新走第 2 步引导。

## 一、分支模型(三分支模型)

```
cxy-master(生产基线,唯一上游是 release)
├── release/YY_MMDD(发布分支:验证→生产→合回 master 收口)
│     ↑ 合入发布
└── feat/fix(功能/修复分支,从 cxy-master 切)
│     ↓ 合入验证
└── cxy-dev(集成验证,不回流 master)

cxy-dev 即 dev,cxy-master 即 master;二者永远平行,永不相交
(dev 上的内容只随 feat/fix 走 release 进生产,不直接回流 master)
```

feat/fix 完整生命周期:**cxy-master 切出 → 合 cxy-dev 验证 → 合 release 发布 → release 合回 cxy-master**。

## 二、铁律(违反即停,先报告用户)

1. **dev / 生产分支(release)/ master 永不相交**:release 上相对 cxy-dev 的差异**只允许是 merge commit**。若发现非 merge 的直接 commit(绕过 dev 直接提交在 release),必须停下来报告用户,不得擅自合。
2. **release 必须从 cxy-master 切**。从上一个 release 分支接着切,会把该分支未回流 master 的历史内容一并带入(skillhub-service 0909 事故)。
3. **合并一律 squash=false 标准合并**,保留原 SHA——squash/cherry-pick 重写 SHA 会让游离态 compare 全线误判。
4. **权限口径:只建不删不合**。批量建 MR/建分支可直接做;**accept 合并、删除分支必须用户逐次授权**,授权前先给清单让用户审。
5. 批量 accept 前遍历**全量 34 仓**的实时 open MR,不信早前快照(0909 漏 a2a-service !8 教训)。

## 三、操作流程

### 1. 创建发布分支(切 release)—— 用 `gitlab-create-release.py` 两步式

- **第一步(只读扫描)**:`python3 gitlab-create-release.py --release release/YY_MMDD --out-xlsx candidates.xlsx`
  扫全部 cxy-master 仓,候选 = ①未收口且 ≤28 天的 开发中/待发/上线验证中 ②已在 cxy-dev 的 feat/fix(老的标「老」)。stdout 按仓分组输出清单 + 去重预览,Excel 落明细。
- **⛔ 人工确认卡点**:候选清单必须给用户确认哪些仓的哪些分支进本次 release,脚本/agent 不得自作主张全建。
- **第二步(写操作)**:`python3 gitlab-create-release.py --release release/YY_MMDD --branches "repoA:feat/x,feat/y;repoB:fix/z" --confirm`
  按仓去重(一仓一 release),基于该仓 `cxy-master` 创建;必须 `--confirm`;已存在跳过(幂等);结果汇总仅 stdout。
- 各参与仓从 `cxy-master` 切 `release/YY_MMDD`(POST `/projects/:id/repository/branches?branch=release/YY_MMDD&ref=cxy-master`)。
- 用户提出分析分支更新时:跑 `python3 gitlab-scan-youli.py --release release/YY_MMDD` 得游离态分类,「待发」即候选发布内容。

### 2. 合入发布分支(feat/fix → release)

- 对每个待发分支建 MR:`source=feat/xxx,target=release/YY_MMDD,squash=false`。
- 用户确认后批量 accept(全量仓实时列表),逐个回读 `state=merged` 确认;偶发 405 是状态机窗口,回查 state 而非当失败。

### 3. 合入前核验(铁律 1)

对每个 release 仓比对 `compare?from=cxy-dev&to=release/YY_MMDD`:差异中**非 merge commit 数必须为 0**。有直接 commit → 报告用户定夺(0909 案例用户可豁免,但必须先暴露)。

### 4. 收口(release → cxy-master)—— 用 `gitlab-close-release.py` 两步式

- **第一步(只读扫描)**:`python3 gitlab-close-release.py --release release/YY_MMDD --out-xlsx close-list.xlsx`
  扫全部参与仓:release 存在? ahead 多少(compare master→release)? 已有 open MR? → stdout 清单(ahead=0 已收口跳过)。
- **⛔ 人工授权卡点**:accept 合并必须用户确认清单后授权,不得自动执行。
- **第二步(写操作)**:`python3 gitlab-close-release.py --release release/YY_MMDD --confirm`
  对 ahead>0 的仓逐个:①无 open MR 则建 MR(source=release, target=cxy-master, squash=false)→ ②accept(405 是状态机窗口,回查 state 再判)→ ③回读校验 compare(master→release)=0 才算收口干净。结果汇总仅 stdout。
- release 上线后,建 MR `release/YY_MMDD → cxy-master`(compare ahead=0 的仓跳过,broken_status 无意义)。
- 用户授权后批量 accept,回读并校验 `compare?from=cxy-master&to=release/YY_MMDD` = **0 commits** 即收口干净。

### 5. 清理已收口分支(须用户确认清单后授权)

- **feat/fix 收口标准(三站全经过)**:①在 cxy-dev(compare dev→分支=0)→ ②进过 release → ③在 cxy-master(compare master→分支=0)。**三站全部核验通过才可删**;只到 master 但没走 dev 的是 ⚠️异常,不算收口,保留并报告。
- **release 收口标准**:release 合回 cxy-master 且 compare(master→release)=0。收口后的 release 分支即可删。
- 删除:DELETE `/projects/:id/repository/branches/:name`(**URL 编码** `feat%2Fxxx`、`release%2F26_0909`)。GitLab CE 删除成功返回空 body,可能报 json 解析错——**空 body = 成功**,必须 GET 复查 404 确认。
- 非本流程仓(skillhub 走 main 系、callcenter/enterprise/紫菁元不走 cxy 流程)**不纳入默认范围**,列出单独请示。

## 四、工具(scripts/ 目录,随技能分发)

两个脚本均为纯标准库(python3,无第三方依赖),跨环境通用:GitLab 地址用 `--host` 覆盖,认证读环境变量 `GITLAB_USER`+`GITLAB_PASSWORD`(账号密码,脚本自动走 OAuth/HTTP Basic),也可传 `--user/--password` 参数;仓库范围自动发现(扫 `MaaS` 组下 `default_branch=cxy-master` 的仓,别的 GitLab 实例换组名即可)。

### 报告 1:游离态分支汇总报告 — `gitlab-scan-youli.py`

- **用途**:发版前分析「哪些分支能上本次发布」+ 日常治理「哪些分支该清」。
- **逻辑**:对每个 feat/fix 分支做三布尔判定(在 master?/在 release?/在 dev?)+ commit 时间新旧,分类为 收口/上线验证中/release卡住/待发/废弃候选/开发中/僵尸/异常,输出 9 列单表。
- **用法**:`--release release/YY_MMDD` 报告;`--out-xlsx` 出 Excel;`--stale-days 28` 调废弃阈值。

### 报告 2:闭环(已收口)分支报告 — `gitlab-report-closed.py`

- **用途**:清理前的待删清单。只报「可安全删除」的分支,异常分支单独列出保留。
- **逻辑**:
  - feat/fix 按**三站标准**判定:①进过 cxy-dev ②进过当前 release(不填 `--release` 则跳过此站)③已回流 cxy-master。三站全经过 → ✅可删;只到 master 没走 dev → ⚠️异常保留。
  - release 分支:已回流 cxy-master(compare=0)→ ✅可删;未回流 → ❌未收口保留。
- **用法**:默认只读出报告;`--delete --confirm` 才执行删除(删除走「空 body=成功 + GET 404 复查」,自动跳过有 open MR 的分支)。

### 工具 3:发车创建 release — `gitlab-create-release.py`

- **用途**:发车第一步「切 release 分支」。扫描候选 → 人工确认 → 按仓去重创建。
- **两步式**:
  1. 只扫描(不带 `--branches`):输出候选清单(stdout 按仓分组 + `--out-xlsx` Excel 明细)。
  2. 创建(`--branches "repo:br1,br2;..." --confirm`):按仓去重,每仓基于 `cxy-master` 建 1 个 `release/xxx`;已存在跳过(幂等);结果汇总仅 stdout。
- **防呆**:写操作必须 `--confirm`;候选必须经用户确认后才组装 `--branches`。

### 工具 4:收口合并 — `gitlab-close-release.py`

- **用途**:release 上线后合并回 cxy-master(收口)。
- **两步式**:
  1. 只扫描(不带 `--confirm`):出收口清单(stdout + `--out-xlsx` Excel),每仓 ahead/open MR/状态。
  2. 执行(`--confirm`):对 ahead>0 的仓建 MR(squash=false)→ accept(405 回查 state)→ 回读校验 compare=0;结果汇总仅 stdout。
- **防呆**:accept 必须人工授权(`--confirm` 卡点);open MR 实时查不信快照;校验不过记失败。

### 报告输出铁律

单表原样输出(项目|分支|描述|三布尔|commit|提交人|分类),禁止拆表/删列/改名;优先直接引用脚本 stdout。

> ⚠️ 两个脚本的删除功能均不可逆;正式操作前先跑只读模式出清单给用户确认。

## 五、历史档案

release/26_0625、26_0812、26_0909 三次发布的完整记录与教训见记忆 `gitlab-release-iterations.md`。
