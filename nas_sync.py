#!/usr/bin/env python3
"""
NAS 侧兜底同步 —— 把 fnpack.json 推到 GitHub，**完全不依赖 GitHub Actions 的定时器**。

为什么需要它（实测结论）：
  1. JaxSupero/FnDepot 的 sync-upstream.yml 从上线起就没被 GitHub 注册过定时：
     state=active、文件在默认分支、字节无误，但 event=schedule 运行数恒为 0，
     换过三次 cron 表达式也没用；
  2. 上游 veenyi/Fnos-Hermes-Studio 会在**同一个 tag 下反复重传同名 FPK**
     （实测一天内 v0.7.25-1 被换了三次：343501170 → 343503444 → 343495677），
     索引只要没跟上，客户端就报「下载大小不完整: expected=… actual=…」。
  两者叠加 ⇒ 索引必须由一台常开的机器盯着。这台 NAS 就是。

行为：
  - 用 gen_fnpack 的同一套逻辑抓上游、做 Content-Length 对账、生成索引；
  - 与线上 fnpack.json 比对，**内容没变就什么都不做**（不产生空提交）；
  - 变了就经 Contents API PUT 覆盖（带 sha，避免并发覆盖）；
  - 只有真正推送时才往 stdout 写一行（配合 hermes cron --no-agent，无输出=静默）；
  - 出错写 stderr 并以非零码退出。

Token：
  从 .gh_token 读取（同目录，chmod 600），或环境变量 GH_TOKEN / GITHUB_TOKEN。
  需要对该仓库有 Contents: Read and write 的 fine-grained PAT（或 repo scope 的经典 PAT）。
  绝不要把 token 写进仓库文件或打印出来。

用法：
    python3 nas_sync.py --dry-run      # 只看会不会推、差异在哪，不写 GitHub、不需要 token
    python3 nas_sync.py --check-token  # 只验证 token 有效性与权限
    python3 nas_sync.py                # 正常同步（cron 用这个）
"""

import argparse
import base64
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gen_fnpack as G  # noqa: E402

API = "https://api.github.com"
OWNER = os.environ.get("FNDEPOT_REPO_OWNER", "JaxSupero")
REPO = os.environ.get("FNDEPOT_REPO_NAME", "FnDepot")
PATH = "fnpack.json"
BRANCH = "main"
TOKEN_FILE = os.path.join(HERE, ".gh_token")
UA = "fndepot-nas-sync"

# 浏览器兜底通道（没有 PAT 时用）：gh_upload.py 需要 websockets，只有这个解释器有
VENV_PY = "/vol1/@apphome/hermes-studio/hermes-agent/venv/bin/python3"
CDP = "http://127.0.0.1:16003"
GH_UPLOAD = os.path.join(HERE, "gh_upload.py")


def read_token():
    for env in ("GH_TOKEN", "GITHUB_TOKEN"):
        v = (os.environ.get(env) or "").strip()
        if v:
            return v, f"env:{env}"
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line, TOKEN_FILE
    return None, None


def api(method, path, token=None, body=None, timeout=40):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/vnd.github+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"message": raw[:300]}
    except Exception as e:  # 网络层
        return 0, {"message": f"{type(e).__name__}: {e}"}


def strip_volatile(doc):
    """比对时忽略 details_updated_at —— 它每次生成都会变，否则会天天推空提交。"""
    d = json.loads(json.dumps(doc))
    app = (d.get("apps") or {}).get(G.APP_NAME) or {}
    app.pop("details_updated_at", None)
    return d


def err_msg(d):
    """从 API 返回里安全取错误文本（返回不一定是 dict）。"""
    return d.get("message") if isinstance(d, dict) else str(d)[:200]


