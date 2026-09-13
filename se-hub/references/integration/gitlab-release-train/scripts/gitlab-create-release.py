#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gitlab-create-release.py — release train 发车:创建 release/xxx 分支

两步式(写操作必须人工确认卡点):
  第一步(只读):不带 --branches → 扫描候选分支,输出清单(stdout 按仓分组 + Excel 明细)。
    候选 = ① 活跃开发分支:未收口(master=否)且 ≤stale-days 的 开发中/待发/上线验证中
           ② 已在 cxy-dev 的 feat/fix:分支头 commit 已在 dev 里(时间老也列,标「老」)
    已收口(master=是)不列。
  第二步(写):--branches "repoA:feat/x,feat/y;repoB:fix/z" --confirm
    → 按仓去重(一仓一 release),基于该仓 cxy-master 创建 release/xxx。
    防呆:必须 --confirm;release 已存在跳过(幂等);逐仓创建、单仓失败不中断。

结果汇总只走 stdout(Excel 仅候选清单用)。

用法:
  # 第一步:出候选清单
  GITLAB_USER=alice GITLAB_PASSWORD=**** python3 gitlab-create-release.py \
      --release release/26_0915 --out-xlsx candidates.xlsx
  # 第二步:确认后创建
  python3 gitlab-create-release.py --release release/26_0915 \
      --branches "repoA:feat/x,feat/y;repoB:fix/z" --confirm

