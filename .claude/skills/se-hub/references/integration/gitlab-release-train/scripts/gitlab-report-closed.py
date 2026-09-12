#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gitlab-report-closed.py — 闭环(已收口)分支报告

按「三站标准」判定 feat/fix 分支是否走完完整生命周期:
  ①进过 cxy-dev(compare cxy-dev→分支 = 0 commits)
  ②进过当前 release(compare release→分支 = 0 commits,可选)
  ③已回流 cxy-master(compare cxy-master→分支 = 0 commits,或 merged 字段)
三站全经过 → 「可删」;只到 master 但没走 dev → 「⚠️ 异常保留」。

release 分支判定:已建回流 MR(或 compare cxy-master→release = 0)→ 「可删」。
切点违规(release 不是从 cxy-master 切)单独标注,不拦截判定。

只读扫描,不做任何删除/合并;删除动作请用 --delete 配合人工确认清单。

用法:
  # 账号密码(由 agent 从记忆注入,不写进本文件)
  GITLAB_USER=alice GITLAB_PASSWORD=**** python3 gitlab-report-closed.py --release release/26_0909
  python3 gitlab-report-closed.py --release release/26_0909 --out closed.md
  python3 gitlab-report-closed.py --project llm-workflow-service   # 调试单仓

参数:
  --release   当前班车分支(如 release/26_0909);不填则跳过站②(仅 dev+master 两站)
  --host      GitLab 地址(默认 http://172.16.168.245:28080)
  --project   只扫某个项目 path(调试用)
  --delete    真正删除「可删」分支(不可逆!需再带 --confirm)
  --out       输出 markdown 文件(不填打印 stdout)
  --verbose   打印每个分支判定过程到 stderr

前提:全链路必须 merge,不能 squash/cherry-pick(会重写 SHA 导致 compare 误判)。
"""
import sys
import json
import time
import argparse
import urllib.request
import urllib.error
import urllib.parse

import gitlab_auth  # 认证解析(账号密码)

DEFAULT_HOST = "http://172.16.168.245:28080"
DEV_BRANCH = "cxy-dev"
MASTER_BRANCH = "cxy-master"


class GitLab:
    def __init__(self, host, auth_headers):
        self.host = host.rstrip("/")
        self.auth_headers = auth_headers

    def _open(self, url, method="GET", retries=3):
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
        data = self._api(
            f"/projects/{pid}/repository/compare"
            f"?from={urllib.parse.quote(frm, safe='')}"
            f"&to={urllib.parse.quote(to, safe='')}"
        )
        if data is None:
            return None
        return data.get("commits") or []

    def has_open_mr(self, pid, branch):
        data = self._api(
            f"/projects/{pid}/merge_requests"
            f"?source_branch={urllib.parse.quote(branch, safe='')}"
            f"&state=opened&per_page=1"
        )
        return bool(data)

    def delete_branch(self, pid, branch):
        """GitLab CE 删除成功返回空 body(非错误);GET 复查 404 才算确认。"""
        url = (f"{self.host}/api/v4/projects/{pid}/repository/branches/"
               f"{urllib.parse.quote(branch, safe='')}")
        try:
            status, _ = self._open(url, method="DELETE")
            if status >= 400 and status != 404:
                return False, f"HTTP {status}"
            st2, _ = self._open(url)
            if st2 == 404:
                return True, "已删除(GET 404 确认)"
            return False, f"删除后 GET 仍返回 {st2}"
        except RuntimeError as e:
            return False, str(e)


def commit_info(b):
    c = b.get("commit", {}) or {}
    return c.get("authored_date", "")[:10], c.get("author_name", ""), c.get("short_id", "")


def scan(gl, release, project_filter, verbose=False):
    projects = gl.list_projects()
    repos = [p for p in projects
             if (not project_filter or p["path"] == project_filter)
             and p.get("default_branch") == MASTER_BRANCH]

    print(f"扫描范围:cxy-master 仓 {len(repos)} 个 | release={release or '(未指定,站②跳过)'}",
          file=sys.stderr)

    deletable, abnormal, release_ok, release_warn, errors = [], [], [], [], []

    for p in repos:
        pid, path = p["id"], p["path"]
        try:
            branches = gl.list_branches(pid)
        except Exception as e:
            errors.append(f"{path}: 拉分支失败 {e}")
            continue
        names = {b["name"] for b in branches}
        has_dev = DEV_BRANCH in names
        print(f"  [{path}]", file=sys.stderr)

        # ── feat/fix 三站判定 ──
        for b in branches:
            name = b["name"]
            if not (name.startswith("feat/") or name.startswith("fix/")):
                continue
            date, author, sha = commit_info(b)
            try:
                if p.get("default_branch") == MASTER_BRANCH and "merged" in b:
                    in_master = bool(b.get("merged", False))
                else:
                    in_master = len(gl.compare(pid, MASTER_BRANCH, name)) == 0
                if not in_master:
                    continue  # 未回流 master,不可能是闭环
                in_dev = (len(gl.compare(pid, DEV_BRANCH, name)) == 0) if has_dev else False
                in_release = None
                if release and release in names:
                    in_release = len(gl.compare(pid, release, name)) == 0

                if verbose:
                    print(f"    {name}: master=是 dev={in_dev} "
                          f"release={in_release}", file=sys.stderr)

                row = {"pid": pid, "project": path, "branch": name, "date": date,
                       "author": author, "sha": sha,
                       "in_dev": "是" if in_dev else "否",
                       "in_release": ("是" if in_release else "否")
                                      if in_release is not None else "-"}
                if in_dev and (in_release is None or in_release):
                    row["verdict"] = "✅ 可删"
                    deletable.append(row)
                elif not in_dev and (in_release is None or in_release):
                    row["verdict"] = "⚠️ 异常保留(进过 master/release 但没走 dev)"
                    abnormal.append(row)
                else:
                    row["verdict"] = "⚠️ 未到站(在 master 但 release 未含)"
                    abnormal.append(row)
            except Exception as e:
                errors.append(f"{path}/{name}: 判定失败 {e}")

        # ── release 分支判定 ──
        for b in branches:
            name = b["name"]
            if not name.startswith("release/"):
                continue
            date, author, sha = commit_info(b)
            try:
                in_master = len(gl.compare(pid, MASTER_BRANCH, name)) == 0
                row = {"pid": pid, "project": path, "branch": name, "date": date,
                       "author": author, "sha": sha}
                if in_master:
                    row["verdict"] = "✅ 可删(已回流 cxy-master)"
                    release_ok.append(row)
                else:
                    row["verdict"] = "❌ 未收口(未回流 cxy-master)"
                    release_warn.append(row)
            except Exception as e:
                errors.append(f"{path}/{name}: 判定失败 {e}")

    return deletable, abnormal, release_ok, release_warn, errors, len(repos)


def render(deletable, abnormal, release_ok, release_warn, n_repos, release, errors):
    L = ["# 闭环(已收口)分支报告", ""]
    L.append(f"扫描时间:{time.strftime('%Y-%m-%d %H:%M')}")
    L.append(f"当前 release:{release or '(未指定,站②跳过)'} | cxy-master 仓:{n_repos}")
    L.append("判定标准(feat/fix 三站全经过):①进过 cxy-dev ②进过当前 release ③已回流 cxy-master")
    L.append("")
    L.append(f"## ✅ feat/fix 可删({len(deletable)})")
    L.append("")
    L.append("| 项目 | 分支 | 在 dev? | 在 release? | 最新 commit | 提交人 |")
    L.append("|---|---|---|---|---|---|")
    for r in sorted(deletable, key=lambda x: (x["project"], x["branch"])):
        L.append(f"| {r['project']} | {r['branch']} | {r['in_dev']} | "
                 f"{r['in_release']} | {r['date']} {r['sha']} | {r['author']} |")
    L.append("")
    L.append(f"## ⚠️ feat/fix 异常/未到站(保留,不删)({len(abnormal)})")
    L.append("")
    L.append("| 项目 | 分支 | 在 dev? | 在 release? | 最新 commit | 提交人 | 判定 |")
    L.append("|---|---|---|---|---|---|---|")
    for r in sorted(abnormal, key=lambda x: (x["project"], x["branch"])):
        L.append(f"| {r['project']} | {r['branch']} | {r['in_dev']} | "
                 f"{r['in_release']} | {r['date']} {r['sha']} | {r['author']} | {r['verdict']} |")
    L.append("")
    L.append(f"## release 分支")
    L.append("")
    L.append(f"### ✅ 可删(已回流 cxy-master)({len(release_ok)})")
    L.append("")
    L.append("| 项目 | 分支 | 最新 commit | 提交人 |")
    L.append("|---|---|---|---|")
    for r in sorted(release_ok, key=lambda x: (x["project"], x["branch"])):
        L.append(f"| {r['project']} | {r['branch']} | {r['date']} {r['sha']} | {r['author']} |")
    L.append("")
    if release_warn:
        L.append(f"### ❌ 未收口(保留)({len(release_warn)})")
        L.append("")
        L.append("| 项目 | 分支 | 最新 commit | 提交人 |")
        L.append("|---|---|---|---|")
        for r in sorted(release_warn, key=lambda x: (x["project"], x["branch"])):
            L.append(f"| {r['project']} | {r['branch']} | {r['date']} {r['sha']} | {r['author']} |")
        L.append("")
    L.append(f"**合计**:feat/fix 可删 {len(deletable)} | 异常保留 {len(abnormal)} "
             f"| release 可删 {len(release_ok)} | release 未收口 {len(release_warn)}")
    if errors:
        L.append("")
        L.append(f"⚠️ 扫描错误 {len(errors)} 个(已跳过):")
        for e in errors:
            L.append(f"- {e}")
    return "\n".join(L)


def do_delete(gl, rows, label):
    deleted, failed = [], []
    print(f"开始删除{label} {len(rows)} 个…", file=sys.stderr)
    for r in rows:
        branch = r["branch"]
        if not (branch.startswith("feat/") or branch.startswith("fix/")
                or branch.startswith("release/")):
            failed.append((r, "非 feat/fix/release,拒绝删除"))
            continue
        if gl.has_open_mr(r["pid"], branch):
            failed.append((r, "有 open MR,跳过"))
            print(f"  ⚠️ 跳过(open MR):{r['project']}/{branch}", file=sys.stderr)
            continue
        ok, detail = gl.delete_branch(r["pid"], branch)
        if ok:
            deleted.append(r)
        else:
            failed.append((r, detail))
        print(f"  {'✅ 已删' if ok else '❌ 失败(' + str(detail) + ')'}:"
              f"{r['project']}/{branch}", file=sys.stderr)
    return deleted, failed


def main():
    ap = argparse.ArgumentParser(
        description="闭环(已收口)分支报告(默认只读;删除需 --delete --confirm)")
    ap.add_argument("--release", help="当前班车分支,如 release/26_0909(不填跳过站②)")
    ap.add_argument("--host", default=DEFAULT_HOST, help="GitLab 地址")
    ap.add_argument("--project", help="只扫某个项目 path(调试用)")
    ap.add_argument("--out", help="输出 markdown 文件(不填打印 stdout)")
    ap.add_argument("--delete", action="store_true", help="删除「可删」分支(不可逆!)")
    ap.add_argument("--confirm", action="store_true", help="删除二次确认开关")
    ap.add_argument("--user", help="GitLab 账号(用户名);也可由环境变量 GITLAB_USER 注入")
    ap.add_argument("--password", help="GitLab 密码;也可由环境变量 GITLAB_PASSWORD 注入")
    ap.add_argument("--verbose", action="store_true")
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
    deletable, abnormal, release_ok, release_warn, errors, n_repos = scan(
        gl, args.release, args.project, args.verbose)

    if args.delete:
        if not args.confirm:
            print("警告:--delete 不可逆。先跑一遍看清单,确认后加 --confirm 执行。",
                  file=sys.stderr)
            sys.exit(2)
        d1, f1 = do_delete(gl, deletable, "feat/fix 可删分支")
        d2, f2 = do_delete(gl, release_ok, "release 可删分支")
        print(f"✅ feat/fix 删 {len(d1)} 失败 {len(f1)} | "
              f"release 删 {len(d2)} 失败 {len(f2)}", file=sys.stderr)
        return

    report = render(deletable, abnormal, release_ok, release_warn,
                    n_repos, args.release, errors)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"✅ markdown 已写入:{args.out}", file=sys.stderr)
    else:
        print(report)


if __name__ == "__main__":
    main()