def build_index(limit=G.KEEP_VERSIONS, deployed=None, verbose=False):
    """抓上游 + 稳定性对账 + 生成索引。返回 (doc, skipped 列表)。"""
    rels = G.fetch_releases(limit + G.FETCH_EXTRA)
    if not rels:
        raise RuntimeError("上游没有可用 release（缺 .fpk 或 sha256）")

    # 只对「新增或 size/sha 变化的版本」做 HEAD 对账，省请求
    check = {rels[0]["version"]}
    if deployed:
        dr = ((deployed.get("apps") or {}).get(G.APP_NAME) or {}).get("releases") or {}
        for r in rels:
            p = dr.get(r["version"], {}).get("packages", {}).get("all", {})
            if p.get("size") != r["size"] or p.get("sha256") != r["sha256"]:
                check.add(r["version"])

    stable, skipped = G.stabilize(rels, settle_minutes=G.SETTLE_MINUTES, only_versions=check)
    if verbose:
        for s in skipped:
            print(f"   [跳过] {s}")
    if not stable:
        raise RuntimeError("所有版本都未通过对账，保留线上索引不动")
    stable = stable[:limit]
    if len(stable) < len(rels) and verbose:
        print(f"   [提示] 上游有 {len(rels)} 个版本，本次收录 {len(stable)} 个")
    doc = G.build(stable, OWNER)
    errs, warns = G.validate(doc)
    for w in warns:
        if verbose:
            print(f"   [warn] {w}")
    if errs:
        raise RuntimeError("规范校验失败：" + "；".join(errs))
    return doc, skipped


def cdp_alive():
    """CDP 端口上有没有活的浏览器（本地回环，别走代理）。"""
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(f"{CDP}/json/version", timeout=6) as r:
            return r.status == 200
    except Exception:
        return False


