#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gitlab-scan-youli.py — 游离态分支治理扫描

判定核心:feat/fix 分支的头 commit 在不在 cxy-master / cxy-dev / release。
在 master=是 即收口(不列入报告);否则按矩阵分类,输出单表(按治理优先级排序)。
走 GitLab compare API(只读),不删不合,对齐约束「只建不删不合」
(见 ~/.claude/.../memory/gitlab-constraints-no-delete-no-merge.md)。

方案全文见 memory:gitlab-youli-branch-governance.md

用法:
  # 账号密码(由 agent 从记忆注入,不写进本文件)
  GITLAB_USER=alice GITLAB_PASSWORD=**** python3 gitlab-scan-youli.py --release release/26_0811
  python3 gitlab-scan-youli.py --release release/26_0811 --stale-days 28 --out-xlsx report.xlsx
  python3 gitlab-scan-youli.py      # 不填 release → 退化为只看 master/dev 两层(省略 release 列)
  python3 gitlab-scan-youli.py --project llm-workflow-service --release release/26_0811  # 调试用,只扫一个仓

参数:
  --release       当前上线分支(如 release/26_0811),动态填入,不自动扫
  --stale-days     废弃阈值天数(默认 28,双周发 × 2 周期);超过算"老"
  --host           GitLab 地址(默认 http://172.16.168.245:28080)
  --project        只扫某个项目(path,调试用),不填扫全部
  --out-xlsx       输出 Excel 报告路径(必填,需 openpyxl);stdout 另打精简摘要
  --verbose        打印每个分支的判定过程到 stderr

前提(误判风险):全链路必须 merge,不能有 squash/cherry-pick。
  squash 会重写 commit SHA,原 feat 的 SHA 在 master 对不上 → 把已收口误判成游离。
  dev 合入已确认是 merge;release→master 是否 merge 待人工确认(命门)。
"""
import sys
import json
import time
import argparse
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from collections import Counter

import gitlab_auth  # 认证解析(账号密码)

# Windows 控制台默认 GBK,打不出 🔴/⚠️ emoji,统一转 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_HOST = "http://172.16.168.245:28080"

# ─────────────────────────── GitLab API ───────────────────────────

class GitLab:
    def __init__(self, host, auth_headers):
        self.host = host.rstrip("/")
        self.auth_headers = auth_headers

    def _open(self, url, method="GET", retries=3):
        """带重试的 urlopen。超时/连不上/5xx 会重试;4xx 直接返回不重试。
        返回 (status, body_bytes)。抛 RuntimeError 表示重试耗尽仍连不上。
        """
        body = None
        for attempt in range(retries):
            req = urllib.request.Request(url, method=method,
                                         headers=dict(self.auth_headers))
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    return e.code, e.read()
                body = e.read()
                time.sleep(2 * (attempt + 1))
            except (TimeoutError, urllib.error.URLError, OSError) as e:
                body = None
                if attempt == retries - 1:
                    raise RuntimeError(f"连不上 {url}: {e}")
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"连不上 {url}: 重试耗尽")

    def _api(self, path):
        url = f"{self.host}/api/v4{path}"
        status, body = self._open(url)
        if status == 404:
            return None
        if status >= 400:
            raise RuntimeError(f"API {url} → HTTP {status}: {body[:200]!r}")
        return json.loads(body.decode())

    def _paged(self, path):
        """自动翻页拉全量。"""
        page = 1
        while True:
            sep = "&" if "?" in path else "?"
            data = self._api(f"{path}{sep}per_page=100&page={page}")
            if not data:
                break
            for item in data:
                yield item
            if len(data) < 100:
                break
            page += 1

    def list_projects(self):
        # MaaS 组含子组(include_subgroups),一页取不完自动翻页
        return list(self._paged("/groups/MaaS/projects?include_subgroups=true"))

    def list_branches(self, pid):
        return list(self._paged(f"/projects/{pid}/repository/branches"))

    def compare(self, pid, frm, to):
        """返回 to 有、from 没有的 commits 列表。
        空 list   = to 完全在 from 里(收口/已合并);
        None      = 分支不存在(调用方应先判存在性)。
        """
        data = self._api(
            f"/projects/{pid}/repository/compare"
            f"?from={urllib.parse.quote(frm, safe='')}"
            f"&to={urllib.parse.quote(to, safe='')}"
        )
        if data is None:
            return None
        return data.get("commits") or []

    def has_open_mr(self, pid, branch):
        """分支作为源分支是否有 open 的 MR。有 → 删分支会让 MR 挂,跳过。"""
        data = self._api(
            f"/projects/{pid}/merge_requests"
            f"?source_branch={urllib.parse.quote(branch, safe='')}"
            f"&state=opened&per_page=1"
        )
        return bool(data)

    def delete_branch(self, pid, branch):
        """删除分支(不可逆)。返回 (成功?, 详情)。404=分支已不存在(视为已删)。"""
        url = (f"{self.host}/api/v4/projects/{pid}/repository/branches/"
               f"{urllib.parse.quote(branch, safe='')}")
        try:
            status, _ = self._open(url, method="DELETE")
            if status == 404:
                return True, "已不存在(视为已删)"
            if status >= 400:
                return False, f"HTTP {status}"
            return True, status
        except RuntimeError as e:
            return False, str(e)


# ─────────────────────────── 判定与分类 ───────────────────────────

# 矩阵(在 master=否 前提下,布尔全展开):
#   release=是 & dev=否           → ⚠️ 异常(跳过 dev 验证,流程违规)
#   release=是 & dev=是  & 时间老 → ⚠️ release 卡住(release 没回流 master)
#   release=是 & dev=是  & 时间近 → 上线验证中
#   release=否 & dev=是  & 时间老 → 🔴 废弃候选
#   release=否 & dev=是  & 时间近 → 待发
#   release=否 & dev=否  & 时间老 → 🔴 僵尸
#   release=否 & dev=否  & 时间近 → 开发中
def classify(in_master, in_release, in_dev, days_old, stale_days):
    if in_master:
        return "收口"  # 不列入报告
    old = days_old > stale_days
    if in_release and not in_dev:
        return "⚠️ 异常"
    if in_release and in_dev:
        return "⚠️ release 卡住" if old else "上线验证中"
    if in_dev:
        return "🔴 废弃候选" if old else "待发"
    return "🔴 僵尸" if old else "开发中"


# 排序优先级:🔴 该清 → ⚠️ 该查 → 正常流转
PRIORITY = {
    "🔴 废弃候选": 0,
    "🔴 僵尸": 1,
    "⚠️ 异常": 2,
    "⚠️ release 卡住": 3,
    "上线验证中": 4,
    "待发": 5,
    "开发中": 6,
}

STAT_ORDER = ["🔴 废弃候选", "🔴 僵尸", "⚠️ 异常", "⚠️ release 卡住",
              "上线验证中", "待发", "开发中"]


def days_since(date_str):
    """ISO/YYYY-MM-DD → 距今天数。解析失败按 0(新)处理,不报错。"""
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - d).days)
    except Exception:
        return 0


def branch_desc(gl, pid, name, master="cxy-master", max_titles=4, max_len=120):
    """分支描述:取分支相对 master 独有的 commit 标题,去重、截断。
    描述「这个分支主要做了什么」。compare 已算过时若缓存无妨,只读。
    """
    try:
        commits = gl.compare(pid, master, name)
        if not commits:
            return ""
        titles = []
        seen = set()
        for c in commits:
            t = (c.get("title") or "").strip()
            if not t or t in seen:
                continue
            # 跳过 merge commit 的噪音标题
            if t.startswith("Merge "):
                continue
            seen.add(t)
            titles.append(t)
            if len(titles) >= max_titles:
                break
        desc = "；".join(titles)
        return desc[:max_len] if len(desc) > max_len else desc
    except Exception:
        return ""


# ─────────────────────────── 扫描主流程 ───────────────────────────

def scan(gl, release, stale_days, project_filter, verbose=False):
    projects = gl.list_projects()
    # 筛 cxy-master 仓:default_branch==cxy-master 即纳入(免费,无需逐个 has_branch)。
    # main/master 仓(紫菁元+tools)不走此流程,自动排除。
    repos = [p for p in projects
             if (not project_filter or p["path"] == project_filter)
             and p.get("default_branch") == "cxy-master"]

    print(f"扫描范围:cxy-master 仓 {len(repos)} 个 | "
          f"当前 release={release or '(未指定)'} | 废弃阈值={stale_days}天",
          file=sys.stderr)

    rows = []
    collected = 0      # 已收口(master=是)计数,不列入报告
    collected_rows = []  # 已收口分支明细(dry-run 待删清单用)
    errors = []

    for p in repos:
        pid = p["id"]
        path = p["path"]
        default_branch = p.get("default_branch", "")
        try:
            branches = gl.list_branches(pid)
        except Exception as e:
            errors.append(f"{path}: 拉分支失败 {e}")
            continue

        branch_names = {b["name"] for b in branches}
        has_dev = "cxy-dev" in branch_names
        has_release = bool(release) and (release in branch_names)

        targets = [b for b in branches
                   if b["name"].startswith("feat/") or b["name"].startswith("fix/")]
        if not targets:
            continue
        print(f"  [{path}] {len(targets)} 个 feat/fix", file=sys.stderr)

        for b in targets:
            name = b["name"]
            commit = b.get("commit", {}) or {}
            date_str = commit.get("authored_date", "")
            author = commit.get("author_name", "")
            short_sha = commit.get("short_id", "")

            try:
                # 在 master?:default_branch==cxy-master 时用 merged 字段(免费且权威);
                # 否则回退 compare。
                if default_branch == "cxy-master":
                    in_master = bool(b.get("merged", False))
                else:
                    in_master = len(gl.compare(pid, "cxy-master", name)) == 0

                if in_master:
                    collected += 1
                    collected_rows.append({
                        "pid": pid,
                        "project": path,
                        "branch": name,
                        "date": date_str[:10],
                        "sha": short_sha,
                        "author": author,
                    })
                    continue  # 收口,不列入

                in_dev = (len(gl.compare(pid, "cxy-dev", name)) == 0) if has_dev else False
                if release and has_release:
                    in_release = len(gl.compare(pid, release, name)) == 0
                else:
                    in_release = False

                d_old = days_since(date_str)
                cat = classify(in_master, in_release, in_dev, d_old, stale_days)

                # 描述:分支相对 master 的独有 commit 标题(只读,拉一次)
                desc = branch_desc(gl, pid, name)

                if verbose:
                    print(f"    {name}: master={in_master} dev={in_dev} "
                          f"release={in_release} days={d_old} → {cat} | {desc[:40]}", file=sys.stderr)

                rows.append({
                    "project": path,
                    "branch": name,
                    "desc": desc,
                    "in_master": "否",   # 收口的已 continue,留下来的都是否
                    "in_release": "是" if in_release else "否",
                    "in_dev": "是" if in_dev else "否",
                    "date": date_str[:10],
                    "sha": short_sha,
                    "author": author,
                    "cat": cat,
                })
            except Exception as e:
                errors.append(f"{path}/{name}: 判定失败 {e}")

    rows.sort(key=lambda r: PRIORITY.get(r["cat"], 99))
    collected_rows.sort(key=lambda r: (r["project"], r["branch"]))
    return rows, collected, len(repos), errors, collected_rows


# ─────────────────────────── 报告渲染 ───────────────────────────

def render_summary(rows, collected, n_repos, release, stale_days, errors):
    """stdout 精简摘要:统计行 + 待发/异常类清单(agent 直接读,不用解析 Excel)。"""
    stats = Counter(r["cat"] for r in rows)
    total_branches = len(rows) + collected

    L = []
    L.append("# 游离态分支扫描摘要")
    L.append(f"扫描时间:{datetime.now().strftime('%Y-%m-%d %H:%M')} | "
             f"release={release or '(未指定)'} | 阈值={stale_days}天 | 仓:{n_repos}")
    L.append(f"feat/fix 总数:{total_branches} | 已收口(不列入):{collected} | 游离态:{len(rows)}")
    stat_str = " | ".join(f"{k} {stats.get(k, 0)}" for k in STAT_ORDER if stats.get(k, 0))
    L.append(f"统计:{stat_str or '无游离态分支'}")

    # 重点清单:该处理的(红/黄) + 待发(本次上线候选)
    focus = [r for r in rows if r["cat"] in
             ("🔴 废弃候选", "🔴 僵尸", "⚠️ 异常", "⚠️ release 卡住", "待发")]
    if focus:
        L.append("")
        L.append("## 重点关注(废弃/僵尸/异常/release卡住/待发)")
        L.append("| 项目 | 分支 | 分类 | 提交人 | 最新 commit |")
        L.append("|---|---|---|---|---|")
        for r in focus:
            commit_cell = f"{r['date']} {r['sha']}" if r['sha'] else r['date']
            L.append(f"| {r['project']} | {r['branch']} | {r['cat']} "
                     f"| {r['author']} | {commit_cell} |")

    if errors:
        L.append("")
        L.append(f"⚠️ 扫描错误 {len(errors)} 个(已跳过):")
        for e in errors:
            L.append(f"- {e}")

    return "\n".join(L)


def render_xlsx(path, rows, collected, n_repos, release, stale_days, errors):
    """Excel 版报告:Sheet「游离态分支」= 全行单表;Sheet「统计」= 分类计数。
    样式与之前 D 盘版一致:表头蓝底白字、冻结首行、自动筛选、按分类着色。
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    headers = ["项目", "分支", "描述", "在 dev?", "在 master?", "在 release?",
               "最新 commit 时间", "commit", "提交人", "分类"]
    if not release:
        headers.remove("在 release?")

    cat_fill = {
        "🔴 废弃候选": "FFC7CE",
        "🔴 僵尸": "FFC7CE",
        "⚠️ 异常": "FFEB9C",
        "⚠️ release 卡住": "FFEB9C",
        "上线验证中": "DDEBF7",
        "待发": "E2EFDA",
        "开发中": "E2EFDA",
    }

    wb = Workbook()
    ws = wb.active
    ws.title = "游离态分支"

    hdr_fill = PatternFill("solid", fgColor="4472C4")
    hdr_font = Font(bold=True, color="FFFFFF")
    thin = Side(style="thin", color="D9D9D9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws.append(headers)
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border

    for r in rows:
        commit_cell = f"{r['date']} {r['sha']}" if r['sha'] else r['date']
        line = [r["project"], r["branch"], r["desc"] or "-", r["in_dev"]]
        if release:
            line.append(r["in_release"])
        line += [r["in_master"], commit_cell, r["sha"], r["author"], r["cat"]]
        ws.append(line)
        row = ws.max_row
        fill = cat_fill.get(r["cat"])
        for c in range(1, len(headers) + 1):
            cell = ws.cell(row=row, column=c)
            cell.border = border
            if fill:
                cell.fill = PatternFill("solid", fgColor=fill)

    # 列宽
    widths = [26, 32, 46, 10, 10, 10, 18, 12, 12, 14]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"

    # Sheet 统计
    stats = Counter(r["cat"] for r in rows)
    ws2 = wb.create_sheet("统计")
    ws2.append(["分类", "数量"])
    for k, v in stats.items():
        ws2.append([k, v])
    ws2.append(["游离态合计", len(rows)])
    ws2.append(["已收口(master,不列入)", collected])
    ws2.append(["cxy-master 仓", n_repos])
    if errors:
        ws2.append([])
        ws2.append(["⚠️ 扫描错误(已跳过)"])
        for e in errors:
            ws2.append([e])
    ws2.column_dimensions["A"].width = 40
    ws2.column_dimensions["B"].width = 12
    for c in (1, 2):
        ws2.cell(row=1, column=c).fill = hdr_fill
        ws2.cell(row=1, column=c).font = hdr_font

    wb.save(path)


def delete_merged(gl, rows, verbose=False):
    """删除已收口分支(仅 feat/fix)。三道保险:
    1. 只动 feat/fix(保护分支天然排除);
    2. 有 open MR 指向的跳过(删了 MR 会挂);
    3. 逐个删、逐个记,失败不中断。
    返回 (deleted, skipped_mr, failed)。
    """
    deleted = []
    skipped_mr = []
    failed = []
    for r in rows:
        branch = r["branch"]
        if not (branch.startswith("feat/") or branch.startswith("fix/")):
            failed.append((r, "非 feat/fix,跳过(不该出现)"))
            continue
        if gl.has_open_mr(r["pid"], branch):
            skipped_mr.append(r)
            print(f"  跳过(有 open MR):{r['project']}/{branch}", file=sys.stderr)
            continue
        ok, detail = gl.delete_branch(r["pid"], branch)
        if ok:
            deleted.append(r)
            print(f"  已删除:{r['project']}/{branch}", file=sys.stderr)
        else:
            failed.append((r, detail))
            print(f"  ❌ 删除失败({detail}):{r['project']}/{branch}", file=sys.stderr)
    return deleted, skipped_mr, failed


# ─────────────────────────── 入口 ───────────────────────────

def main():
    ap = argparse.ArgumentParser(description="游离态分支治理扫描(只读,不删不合)")
    ap.add_argument("--release", help="当前上线分支,如 release/26_0811(不填则省略 release 列)")
    ap.add_argument("--stale-days", type=int, default=28,
                    help="废弃阈值天数(默认28,双周发×2周期);超过算老")
    ap.add_argument("--host", default=DEFAULT_HOST, help="GitLab 地址")
    ap.add_argument("--project", help="只扫某个项目 path(调试用)")
    ap.add_argument("--out-xlsx", required=True,
                    help="输出 Excel 报告路径(需 openpyxl);stdout 另打精简摘要")
    ap.add_argument("--list-merged", action="store_true",
                    help="dry-run:列出已收口(merged=true)分支清单,便于人工确认后清理")
    ap.add_argument("--delete-merged", action="store_true",
                    help="真正删除已收口分支(不可逆!默认跳过有 open MR 的)。用前先 --list-merged 确认")
    ap.add_argument("--confirm-delete", action="store_true",
                    help="确认执行删除(--delete-merged 的二次确认开关)")
    ap.add_argument("--user", help="GitLab 账号(用户名);也可由环境变量 GITLAB_USER 注入")
    ap.add_argument("--password", help="GitLab 密码;也可由环境变量 GITLAB_PASSWORD 注入")
    ap.add_argument("--verbose", action="store_true", help="打印每个分支判定过程到 stderr")
    args = ap.parse_args()

    try:
        auth_headers, auth_method = gitlab_auth.resolve_from_env(
            args.host, user=args.user, password=args.password)
    except RuntimeError as e:
        print(f"错误:{e}\n"
              f"  首次使用请由 agent 引导输入账号密码并存入记忆。",
              file=sys.stderr)
        sys.exit(1)

    print(f"[认证] 使用方式:{auth_method}", file=sys.stderr)
    gl = GitLab(args.host, auth_headers)
    rows, collected, n_repos, errors, collected_rows = scan(
        gl, args.release, args.stale_days, args.project, args.verbose)

    if args.list_merged:
        # dry-run:输出待删清单(只读,不删)。按项目聚合,方便人工核对。
        L = ["# 已收口分支(merged=true)待删清单", ""]
        L.append(f"判定:分支相对 cxy-master 无独有 commit(merged 字段,GitLab 服务端算)。")
        L.append(f"共 {collected} 个,按项目聚合如下。确认后执行删除(不可逆)。")
        L.append("")
        by_proj = {}
        for r in collected_rows:
            by_proj.setdefault(r["project"], []).append(r)
        for proj in sorted(by_proj):
            brs = by_proj[proj]
            L.append(f"### {proj} ({len(brs)})")
            L.append("| 分支 | 最新 commit | 提交人 |")
            L.append("|---|---|---|")
            for b in sorted(brs, key=lambda x: x["branch"]):
                L.append(f"| {b['branch']} | {b['date']} {b['sha']} | {b['author']} |")
            L.append("")
        L.append(f"**合计:{collected} 个已收口分支**")
        print("\n".join(L))
        return

    if args.delete_merged:
        # 确认守卫:必须显式二次确认,防止手滑直接删。
        if not args.confirm_delete:
            print("警告:--delete-merged 是不可逆批量删除。请先 --list-merged 看清单,"
                  "确认后再加 --confirm-delete 执行。", file=sys.stderr)
            sys.exit(2)
        print(f"开始删除已收口分支,共 {len(collected_rows)} 个(跳过有 open MR 的)…",
              file=sys.stderr)
        deleted, skipped_mr, failed = delete_merged(gl, collected_rows, args.verbose)
        print(file=sys.stderr)
        print(f"✅ 已删除 {len(deleted)} | ⚠️ 跳过(open MR) {len(skipped_mr)} "
              f"| ❌ 失败 {len(failed)}", file=sys.stderr)
        for r in skipped_mr:
            print(f"  跳过:{r['project']}/{r['branch']} (open MR)", file=sys.stderr)
        for r, d in failed:
            print(f"  失败:{r['project']}/{r['branch']} → {d}", file=sys.stderr)
        return

    render_xlsx(args.out_xlsx, rows, collected, n_repos,
                args.release, args.stale_days, errors)
    print(f"✅ Excel 已写入:{args.out_xlsx}", file=sys.stderr)
    print()
    print(render_summary(rows, collected, n_repos,
                         args.release, args.stale_days, errors))


if __name__ == "__main__":
    main()
