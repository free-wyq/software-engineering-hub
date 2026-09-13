#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gitlab-close-release.py — release 收口:release/xxx 上线后合并回 cxy-master

两步式(accept 合并必须人工授权,铁律4):
  第一步(只读):不带 --confirm → 扫描全仓 release→cxy-master 差异,出收口清单。
    每仓判定:release 分支存在? ahead 多少(compare master→release)?
    已有 open MR(release→master)? → stdout 清单 + Excel 明细(可选)。
    ahead=0 的仓已收口/无内容,跳过。
  第二步(写):--confirm → 对 ahead>0 的仓:
    ① 无 open MR 则建 MR(source=release, target=cxy-master, squash=false)
    ② 逐个 accept;405 是状态机窗口 → 回查 state 再判,不当失败
    ③ 回读校验 compare(master→release)=0 commits 才算收口干净
    结果汇总仅 stdout。

用法:
  # 第一步:出收口清单
  GITLAB_USER=alice GITLAB_PASSWORD=**** python3 gitlab-close-release.py \
      --release release/26_0915 --out-xlsx close-list.xlsx
  # 第二步:授权后执行收口
  python3 gitlab-close-release.py --release release/26_0915 --confirm

参数:
  --release     release 分支名(如 release/26_0915),必填
  --confirm     真正执行建 MR + accept + 校验(写操作必须加)
  --host        GitLab 地址(默认 http://172.16.168.245:28080)
  --project     只扫某个项目 path(调试用)
  --out-xlsx    第一步清单 Excel 输出路径(需 openpyxl)
  --user/--password  认证(也可环境变量 GITLAB_USER/GITLAB_PASSWORD)
  --verbose     打印判定过程到 stderr

约束(对齐 SKILL.md 铁律):
  - 合并一律 squash=false 标准 merge(保留原 SHA,否则游离态 compare 误判)
  - accept 前遍历全量仓实时 open MR,不信早前快照
  - 收口校验:compare?from=cxy-master&to=release = 0 commits
"""
import sys
import json
import time
import argparse
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from collections import OrderedDict

import gitlab_auth

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

    def _api(self, path, method="GET", data=None):
        url = f"{self.host}/api/v4{path}"
        status, body = self._open(url, method=method, data=data)
        if status == 404:
            return None
        if status >= 400:
            raise RuntimeError(f"API {url} → HTTP {status}: {body[:200]!r}")
        if not body:
            return None  # GitLab CE 删除等操作返回空 body
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
        """返回 to 有、from 没有的 commits。空=已收口;None=分支不存在。"""
        data = self._api(
            f"/projects/{pid}/repository/compare"
            f"?from={urllib.parse.quote(frm, safe='')}"
            f"&to={urllib.parse.quote(to, safe='')}"
        )
        if data is None:
            return None
        return data.get("commits") or []

    def list_open_mrs(self, pid, source_branch, target_branch):
        """指定 source→target 的 open MR(实时查,不信快照)。"""
        return list(self._paged(
            f"/projects/{pid}/merge_requests"
            f"?source_branch={urllib.parse.quote(source_branch, safe='')}"
            f"&target_branch={urllib.parse.quote(target_branch, safe='')}"
            f"&state=opened"))

    def create_mr(self, pid, source, target, title):
        """建 MR(squash=false 标准 merge)。返回 (mr dict|None, detail)。已存在时返回已有 MR。"""
        body = urllib.parse.urlencode({
            "source_branch": source,
            "target_branch": target,
            "title": title,
            "squash": "false",
            "remove_source_branch": "false",
        }).encode("utf-8")
        url = f"{self.host}/api/v4/projects/{pid}/merge_requests"
        try:
            status, resp = self._open(url, method="POST", data=body)
            if status in (200, 201):
                return json.loads(resp.decode()), "created"
            if status == 409:  # MR 已存在
                mrs = self.list_open_mrs(pid, source, target)
                if mrs:
                    return mrs[0], "已存在(复用)"
                return None, f"HTTP 409 但查不到 open MR"
            return None, f"HTTP {status}: {resp[:120]!r}"
        except RuntimeError as e:
            return None, str(e)

    def accept_mr(self, pid, iid):
        """accept MR。返回 (成功?, state, detail)。405=状态机窗口,回查 state 再判。"""
        url = f"{self.host}/api/v4/projects/{pid}/merge_requests/{iid}/merge"
        try:
            status, resp = self._open(url, method="PUT")
            if status in (200, 201):
                mr = json.loads(resp.decode())
                return mr.get("state") == "merged", mr.get("state"), "accepted"
            if status == 405:
                # 状态机窗口:回查实时 state,merged 即成功
                time.sleep(2)
                mr = self._api(f"/projects/{pid}/merge_requests/{iid}")
                state = (mr or {}).get("state", "?")
                return state == "merged", state, "405→回查state"
            return False, "?", f"HTTP {status}: {resp[:120]!r}"
        except RuntimeError as e:
            return False, "?", str(e)


# ─────────────────────────── 第一步:扫描收口清单 ───────────────────────────

def scan_close_list(gl, release, project_filter, verbose=False):
    """扫全部 cxy-master 仓,返回 (rows, n_repos, errors)。
    每仓:release 存在? ahead(master→release)? open MR?"""
    projects = gl.list_projects()
    repos = [p for p in projects
             if (not project_filter or p["path"] == project_filter)
             and p.get("default_branch") == "cxy-master"]

    print(f"扫描范围:cxy-master 仓 {len(repos)} 个 | release={release}",
          file=sys.stderr)

    rows, errors = [], []
    for p in repos:
        pid, path = p["id"], p["path"]
        try:
            branch_names = {b["name"] for b in gl.list_branches(pid)}
            if release not in branch_names:
                continue  # 没切这个 release 的仓,不参与本次收口
            commits = gl.compare(pid, "cxy-master", release)
            if commits is None:
                errors.append(f"{path}: compare 失败(cxy-master 不存在?)")
                continue
            ahead = len(commits)
            mrs = gl.list_open_mrs(pid, release, "cxy-master")
            mr_iid = str(mrs[0]["iid"]) if mrs else "-"
            if ahead == 0:
                status = "✅ 已收口(ahead=0)"
            elif mrs:
                status = "待 accept(有 open MR)"
            else:
                status = "待建 MR"
            if verbose:
                print(f"  [{path}] ahead={ahead} mr={mr_iid} → {status}",
                      file=sys.stderr)
            rows.append({
                "project": path,
                "ahead": ahead,
                "mr": mr_iid,
                "status": status,
                "first_commit": (commits[0]["short_id"] + " " +
                                 commits[0]["title"][:40]) if commits else "-",
            })
        except Exception as e:
            errors.append(f"{path}: 扫描失败 {e}")

    rows.sort(key=lambda r: (0 if r["ahead"] > 0 else 1, r["project"]))
    return rows, len(repos), errors


def render_close_list(rows, n_repos, release, errors):
    L = []
    L.append(f"# release 收口清单(release={release} → cxy-master)")
    L.append(f"扫描时间:{datetime.now().strftime('%Y-%m-%d %H:%M')} | "
             f"仓:{n_repos} | 参与本次 release:{len(rows)} 仓")
    todo = [r for r in rows if r["ahead"] > 0]
    L.append(f"待收口(ahead>0):{len(todo)} 仓 | 已收口:{len(rows) - len(todo)} 仓")
    L.append("")
    L.append("| 项目 | ahead | open MR | 状态 | 首个 commit |")
    L.append("|---|---|---|---|---|")
    for r in rows:
        L.append(f"| {r['project']} | {r['ahead']} | {r['mr']} "
                 f"| {r['status']} | {r['first_commit']} |")
    if errors:
        L.append("")
        L.append(f"⚠️ 扫描错误 {len(errors)} 个(已跳过):")
        for e in errors:
            L.append(f"- {e}")
    L.append("")
    L.append("⛔ accept 合并必须人工授权。确认清单后加 --confirm 执行收口。")
    return "\n".join(L)


def render_close_xlsx(path, rows, errors):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    headers = ["项目", "ahead", "open MR", "状态", "首个 commit"]
    wb = Workbook()
    ws = wb.active
    ws.title = "收口清单"
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
        ws.append([r["project"], r["ahead"], r["mr"], r["status"], r["first_commit"]])
        row = ws.max_row
        fill = "E2EFDA" if r["ahead"] > 0 else None
        for c in range(1, len(headers) + 1):
            cell = ws.cell(row=row, column=c)
            cell.border = border
            if fill:
                cell.fill = PatternFill("solid", fgColor=fill)

    for i, w in enumerate([26, 10, 12, 22, 60], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"

    if errors:
        ws2 = wb.create_sheet("扫描错误")
        for e in errors:
            ws2.append([e])
        ws2.column_dimensions["A"].width = 80

    wb.save(path)


# ─────────────────────────── 第二步:执行收口 ───────────────────────────

def execute_close(gl, release, verbose=False):
    """对 ahead>0 的仓:建 MR(无则建)→ accept → 回读校验 compare=0。
    返回 (merged, already, failed)。"""
    rows, n_repos, errors = scan_close_list(gl, release, None, verbose)
    todo = [r for r in rows if r["ahead"] > 0]

    print(f"待收口 {len(todo)} 仓,开始执行(建 MR → accept → 校验)…", file=sys.stderr)

    merged, already, failed = [], [], []
    projects = {p["path"]: p for p in gl.list_projects()}

    for r in todo:
        path = r["project"]
        p = projects.get(path)
        if not p:
            failed.append((path, "项目不存在"))
            continue
        pid = p["id"]
        try:
            # ① open MR?无则建(squash=false)
            mrs = gl.list_open_mrs(pid, release, "cxy-master")
            if mrs:
                mr, how = mrs[0], "已有"
            else:
                mr, how = gl.create_mr(
                    pid, release, "cxy-master",
                    f"收口:{release} → cxy-master")
                if mr is None:
                    failed.append((path, f"建 MR 失败:{how}"))
                    continue
            iid = mr["iid"]
            # ② accept(405 回查 state)
            ok, state, detail = gl.accept_mr(pid, iid)
            if not ok:
                failed.append((path, f"accept 失败(iid={iid}, state={state}, {detail})"))
                continue
            # ③ 回读校验:compare(master→release)=0
            commits = gl.compare(pid, "cxy-master", release)
            if commits is None or len(commits) > 0:
                failed.append((path, f"收口校验失败:compare 仍有 {len(commits or [])} commits"))
                continue
            if how == "已有":
                already.append(path)
                print(f"  ✅ {path}:MR !{iid} accept 成功,compare=0(复用已有 MR)",
                      file=sys.stderr)
            else:
                merged.append(path)
                print(f"  ✅ {path}:MR !{iid} 建单+accept 成功,compare=0",
                      file=sys.stderr)
        except Exception as e:
            failed.append((path, str(e)))

    return merged, already, failed, len(todo)


def print_close_result(merged, already, failed, release):
    L = []
    L.append(f"# release 收口结果({release} → cxy-master)")
    L.append(f"✅ 收口成功 {len(merged)} 仓(新建 MR):")
    for p in merged:
        L.append(f"  {p}")
    if already:
        L.append(f"✅ 收口成功 {len(already)} 仓(复用已有 MR):")
        for p in already:
            L.append(f"  {p}")
    L.append(f"❌ 失败 {len(failed)} 仓:")
    for p, d in failed:
        L.append(f"  {p} → {d}")
    L.append("")
    L.append("后续:收口后的 release 分支可用 gitlab-report-closed.py 清理。")
    print("\n".join(L))


# ─────────────────────────── 入口 ───────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="release 收口:release/xxx 上线后合并回 cxy-master(建 MR→accept→校验)")
    ap.add_argument("--release", required=True,
                    help="release 分支名,如 release/26_0915")
    ap.add_argument("--confirm", action="store_true",
                    help="确认执行建 MR + accept + 校验(写操作必须加)")
    ap.add_argument("--host", default=DEFAULT_HOST, help="GitLab 地址")
    ap.add_argument("--project", help="只扫某个项目 path(调试用)")
    ap.add_argument("--out-xlsx", help="收口清单 Excel 输出路径(需 openpyxl)")
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

    # 第二步:执行收口(写操作)
    if args.confirm:
        merged, already, failed, todo_n = execute_close(gl, args.release, args.verbose)
        print_close_result(merged, already, failed, args.release)
        sys.exit(0 if not failed else 1)

    # 第一步:扫描收口清单(只读)
    rows, n_repos, errors = scan_close_list(
        gl, args.release, args.project, args.verbose)
    if args.out_xlsx:
        render_close_xlsx(args.out_xlsx, rows, errors)
        print(f"✅ Excel 已写入:{args.out_xlsx}", file=sys.stderr)
    print()
    print(render_close_list(rows, n_repos, args.release, errors))


if __name__ == "__main__":
    main()
