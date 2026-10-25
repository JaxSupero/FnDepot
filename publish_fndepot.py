#!/usr/bin/env python3
"""把本地工作副本的三个文件推到 JaxSupero/FnDepot（Contents API）。

用法:
    GH_TOKEN=xxx python3 publish_fndepot.py --dry-run
    GH_TOKEN=xxx python3 publish_fndepot.py

为什么单独写一个：仓库里原来的 gh_upload.py 走浏览器网页上传，但它依赖的
gh_repo.py 不在仓库里，而且 .github/workflows/*.yml 只能拍平后手工 rename。
有 PAT 时走 Contents API 一步到位，并能带 sha 做并发保护（避免覆盖别人刚提交的版本）。
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

OWNER = "JaxSupero"
REPO = "FnDepot"
BRANCH = "main"
HERE = os.path.dirname(os.path.abspath(__file__))
FILES = [
    "gen_fnpack.py",
    "fnpack.json",
    ".github/workflows/sync-upstream.yml",
    "README.md",
]
COMMIT_MSG = os.environ.get(
    "GH_COMMIT_MSG",
    "feat: 收录 Octop（Docker 版 + 本地版 x86/arm）\n\n"
    "- gen_fnpack.py：新增 octop / octop-native 两个 spec；支持一个 release 多架构资产"
    "（arch_assets + pattern），版本号 bN 规范化为 SemVer 预发布号；新增 FNDEPOT_SKIP_HEAD 本地排障开关\n"
    "- fnpack.json：重新生成（含 Octop 各版本 size/sha256）\n"
    "- sync-upstream.yml：合规校验 KNOWN 增加 octop、octop-native\n"
    "- README.md：收录清单与多架构说明",
)


def api(method, path, token, data=None):
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        method=method,
        data=json.dumps(data).encode() if data else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "fndepot-publish",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        print(f"  HTTP {e.code} {path}: {body}", file=sys.stderr)
        raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        print("缺少 GH_TOKEN（fine-grained PAT，需 Contents: Read and write + Workflows: Read and write）",
              file=sys.stderr)
        return 2

    me = api("GET", "/user", token)
    print(f"认证身份：{me.get('login')}")

    # 先取远端当前 sha，PUT 时带上，避免并发覆盖
    shas = {}
    for path in FILES:
        try:
            cur = api("GET", f"/repos/{OWNER}/{REPO}/contents/{path}?ref={BRANCH}", token)
            shas[path] = cur["sha"]
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            shas[path] = None
        print(f"  {path}: 远端 sha={str(shas[path])[:10]}")

    for path in FILES:
        local = os.path.join(HERE, path)
        with open(local, "rb") as f:
            raw = f.read()
        if args.dry_run:
            print(f"[dry] {path} {len(raw)} bytes → 待提交")
            continue
        payload = {
            "message": COMMIT_MSG,
            "content": base64.b64encode(raw).decode(),
            "branch": BRANCH,
        }
        if shas[path]:
            payload["sha"] = shas[path]
        res = api("PUT", f"/repos/{OWNER}/{REPO}/contents/{path}", token, payload)
        print(f"  ✓ {path} → commit {res['commit']['sha'][:10]}")
    print("完成" if not args.dry_run else "DRY-RUN 完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
