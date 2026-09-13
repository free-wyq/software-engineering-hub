#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_all_scenarios.py — gitlab-release-train 全场景测试(Mock GitLab)

内置有状态 Mock GitLab(HTTP 服务,线程内启动),模拟:
  OAuth 认证 / 项目列表 / 分支列表 / compare / 建分支 / 删分支 / MR 建单与 accept
按 SKILL.md 场景 A~E 顺序执行 5 个脚本,断言输出与状态变化,含防呆路径。

Mock 数据(3 仓):
  repoA(cxy-master): feat/x(待发) feat/old(废弃候选) fix/y(开发中)
                     feat/closed(已收口) release/26_0915(ahead=2)
  repoB(cxy-master): feat/z(开发中)
  repoC(main):       自动排除(非 cxy-master 仓)

运行:python3 tests/test_all_scenarios.py   (需 openpyxl)
"""
import os
import sys
import json
import socket
import tempfile
import threading
import subprocess
import urllib.request
import urllib.parse
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TODAY = "2026-09-12"
OLD = "2026-07-01"


# ─────────────────────────── Mock GitLab 状态 ───────────────────────────

def fresh_state():
    return {
        "repos": [
            {"id": 1, "path": "repoA", "default_branch": "cxy-master", "next_iid": 1,
             "branches": {
                 "cxy-master": {"commits": ["m1", "m2"]},
                 "cxy-dev": {"commits": ["m1", "m2", "c1", "c2"]},
                 "feat/x": {"commits": ["m1", "m2", "c1"], "date": TODAY, "author": "alice"},
                 "feat/old": {"commits": ["m1", "m2", "c2"], "date": OLD, "author": "bob"},
                 "fix/y": {"commits": ["m1", "m2", "c3"], "date": TODAY, "author": "carol"},
                 "feat/closed": {"commits": ["m1", "m2"], "date": OLD, "author": "dave"},
                 "release/26_0915": {"commits": ["m1", "m2", "r1"],
                                     "date": TODAY, "author": "ops"},
             },
             "mrs": []},
            {"id": 2, "path": "repoB", "default_branch": "cxy-master", "next_iid": 1,
             "branches": {
                 "cxy-master": {"commits": ["m1", "m2"]},
                 "cxy-dev": {"commits": ["m1", "m2"]},
                 "feat/z": {"commits": ["m1", "m2", "c9"], "date": TODAY, "author": "eve"},
             },
             "mrs": []},
            {"id": 3, "path": "repoC", "default_branch": "main", "next_iid": 1,
             "branches": {
                 "main": {"commits": ["x1"]},
                 "feat/ignore": {"commits": ["x1", "x2"], "date": TODAY, "author": "sam"},
             },
             "mrs": []},
        ]
    }


STATE = fresh_state()


def find_repo(pid):
    for r in STATE["repos"]:
        if r["id"] == pid:
            return r
    return None


def branch_json(repo, name):
    b = repo["branches"][name]
    master = set(repo["branches"][repo["default_branch"]]["commits"])
    sha = abs(hash(name)) % 10**8
    return {
        "name": name,
        "merged": set(b["commits"]) <= master,
        "commit": {"authored_date": b.get("date", TODAY) + "T10:00:00Z",
                   "author_name": b.get("author", "ops"),
                   "short_id": f"{sha:08x}"},
    }


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默访问日志
        pass

    def _send(self, status, payload=None):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _authed(self):
        return bool(self.headers.get("Authorization"))

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return {k: v[0] for k, v in
                    urllib.parse.parse_qs(raw.decode("utf-8")).items()}
        except Exception:
            return {}

    def do_POST(self):
        if not self._authed() and not self.path.startswith("/oauth"):
            return self._send(401, {"message": "401 Unauthorized"})
        parsed = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

        if parsed.path == "/oauth/token":
            form = self._read_body()
            if form.get("username") and form.get("password"):
                return self._send(200, {"access_token": "mock-token"})
            return self._send(401, {"message": "bad credentials"})

        parts = parsed.path.strip("/").split("/")
        if parts[:2] == ["api", "v4"]:
            parts = parts[2:]
        # /projects/:id/repository/branches
        if len(parts) == 4 and parts[0] == "projects" and parts[2] == "repository" \
                and parts[3] == "branches":
            repo = find_repo(int(parts[1]))
            name, ref = q.get("branch"), q.get("ref")
            if not repo or not name or ref not in repo["branches"]:
                return self._send(400, {"message": "invalid ref"})
            if name in repo["branches"]:
                return self._send(400, {"message": "Branch already exists"})
            repo["branches"][name] = {
                "commits": list(repo["branches"][ref]["commits"]),
                "date": TODAY, "author": "ops"}
            return self._send(201, branch_json(repo, name))

        # /projects/:id/merge_requests
        if len(parts) == 3 and parts[0] == "projects" and parts[2] == "merge_requests":
            repo = find_repo(int(parts[1]))
            form = self._read_body()
            src, tgt = form.get("source_branch"), form.get("target_branch")
            for mr in repo["mrs"]:
                if mr["source_branch"] == src and mr["state"] == "opened":
                    return self._send(409, {"message": "MR already exists"})
            mr = {"iid": repo["next_iid"], "state": "opened",
                  "source_branch": src, "target_branch": tgt}
            repo["next_iid"] += 1
            repo["mrs"].append(mr)
            return self._send(201, dict(mr))
        return self._send(404, {"message": "not found"})

    def do_PUT(self):
        if not self._authed():
            return self._send(401, {"message": "401 Unauthorized"})
        parts = urllib.parse.urlparse(self.path).path.strip("/").split("/")
        if parts[:2] == ["api", "v4"]:
            parts = parts[2:]
        # /projects/:id/merge_requests/:iid/merge
        if len(parts) == 5 and parts[0] == "projects" and parts[2] == "merge_requests" \
                and parts[4] == "merge":
            repo = find_repo(int(parts[1]))
            iid = int(parts[3])
            mr = next((m for m in repo["mrs"] if m["iid"] == iid), None)
            if not mr:
                return self._send(404, {"message": "MR not found"})
            src, tgt = mr["source_branch"], mr["target_branch"]
            if src in repo["branches"] and tgt in repo["branches"]:
                merged = set(repo["branches"][tgt]["commits"]) | \
                         set(repo["branches"][src]["commits"])
                repo["branches"][tgt]["commits"] = sorted(merged)
            mr["state"] = "merged"
            return self._send(200, dict(mr))
        return self._send(404, {"message": "not found"})

    def do_DELETE(self):
        if not self._authed():
            return self._send(401, {"message": "401 Unauthorized"})
        parts = urllib.parse.urlparse(self.path).path.strip("/").split("/")
        if parts[:2] == ["api", "v4"]:
            parts = parts[2:]
        # /projects/:id/repository/branches/<name>(name 可能含 feat%2Fx)
        if len(parts) == 5 and parts[0] == "projects" and parts[2] == "repository" \
                and parts[3] == "branches":
            repo = find_repo(int(parts[1]))
            name = urllib.parse.unquote(parts[4])
            if not repo or name not in repo["branches"]:
                return self._send(404, {"message": "Branch Not Found"})
            del repo["branches"][name]
            return self._send(204, None)  # GitLab CE: 空 body
        return self._send(404, {"message": "not found"})

    def do_GET(self):
        if not self._authed():
            return self._send(401, {"message": "401 Unauthorized"})
        parsed = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        page = int(q.get("page", 1))

        if parsed.path == "/api/v4/groups/MaaS/projects":
            if page > 1:
                return self._send(200, [])
            return self._send(200, [
                {"id": r["id"], "path": r["path"],
                 "default_branch": r["default_branch"]} for r in STATE["repos"]])

        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 4 and parts[0] == "api" and parts[1] == "v4" \
                and parts[2] == "projects":
            repo = find_repo(int(parts[3])) if len(parts) > 4 else None
            rest = parts[4:]

            # /projects/:id/repository/branches(列表)
            if rest == ["repository", "branches"]:
                if not repo:
                    return self._send(404, {"message": "project not found"})
                if page > 1:
                    return self._send(200, [])
                return self._send(200, [branch_json(repo, n)
                                        for n in repo["branches"]])

            # /projects/:id/repository/branches/<name>(单个,删除复查用)
            if len(rest) == 3 and rest[:2] == ["repository", "branches"]:
                name = urllib.parse.unquote(rest[2])
                if repo and name in repo["branches"]:
                    return self._send(200, branch_json(repo, name))
                return self._send(404, {"message": "Branch Not Found"})

            # /projects/:id/repository/compare
            if rest == ["repository", "compare"]:
                frm, to = q.get("from"), q.get("to")
                if not repo or frm not in repo["branches"] or to not in repo["branches"]:
                    return self._send(404, {"message": "Branch Not Found"})
                extra = set(repo["branches"][to]["commits"]) - \
                        set(repo["branches"][frm]["commits"])
                return self._send(200, {"commits": [
                    {"id": c, "short_id": c[:8], "title": f"commit {c}"}
                    for c in sorted(extra)]})

            # /projects/:id/merge_requests
            if rest == ["merge_requests"]:
                if not repo:
                    return self._send(404, {"message": "project not found"})
                mrs = [m for m in repo["mrs"]
                       if (not q.get("state") or m["state"] == q["state"])
                       and (not q.get("source_branch")
                            or m["source_branch"] == q["source_branch"])
                       and (not q.get("target_branch")
                            or m["target_branch"] == q["target_branch"])]
                if page > 1:
                    return self._send(200, [])
                return self._send(200, mrs)

            # /projects/:id/merge_requests/:iid(回查 state)
            if len(rest) == 2 and rest[0] == "merge_requests":
                mr = next((m for m in repo["mrs"] if m["iid"] == int(rest[1])), None)
                if not mr:
                    return self._send(404, {"message": "MR not found"})
                return self._send(200, dict(mr))
        return self._send(404, {"message": "not found"})


# ─────────────────────────── 测试框架 ───────────────────────────

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" | {detail}" if detail and not cond else ""))


def run_script(script, args, env_extra=None, cwd=HERE):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    cmd = [sys.executable, os.path.join(SCRIPTS, script)] + args
    p = subprocess.run(cmd, capture_output=True, env=env, cwd=cwd, timeout=120)
    return p.returncode, p.stdout.decode("utf-8", "replace"), \
        p.stderr.decode("utf-8", "replace")


def api_get(port, path):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v4{path}",
        headers={"Authorization": "Bearer mock-token"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None


def branch_exists(port, pid, name):
    st, _ = api_get(port, f"/projects/{pid}/repository/branches/"
                          f"{urllib.parse.quote(name, safe='')}")
    return st == 200


def main():
    # 启动 Mock 服务
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = ThreadingHTTPServer(("127.0.0.1", port), MockHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{port}"
    env = {"GITLAB_USER": "tester", "GITLAB_PASSWORD": "pw", "TEST_HOST": host}
    tmp = tempfile.mkdtemp(prefix="grt-test-")
    REL = "release/26_0915"

    print(f"Mock GitLab: {host} | 临时目录: {tmp}\n")

    # ── S0 认证防呆 ──
    print("S0 认证防呆(无凭证应退出 1)")
    rc, out, err = run_script("gitlab-scan-youli.py",
                              ["--release", REL, "--out-xlsx",
                               os.path.join(tmp, "x.xlsx"), "--host", host],
                              env_extra={"GITLAB_USER": "", "GITLAB_PASSWORD": ""})
    check("S0 无凭证退出码=1", rc == 1, f"rc={rc}")
    check("S0 报错含「缺少认证凭证」", "缺少认证凭证" in err)

    # ── S1 场景E:游离态扫描 ──
    print("S1 场景E 游离态扫描(只读报告)")
    rc, out, err = run_script("gitlab-scan-youli.py",
                              ["--release", REL, "--out-xlsx",
                               os.path.join(tmp, "youli.xlsx"), "--host", host], env)
    check("S1 退出码=0", rc == 0, f"rc={rc} err={err[-200:]}")
    check("S1 feat/x 分类=待发", "feat/x" in out and "待发" in out)
    # stdout 摘要只列红/黄类+待发,「开发中」需从 Excel 验证
    from openpyxl import load_workbook
    wb = load_workbook(os.path.join(tmp, "youli.xlsx"))
    ws = wb["游离态分支"]
    rows = [[c.value for c in r] for r in ws.iter_rows()]
    check("S1 fix/y 分类=开发中(Excel)",
          any(r[1] == "fix/y" and r[-1] == "开发中" for r in rows))
    check("S1 feat/old 分类=废弃候选", "feat/old" in out and "废弃候选" in out)
    check("S1 已收口 feat/closed 不列入", "feat/closed" not in out)
    check("S1 repoC(main) 自动排除", "repoC" not in out)
    check("S1 Excel 已生成", os.path.exists(os.path.join(tmp, "youli.xlsx")))

    print("S1b 场景E --list-merged 待删清单")
    rc, out, err = run_script("gitlab-scan-youli.py",
                              ["--release", REL, "--out-xlsx",
                               os.path.join(tmp, "x2.xlsx"),
                               "--list-merged", "--host", host], env)
    check("S1b 清单含 feat/closed", rc == 0 and "feat/closed" in out)

    # ── S2 场景A:创建发布分支 ──
    print("S2 场景A 创建发布分支 第一步(候选扫描)")
    rc, out, err = run_script("gitlab-create-release.py",
                              ["--release", REL, "--out-xlsx",
                               os.path.join(tmp, "cand.xlsx"), "--host", host], env)
    check("S2 退出码=0", rc == 0, f"rc={rc} err={err[-200:]}")
    check("S2 候选含 feat/x(待发)", "feat/x" in out)
    check("S2 候选含 fix/y(开发中)", "fix/y" in out)
    check("S2 候选含 feat/old 标「老(在dev)」", "feat/old" in out and "老(在dev)" in out)
    check("S2 repoB feat/z 入候选", "feat/z" in out)
    check("S2 去重预览含 repoA/repoB", "repoA" in out and "repoB" in out)
    check("S2 已收口 feat/closed 不在候选", "feat/closed" not in out)
    check("S2 提示人工确认", "请确认" in out)

    print("S2b 场景A 第二步防呆(无 --confirm)")
    rc, out, err = run_script("gitlab-create-release.py",
                              ["--release", REL, "--branches", "repoB:feat/z",
                               "--host", host], env)
    check("S2b 无 --confirm 退出码=2", rc == 2, f"rc={rc}")
    check("S2b repoB 未建 release", not branch_exists(port, 2, REL))

    print("S2c 场景A 第二步执行创建")
    rc, out, err = run_script("gitlab-create-release.py",
                              ["--release", REL,
                               "--branches", "repoA:feat/x,fix/y;repoB:feat/z",
                               "--confirm", "--host", host], env)
    check("S2c 退出码=0", rc == 0, f"rc={rc} err={err[-200:]}")
    check("S2c repoA 已存在跳过", "已存在" in out and "repoA" in out)
    check("S2c repoB 创建成功", "创建成功" in out)
    check("S2c repoB release 分支已存在(API 复核)",
          branch_exists(port, 2, REL))

    # ── S3 场景C:收口 ──
    print("S3 场景C 收口 第一步(清单扫描)")
    rc, out, err = run_script("gitlab-close-release.py",
                              ["--release", REL, "--out-xlsx",
                               os.path.join(tmp, "close.xlsx"), "--host", host], env)
    check("S3 退出码=0", rc == 0, f"rc={rc} err={err[-200:]}")
    check("S3 repoA ahead=2 待建 MR", "repoA" in out and "待建 MR" in out)
    check("S3 repoB ahead=0 已收口", "已收口" in out)

    print("S3b 场景C 第二步执行收口(建 MR→accept→校验)")
    rc, out, err = run_script("gitlab-close-release.py",
                              ["--release", REL, "--confirm", "--host", host], env)
    check("S3b 退出码=0", rc == 0, f"rc={rc} err={err[-300:]}")
    check("S3b repoA 收口成功", "收口成功 1 仓" in out and "repoA" in out, out[-200:])
    st, data = api_get(port, f"/projects/1/repository/compare"
                             f"?from=cxy-master&to={urllib.parse.quote(REL, safe='')}")
    check("S3b 回读校验 compare(master→release)=0",
          st == 200 and data is not None and len(data.get("commits", [])) == 0,
          f"commits={data and len(data.get('commits', []))}")

    # ── S4 场景D:清理 ──
    print("S4 场景D 收口报告(只读)")
    rc, out, err = run_script("gitlab-report-closed.py",
                              ["--release", REL, "--host", host], env)
    check("S4 退出码=0", rc == 0, f"rc={rc} err={err[-200:]}")
    check("S4 feat/closed 可删", "feat/closed" in out and "可删" in out)
    check("S4 feat/x 未回流 master,不进报告", "feat/x" not in out)
    check("S4 release/26_0915 可删(已回流)", "release/26_0915" in out)

    print("S4b 场景D 删除防呆(无 --confirm)")
    rc, out, err = run_script("gitlab-report-closed.py",
                              ["--release", REL, "--delete", "--host", host], env)
    check("S4b 无 --confirm 退出码=2", rc == 2, f"rc={rc}")

    print("S4c 场景D 删除(open MR 防呆)")
    # 给 feat/closed 挂一个 open MR → 应跳过
    req = urllib.request.Request(
        f"{host}/api/v4/projects/1/merge_requests", method="POST",
        headers={"Authorization": "Bearer mock-token",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data=urllib.parse.urlencode(
            {"source_branch": "feat/closed", "target_branch": "cxy-master",
             "title": "t"}).encode())
    urllib.request.urlopen(req, timeout=10).read()
    rc, out, err = run_script("gitlab-report-closed.py",
                              ["--release", REL, "--delete", "--confirm",
                               "--host", host], env)
    check("S4c 退出码=0", rc == 0, f"rc={rc} err={err[-300:]}")
    check("S4c feat/closed 因 open MR 跳过", "跳过" in err and "feat/closed" in err)
    check("S4c release/26_0915 已删除(API 复核 404)",
          not branch_exists(port, 1, REL))
    check("S4c feat/x 仍存在(API 复核 200)",
          branch_exists(port, 1, "feat/x"))
    check("S4c feat/closed 仍存在(API 复核 200)",
          branch_exists(port, 1, "feat/closed"))

    # ── S5 场景E:delete-merged 防呆与执行 ──
    print("S5 场景E --delete-merged 防呆(无 --confirm-delete)")
    rc, out, err = run_script("gitlab-scan-youli.py",
                              ["--release", REL, "--out-xlsx",
                               os.path.join(tmp, "x3.xlsx"),
                               "--delete-merged", "--host", host], env)
    check("S5 无确认退出码=2", rc == 2, f"rc={rc}")

    print("S5b 场景E --delete-merged 执行(跳过 open MR)")
    rc, out, err = run_script("gitlab-scan-youli.py",
                              ["--release", REL, "--out-xlsx",
                               os.path.join(tmp, "x4.xlsx"), "--delete-merged",
                               "--confirm-delete", "--host", host], env)
    check("S5b 退出码=0", rc == 0, f"rc={rc} err={err[-300:]}")
    check("S5b 跳过 open MR 1 个(feat/closed)",
          "跳过(open MR) 1" in err, err[-200:])

    # ── 汇总 ──
    server.shutdown()
    print(f"\n{'='*50}")
    print(f"结果: PASS {len(PASS)} | FAIL {len(FAIL)}")
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print(f"  - {f}")
        sys.exit(1)
    print("全场景测试通过 ✅")


if __name__ == "__main__":
    main()
