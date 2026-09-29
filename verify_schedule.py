#!/usr/bin/env python3
"""
自查 FnDepot 索引的两件事，只读、不改任何东西：

  1) GitHub 上的 sync-upstream 定时到底有没有真的 armed
     —— 只看 API 的 state=active 是不够的。实测踩过：state=active、文件在默认分支、
        字节完全正确，但 event=schedule 的运行数恒为 0，定时从未触发。
        真正的判据是 event=schedule 是否产生过运行，以及最近一次距今多久。

  2) 线上 fnpack.json 是否已经过期
     —— 逐版本比对索引里的 size 与上游资产的真实 size。
        上游会在同一 tag 下重传同名 FPK，索引没跟上时客户端就会报
        「下载大小不完整: expected=… actual=…」。

用法:
    python3 verify_schedule.py
    python3 verify_schedule.py --repo JaxSupero/FnDepot

退出码: 0 = 两项都正常；1 = 定时未触发或索引过期。
"""

import argparse
import json
import urllib.request
from datetime import datetime, timezone

API = "https://api.github.com"
HDR = {"User-Agent": "fndepot-verify", "Accept": "application/vnd.github+json"}
UPSTREAM = "veenyi/Fnos-Hermes-Studio"


def get_json(url, raw=False):
    req = urllib.request.Request(url, headers=HDR)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8") if raw else json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="JaxSupero/FnDepot", help="索引仓库 owner/repo")
    a = ap.parse_args()
    owner, repo = a.repo.split("/", 1)
    now = datetime.now(timezone.utc)
    bad = []

    print("=" * 68)
    print(f"1) 定时任务是否已注册   [UTC {now.isoformat(timespec='seconds')}]")
    st = get_json(f"{API}/repos/{owner}/{repo}/actions/workflows")
    wf = next((w for w in st.get("workflows", []) if w["path"].endswith("sync-upstream.yml")), None)
    if not wf:
        print("   ❌ 找不到 sync-upstream.yml 工作流")
        bad.append("workflow 缺失")
    else:
        print(f"   workflow: {wf['name']}  path={wf['path']}")
        print(f"   state   : {wf['state']}   （注意：active ≠ 定时已注册）")
        runs = get_json(f"{API}/repos/{owner}/{repo}/actions/runs?event=schedule&per_page=5")
        total = runs.get("total_count", 0)
        print(f"   schedule 触发过的运行数: {total}")
        if total == 0:
            print("   ❌ 从未被定时触发过 —— GitHub 没有 armed 这个 schedule")
            print("      处置：在网页改一下 cron 表达式并提交（换分钟数即可），")
            print("            或在 Actions 页把该工作流 Disable → 再 Enable。")
            bad.append("schedule 未注册")
        else:
            last = runs["workflow_runs"][0]
            age = (now - datetime.fromisoformat(last["created_at"].replace("Z", "+00:00"))).total_seconds() / 60
            print(f"   最近一次: {last['created_at']}  ({age:.0f} 分钟前)  {last['conclusion']}")
            if age > 240:
                print(f"   ⚠ 已 {age/60:.1f} 小时没有定时运行，检查是否被 GitHub 停用（60 天无活动会自动停用）")
                bad.append("定时长时间未运行")

    print("=" * 68)
    print("2) 线上索引是否过期")
    live = json.loads(get_json(
        f"https://raw.githubusercontent.com/{owner}/{repo}/main/fnpack.json", raw=True))
    app = live["apps"]["hermes-studio"]
    up = get_json(f"{API}/repos/{UPSTREAM}/releases?per_page=15")
    real = {}
    for rel in up:
        fpk = next((x for x in rel.get("assets", []) if x["name"].endswith(".fpk")), None)
        if fpk:
            real[rel["tag_name"].lstrip("v")] = (fpk["size"], fpk.get("digest", "").replace("sha256:", ""))
    stale = []
    for ver, r in app["releases"].items():
        p = r["packages"]["all"]
        cur = real.get(ver)
        if not cur:
            print(f"   [--] {ver}: 上游已无此 Release（索引保留，无碍）")
            continue
        size_ok = cur[0] == p["size"]
        sha_ok = (not cur[1]) or cur[1] == p["sha256"]
        flag = "OK" if (size_ok and sha_ok) else "!!"
        print(f"   [{flag}] {ver}: 索引 size={p['size']} 实际={cur[0]}"
              f"{'' if size_ok else '  ← 不一致，客户端会报下载大小不完整'}")
        if not (size_ok and sha_ok):
            stale.append(ver)
    if stale:
        print(f"   ❌ 过期版本: {', '.join(stale)} —— 重新生成 fnpack.json 并提交")
        bad.append("索引过期")
    else:
        print("   ✅ 索引与上游一致")

    print("=" * 68)
    print("结论: " + ("✅ 两项检查通过" if not bad else "❌ " + "；".join(bad)))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