def browser_push(doc, changes):
    """
    没有 PAT 时的兜底：把新索引写到本地 → 用宿主机 Chrome 的真实会话走网页上传页提交。

    实测（2026-09-30）：
      - 默认 context 能直连 github.com、握着 JaxSupero 的真实登录态，网页提交正常；
      - 「建代理 context + 从默认 context 读 cookie 注入」那条路 POST 一定被 GitHub
        的 CSRF 检查拒掉（返回 "You signed in with another tab or window"），别再走。

    返回 (rc, 说明)：0=成功，3=浏览器不可用（属预期情况），1=真失败。
    """
    if not os.path.exists(VENV_PY):
        return 3, f"缺少解释器 {VENV_PY}"
    if not cdp_alive():
        return 3, "CDP 无响应（浏览器没开？）"

    body_text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    tmp = os.path.join(HERE, ".fnpack.nas.json")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body_text)
        os.replace(tmp, os.path.join(HERE, PATH))
    except Exception as e:
        return 1, f"写本地索引失败：{e}"

    latest = next(iter(doc["apps"][G.APP_NAME]["releases"]))
    env = dict(os.environ)
    env["GH_COMMIT_MSG"] = (f"chore: 同步 Hermes Studio {latest}\n\n"
                            f"上游同 tag 重传导致索引过期，NAS 侧自动修正。"
                            f"变更：{'; '.join(changes) or '元数据'}")
    try:
        r = subprocess.run([VENV_PY, GH_UPLOAD, "--ctx", "default", "--files", PATH],
                           capture_output=True, text=True, timeout=240, cwd=HERE, env=env)
    except subprocess.TimeoutExpired:
        return 1, "浏览器提交超时"
    out = (r.stdout or "").strip()
    if r.returncode == 0 and "✅ 已提交" in out:
        return 0, "经浏览器提交成功"
    return 1, f"浏览器提交失败 rc={r.returncode}；{out[-200:]}{(r.stderr or '')[-200:]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报告差异，不写 GitHub")
    ap.add_argument("--check-token", action="store_true", help="只验证 token")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    token, src = read_token()

    if a.check_token:
        if not token:
            print(f"❌ 没找到 token。请把 PAT 写进 {TOKEN_FILE}（chmod 600），或设 GH_TOKEN。", file=sys.stderr)
            return 2
        st, d = api("GET", f"/repos/{OWNER}/{REPO}", token)
        if st != 200:
            print(f"❌ token 无效或无权访问 {OWNER}/{REPO}：HTTP {st} {err_msg(d)}", file=sys.stderr)
            return 1
        st2, d2 = api("GET", f"/repos/{OWNER}/{REPO}/contents/{PATH}?ref={BRANCH}", token)
        can_write = (d2.get("permissions") or {}).get("push") if isinstance(d2, dict) else None
        print(f"✅ token 有效（来源 {src}）")
        print(f"   仓库 {OWNER}/{REPO} 可见；contents/{PATH} HTTP {st2}，push 权限={can_write}")
        return 0

    # 1) 取线上索引（同时拿到 sha）
    st, live = api("GET", f"/repos/{OWNER}/{REPO}/contents/{PATH}?ref={BRANCH}", token)
    if st != 200:
        print(f"❌ 读不到线上 {PATH}：HTTP {st} {err_msg(live)}", file=sys.stderr)
        return 1
    live_sha = live["sha"]
    deployed = json.loads(base64.b64decode(live["content"]).decode("utf-8"))

    # 2) 生成新索引
    try:
        doc, skipped = build_index(deployed=deployed, verbose=a.verbose)
    except Exception as e:
        print(f"❌ 生成索引失败：{e}", file=sys.stderr)
        return 1

    # 3) 比对（忽略 details_updated_at）
    if strip_volatile(doc) == strip_volatile(deployed):
        if a.verbose:
            print("   [不变] 线上索引已是最新，无需提交")
        return 0

    latest = next(iter(doc["apps"][G.APP_NAME]["releases"]))
    dr = deployed["apps"][G.APP_NAME]["releases"]
    nr = doc["apps"][G.APP_NAME]["releases"]
    changes = []
    for v, r in nr.items():
        p = r["packages"]["all"]
        op = (dr.get(v) or {}).get("packages", {}).get("all")
        if op is None:
            changes.append(f"新增 {v}")
        elif op.get("size") != p["size"] or op.get("sha256") != p["sha256"]:
            changes.append(f"{v} size {op.get('size')}→{p['size']}")
    for v in dr:
        if v not in nr:
            changes.append(f"移除 {v}")

    if a.dry_run:
        print(f"[dry-run] 索引需要更新：{latest}；差异：{'; '.join(changes) or '仅元数据'}")
        if skipped:
            print(f"[dry-run] 跳过：{'; '.join(skipped)}")
        return 0

    if not token:
        rc, why = browser_push(doc, changes)
        if rc == 0:
            print(f"🔄 已同步 Hermes Studio {latest} → {OWNER}/{REPO}（浏览器通道）；"
                  f"变更：{'; '.join(changes) or '元数据'}")
            return 0
        if rc == 3:
            # 浏览器不可用属预期情况（没开浏览器 / CDP 没起），静默退出，
            # 免得 cron 每 5 分钟报一次失败告警
            if a.verbose:
                print(f"   [跳过] {why}，且没有 PAT", file=sys.stderr)
            return 0
        print(f"❌ {why}", file=sys.stderr)
        return 1

    # 4) 推送（带 sha；并发冲突则重取一次）
    body_text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    msg = f"chore: 同步 Hermes Studio {latest}"
    for attempt in (1, 2):
        payload = {
            "message": msg,
            "content": base64.b64encode(body_text.encode("utf-8")).decode(),
            "branch": BRANCH,
            "sha": live_sha,
        }
        st, res = api("PUT", f"/repos/{OWNER}/{REPO}/contents/{PATH}", token, payload, timeout=60)
        if st in (200, 201):
            commit = ((res.get("commit") or {}).get("sha") or "")[:10]
            # 本地副本同步落盘，保持与线上一致
            try:
                tmp = os.path.join(HERE, ".fnpack.nas.json")
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(body_text)
                os.replace(tmp, os.path.join(HERE, PATH))
            except Exception as e:
                print(f"   （本地副本写入失败，不影响线上：{e}）")
            print(f"🔄 已同步 Hermes Studio {latest} → {OWNER}/{REPO} commit={commit}；"
                  f"变更：{'; '.join(changes) or '元数据'}")
            return 0
        if st in (409, 422) and attempt == 1:
            # 有人抢先提交了，重新取 sha 再试一次
            st2, again = api("GET", f"/repos/{OWNER}/{REPO}/contents/{PATH}?ref={BRANCH}", token)
            if st2 == 200:
                live_sha = again["sha"]
                deployed = json.loads(base64.b64decode(again["content"]).decode("utf-8"))
                if strip_volatile(doc) == strip_volatile(deployed):
                    if a.verbose:
                        print("   [不变] 重取后已一致")
                    return 0
                continue
        print(f"❌ 推送失败：HTTP {st} {res.get('message')}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
