#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gitlab_auth.py — GitLab 认证解析(纯标准库,随技能分发)

职责:把「账号密码」统一成一组 HTTP 请求头,供 gitlab-scan-youli.py /
gitlab-report-closed.py 复用。本文件本身不持有任何账号密码——凭证由
调用方从环境变量(运行期由 agent 从记忆注入)或命令行参数传入。

认证方式(唯一):
  user+password → 先试 OAuth 密码授权拿 Bearer,失败则退回 HTTP Basic

⚠️ 账号密码严禁写进技能目录任何文件(SKILL.md / scripts / 模板)。
   正确做法:首次运行由 agent 向用户索取 → 存入 agent 记忆 → 每次运行
   从记忆读出后以环境变量 GITLAB_USER / GITLAB_PASSWORD 注入本脚本。
"""
import base64
import json
import os
import urllib.request
import urllib.error
import urllib.parse


def get_bearer_token(host, user, password, timeout=30):
    """试 OAuth 密码授权(grant_type=password),成功返回 access_token,否则 None。"""
    host = host.rstrip("/")
    url = f"{host}/oauth/token"
    data = urllib.parse.urlencode({
        "grant_type": "password",
        "username": user,
        "password": password,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
        return body.get("access_token")
    except Exception:
        return None


def resolve_headers(host, user=None, password=None):
    """返回 (headers: dict, method: str)。method 描述用了哪种认证,便于日志提示。"""
    if user and password:
        bearer = get_bearer_token(host, user, password)
        if bearer:
            return {"Authorization": f"Bearer {bearer}"}, "OAuth-Bearer"
        raw = f"{user}:{password}".encode("utf-8")
        b64 = base64.b64encode(raw).decode("ascii")
        return {"Authorization": f"Basic {b64}"}, "HTTP-Basic"
    raise RuntimeError(
        "缺少认证凭证:需 GITLAB_USER + GITLAB_PASSWORD")


def resolve_from_env(host, user=None, password=None):
    """从显式参数或环境变量解析认证头。

    运行期由 agent 注入:GITLAB_USER / GITLAB_PASSWORD。
    """
    user = user or os.environ.get("GITLAB_USER", "")
    password = password or os.environ.get("GITLAB_PASSWORD", "")
    return resolve_headers(host, user=user or None, password=password or None)