参数:
  --release     release 分支名(如 release/26_0915),必填
  --branches    确认的分支清单,格式 仓path:分支1,分支2;仓path2:分支3(不填=只扫描)
  --confirm     真正执行创建(写操作二次确认开关)
  --stale-days  活跃阈值天数(默认 28,双周发 × 2 周期)
  --host        GitLab 地址(默认 http://172.16.168.245:28080)
  --project     只扫某个项目 path(调试用)
  --out-xlsx    第一步候选清单 Excel 输出路径(需 openpyxl)
  --verbose     打印判定过程到 stderr

前提:复用游离态扫描判定,全链路必须 merge(不能 squash/cherry-pick)。
"""
import sys
import json
import time
import argparse
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from collections import Counter, OrderedDict

import gitlab_auth  # 认证解析(账号密码)

# Windows 控制台默认 GBK,统一转 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_HOST = "http://172.16.168.245:28080"

# ─────────────────────────── GitLab API ───────────────────────────

class GitLab:
    def __init__(self, host, auth_headers):
        self.host = host.rstrip("/")
        self.auth_headers = auth_headers

    def _open(self, url, method="GET", data=None, retries=3):
        """带重试的 urlopen。返回 (status, body_bytes)。"""
        for attempt in range(retries):
            req = urllib.request.Request(url, method=method,
                                         headers=dict(self.auth_headers),
                                         data=data)
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
        return list(self._paged("/groups/MaaS/projects?include_subgroups=true"))

    def list_branches(self, pid):
        return list(self._paged(f"/projects/{pid}/repository/branches"))

    def compare(self, pid, frm, to):
        """返回 to 有、from 没有的 commits。空=to 已在 from 里;None=不存在。"""
        data = self._api(
            f"/projects/{pid}/repository/compare"
            f"?from={urllib.parse.quote(frm, safe='')}"
            f"&to={urllib.parse.quote(to, safe='')}"
        )
        if data is None:
            return None
        return data.get("commits") or []

    def create_branch(self, pid, branch, ref):
        """创建分支。返回 (成功?, 详情)。已存在(400)视为跳过成功。"""
        url = (f"{self.host}/api/v4/projects/{pid}/repository/branches"
               f"?branch={urllib.parse.quote(branch, safe='')}"
               f"&ref={urllib.parse.quote(ref, safe='')}")
        try:
            status, body = self._open(url, method="POST")
            if status in (200, 201):
                return True, "created"
            if status == 400:  # GitLab: branch already exists
                return True, "已存在(跳过)"
            return False, f"HTTP {status}: {body[:120]!r}"
        except RuntimeError as e:
            return False, str(e)


# ─────────────────────────── 判定(复用游离态逻辑) ───────────────────────────

def days_since(date_str):
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - d).days)
    except Exception:
        return 0


# 活跃分类(未收口且时间近):开发中/待发/上线验证中
ACTIVE_CATS = ("开发中", "待发", "上线验证中")


def classify(in_master, in_dev, days_old, stale_days):
    """发车视角的简化分类(不看 release 层):收口/活跃/老。"""
    if in_master:
        return "收口"
    old = days_old > stale_days
    if in_dev:
        return "待发" if not old else "老(在dev)"
    return ("开发中" if not old else "老(未在dev)")


# ─────────────────────────── 扫描候选 ───────────────────────────

def scan_candidates(gl, stale_days, project_filter, verbose=False):
    """扫全部 cxy-master 仓,返回 (候选rows, n_repos, errors)。
    候选 = 未收口且(时间近 或 在 cxy-dev)。"""
    projects = gl.list_projects()
    repos = [p for p in projects
             if (not project_filter or p["path"] == project_filter)
             and p.get("default_branch") == "cxy-master"]

    print(f"扫描范围:cxy-master 仓 {len(repos)} 个 | 活跃阈值={stale_days}天",
          file=sys.stderr)

    rows, errors = [], []
    for p in repos:
        pid, path = p["id"], p["path"]
        try:
            branches = gl.list_branches(pid)
        except Exception as e:
            errors.append(f"{path}: 拉分支失败 {e}")
            continue

        has_dev = any(b["name"] == "cxy-dev" for b in branches)
        targets = [b for b in branches
                   if b["name"].startswith("feat/") or b["name"].startswith("fix/")]
        if not targets:
            continue
        print(f"  [{path}] {len(targets)} 个 feat/fix", file=sys.stderr)

        for b in targets:
            name = b["name"]
            commit = b.get("commit", {}) or {}
            date_str = commit.get("authored_date", "")
            try:
                in_master = bool(b.get("merged", False))
                if in_master:
                    continue  # 收口,不列
                in_dev = (len(gl.compare(pid, "cxy-dev", name)) == 0) if has_dev else False
                d_old = days_since(date_str)
                cat = classify(False, in_dev, d_old, stale_days)
                # 候选:时间近的活跃,或已在 dev(哪怕老)
                if not (cat in ACTIVE_CATS or in_dev):
                    if verbose:
                        print(f"    跳过(不活跃):{name} {cat}", file=sys.stderr)
                    continue
                if verbose:
                    print(f"    候选:{name} {cat} days={d_old}", file=sys.stderr)
                rows.append({
                    "project": path,
                    "branch": name,
                    "in_dev": "是" if in_dev else "否",
                    "date": date_str[:10],
                    "sha": commit.get("short_id", ""),
                    "author": commit.get("author_name", ""),
                    "cat": cat,
                })
            except Exception as e:
                errors.append(f"{path}/{name}: 判定失败 {e}")

    rows.sort(key=lambda r: (r["project"], r["branch"]))
    return rows, len(repos), errors


def render_candidates(rows, n_repos, release, stale_days, errors):
    """stdout 候选清单:按仓分组 + 去重预览(agent 直接读,转给用户确认)。"""
    L = []
    L.append(f"# release 发车候选清单(release={release})")
    L.append(f"扫描时间:{datetime.now().strftime('%Y-%m-%d %H:%M')} | "
             f"仓:{n_repos} | 活跃阈值:{stale_days}天 | 候选分支:{len(rows)} 条")
    L.append("")
    by_proj = OrderedDict()
    for r in rows:
        by_proj.setdefault(r["project"], []).append(r)

    for proj in sorted(by_proj):
        L.append(f"## {proj}")
        L.append("| 分支 | 在dev? | 分类 | 提交人 | 最新 commit |")
        L.append("|---|---|---|---|---|")
        for r in by_proj[proj]:
            commit_cell = f"{r['date']} {r['sha']}" if r["sha"] else r["date"]
            L.append(f"| {r['branch']} | {r['in_dev']} | {r['cat']} "
                     f"| {r['author']} | {commit_cell} |")
        L.append("")

    L.append(f"若全选,将创建 release 的仓({len(by_proj)} 个,已按仓去重):")
    L.append(", ".join(sorted(by_proj)))
    if errors:
        L.append("")
        L.append(f"⚠️ 扫描错误 {len(errors)} 个(已跳过):")
        for e in errors:
            L.append(f"- {e}")
    L.append("")
    L.append("⛔ 请确认哪些仓的哪些分支进本次 release,确认后执行第二步创建。")
    return "\n".join(L)


def render_candidates_xlsx(path, rows, errors):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    headers = ["项目", "分支", "在dev?", "分类", "最新 commit 时间", "commit", "提交人"]
    cat_fill = {"开发中": "E2EFDA", "待发": "DDEBF7",
                "上线验证中": "DDEBF7", "老(在dev)": "FFEB9C", "老(未在dev)": "FFEB9C"}

    wb = Workbook()
    ws = wb.active
    ws.title = "发车候选"
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
        ws.append([r["project"], r["branch"], r["in_dev"], r["cat"],
                   r["date"], r["sha"], r["author"]])
        row = ws.max_row
        fill = cat_fill.get(r["cat"])
        for c in range(1, len(headers) + 1):
            cell = ws.cell(row=row, column=c)
            cell.border = border
            if fill:
                cell.fill = PatternFill("solid", fgColor=fill)

    for i, w in enumerate([26, 32, 10, 14, 18, 12, 12], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"

    if errors:
        ws2 = wb.create_sheet("扫描错误")
        for e in errors:
            ws2.append([e])
        ws2.column_dimensions["A"].width = 80

    wb.save(path)


# ─────────────────────────── 创建 release ───────────────────────────

def parse_branches(spec):
    """'repoA:feat/x,feat/y;repoB:fix/z' → OrderedDict{repo: [branches]}"""
    result = OrderedDict()
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(f"--branches 格式错误(缺冒号):{part}")
        repo, brs = part.split(":", 1)
        repo = repo.strip()
        blist = [b.strip() for b in brs.split(",") if b.strip()]
        if not repo or not blist:
            raise ValueError(f"--branches 格式错误:{part}")
        result.setdefault(repo, [])
        result[repo].extend(blist)
    return result


def create_releases(gl, release, branches_spec, verbose=False):
    """按仓去重,逐仓基于 cxy-master 创建 release/xxx。返回 (created, skipped, failed)。"""
    plan = parse_branches(branches_spec)
    projects = {p["path"]: p for p in gl.list_projects()}

    created, skipped, failed = [], [], []
    for repo, brs in plan.items():
        p = projects.get(repo)
        if not p:
            failed.append((repo, f"项目不存在:{repo}"))
            continue
        pid = p["id"]
        ok, detail = gl.create_branch(pid, release, "cxy-master")
        if ok:
            if detail == "created":
                created.append((repo, brs))
                print(f"  ✅ {repo} → {release}(基于 cxy-master,来源分支:{','.join(brs)})",
                      file=sys.stderr)
            else:
                skipped.append((repo, detail))
                print(f"  ⏭️ {repo} → {release}({detail})", file=sys.stderr)
        else:
            failed.append((repo, detail))
            print(f"  ❌ {repo} → {detail}", file=sys.stderr)
    return created, skipped, failed


def print_result_summary(created, skipped, failed, release):
    """结果汇总:仅 stdout。"""
    L = []
    L.append(f"# release 创建结果({release})")
    L.append(f"✅ 创建成功 {len(created)} 仓:")
    for repo, brs in created:
        L.append(f"  {repo} → {release}")
    L.append(f"⏭️ 已存在跳过 {len(skipped)} 仓:")
    for repo, d in skipped:
        L.append(f"  {repo} → {release} ({d})")
    L.append(f"❌ 失败 {len(failed)} 仓:")
    for repo, d in failed:
        L.append(f"  {repo} → {d}")
    print("\n".join(L))


# ─────────────────────────── 入口 ───────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="release train 发车:扫描候选 → 人工确认 → 按仓去重创建 release/xxx")
    ap.add_argument("--release", required=True,
                    help="release 分支名,如 release/26_0915")
    ap.add_argument("--branches",
                    help="确认的分支清单 'repoA:feat/x,feat/y;repoB:fix/z'(不填=只扫描候选)")
    ap.add_argument("--confirm", action="store_true",
                    help="确认执行创建(写操作必须加)")
    ap.add_argument("--stale-days", type=int, default=28,
                    help="活跃阈值天数(默认28)")
    ap.add_argument("--host", default=DEFAULT_HOST, help="GitLab 地址")
    ap.add_argument("--project", help="只扫某个项目 path(调试用)")
    ap.add_argument("--out-xlsx", help="候选清单 Excel 输出路径(需 openpyxl)")
    ap.add_argument("--user", help="GitLab 账号;也可由环境变量 GITLAB_USER 注入")
    ap.add_argument("--password", help="GitLab 密码;也可由环境变量 GITLAB_PASSWORD 注入")
    ap.add_argument("--verbose", action="store_true", help="打印判定过程到 stderr")
    args = ap.parse_args()

    try:
        auth_headers, auth_method = gitlab_auth.resolve_from_env(
            args.host, user=args.user, password=args.password)
    except RuntimeError as e:
        print(f"错误:{e}\n  首次使用请由 agent 引导输入账号密码并存入记忆。",
              file=sys.stderr)
        sys.exit(1)

    print(f"[认证] 使用方式:{auth_method}", file=sys.stderr)
    gl = GitLab(args.host, auth_headers)

    # 第二步:创建(写操作)
    if args.branches:
        if not args.confirm:
            print("警告:创建 release 是写操作。请核对清单后加 --confirm 执行。",
                  file=sys.stderr)
            sys.exit(2)
        created, skipped, failed = create_releases(
            gl, args.release, args.branches, args.verbose)
        print_result_summary(created, skipped, failed, args.release)
        sys.exit(0 if not failed else 1)

    # 第一步:扫描候选(只读)
    rows, n_repos, errors = scan_candidates(
        gl, args.stale_days, args.project, args.verbose)

    if args.out_xlsx:
        render_candidates_xlsx(args.out_xlsx, rows, errors)
        print(f"✅ Excel 已写入:{args.out_xlsx}", file=sys.stderr)
    print()
    print(render_candidates(rows, n_repos, args.release, args.stale_days, errors))


if __name__ == "__main__":
    main()
