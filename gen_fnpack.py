#!/usr/bin/env python3
"""
生成/校验本仓库根目录的 fnpack.json（FnDepot V2 规范）。

收录对象：veenyi/Fnos-Hermes-Studio 发布的 fnOS 原生 Hermes Studio FPK。

为什么必须自动生成：veenyi 有 auto-update.yml 每天发版，版本号 / size / sha256 每次都变。
手写 fnpack.json 必然过期，客户端强制校验会因 sha256 不匹配而失败。

更棘手的一点（真实故障）：veenyi 会在**同一个 tag 下重传同名 FPK**（打包重试 / 依赖重装），
tag 与版本号不变，但资产字节数与 sha256 都变了。索引一旦没跟上，客户端就会出现
「下载大小不完整: expected=… actual=…」——它按索引里的旧 size 判定，而实际下到的是新文件。
因此本脚本除了生成索引，还做三件事：
  1. 静默期（--settle）：资产刚被替换的 N 分钟内不收录，避免 CDN 内容与新 size 尚未对齐；
  2. 尺寸对账：写盘前用 HEAD 的 Content-Length 与 API size 反复核对，不一致就跳过该版本；
  3. 漂移告警：与已发布的 fnpack.json 比对，同 tag 的 size/sha256 变化会显式打印。

用法:
    python3 gen_fnpack.py                 # 生成 fnpack.json 并校验
    python3 gen_fnpack.py --verify        # 额外联网校验（资源可达 + content-length == size）
    python3 gen_fnpack.py --deep          # 实际下载核对 sha256（很慢，约 330MB/版本）
    python3 gen_fnpack.py --settle 0      # 关闭静默期（本地排障用）

环境变量:
    FNDEPOT_OWNER   本源作者的 GitHub 用户名（写入 source_info.author / homepage）。
                    留空时自动从 git remote 推断，再退化为 FALLBACK_OWNER。

注意：FnDepot 规范禁止把 "FnDepot" 用作源名或作者名；本脚本会强制校验这一点。
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone

REPO = "veenyi/Fnos-Hermes-Studio"
UPSTREAM = "https://github.com/EKKOLearnAI/hermes-studio"
FALLBACK_OWNER = "CHANGE_ME"

# 相对 FPK manifest 实测值（veenyi 仓库根 manifest）
APP_NAME = "hermes-studio"  # 必须等于 FPK manifest 的 appname，区分大小写
RUN_AS = "package"  # config/privilege: defaults.run-as
IS_DOCKER = False
INSTALL_TYPE = ""  # 存储空间
SERVICE_PORT = "8648"
CATEGORIES = ["AI赋能", "系统工具"]  # 固定分类，最多两个，第一项为主分类

# 有意偏离上游 manifest 的 platform：manifest 写 all，但原生 node-pty 绑定
# Linux x64 / Node24 ABI，打包脚本无 arm 逻辑，填 x86 可避免 arm 装完启动失败。
PLATFORM = ["x86"]
KEEP_VERSIONS = 10
FETCH_EXTRA = 3        # 多抓几个，给下面「静默期/对账失败被跳过」留余量
SETTLE_MINUTES = 0     # 资产刚被重传后的静默期；默认关闭（见 README：真正把关的是 Content-Length 对账，
                       # 静默期只会推迟恢复，上游一天重传两三次时会白白错过最新版本）
HEAD_RETRIES = 5       # Content-Length 与 API size 对账的重试次数
HEAD_RETRY_SLEEP = 5

DESC = (
    "Hermes Studio 是 Hermes Agent 的桌面 / Web 控制台与本地运行时，"
    "支持与 AI Agent 流式对话、管理模型与配置、接入多平台渠道、自动化任务、"
    "文件浏览器、Web 终端与本地数据持久化。"
    "<br/><br/><strong>核心能力：</strong>"
    "<br/>• Agent 聊天：Socket.IO 流式运行 Hermes Agent，支持工具追踪与本地会话持久化"
    "<br/>• 本地控制台：集中管理 profiles、providers、models、凭证、记忆、技能、插件与日志"
    "<br/>• 自动化：配置平台渠道、cron 定时任务、看板任务、群聊房间与 MCP 服务器"
    "<br/>• 工作区工具：文件浏览器、Web 终端、语音输入输出与编码 Agent 运行器"
    "<br/><br/><strong>部署说明：</strong>"
    "<br/>本包为官方预构建产物 + bundled node_modules 的非 Docker 打包，"
    "安装时离线复制预装依赖，不联网执行 npm install，启动后监听 8648 端口。"
)

# icon / preview / readme 直接引用上游 raw，仓库地址不会变
ICON_URL = f"https://raw.githubusercontent.com/{REPO}/main/ICON_256.PNG"
PREVIEW_URLS = [f"https://raw.githubusercontent.com/{REPO}/main/preview/preview.png"]
README_URL = f"https://raw.githubusercontent.com/{REPO}/main/README.md"
BUG_REPORT_URL = f"https://github.com/{REPO}/issues"

SOURCE_NAME_SUFFIX = "Hermes Studio 应用源"


def detect_owner():
    """推断本源作者（仓库属主）：环境变量 > git remote > FALLBACK_OWNER。"""
    env = os.environ.get("FNDEPOT_OWNER", "").strip()
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=here, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception:
        url = ""
    m = re.search(r"github\.com[/:]([^/]+)/", url)
    if m and m.group(1) not in ("github.com",):
        return m.group(1)
    return FALLBACK_OWNER


def http_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "fndepot-gen", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def parse_ts(s):
    """解析 GitHub 的 ISO8601 时间戳；无法解析返回 None。"""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def norm_version(tag, name):
    """
    veenyi 的 tag 形如 v0.7.25-1：主版本是 web-ui 版本，-1 是打包迭代号。
    FnDepot 客户端要求 SemVer 可比较，直接用 tag 去掉 v 前缀即可（0.7.25-1 是合法 SemVer 预发布号）。
    """
    return tag.lstrip("v")


def fetch_releases(limit):
    releases = http_json(f"https://api.github.com/repos/{REPO}/releases?per_page={max(limit, 30)}")
    out = []
    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        fpk = next((a for a in rel.get("assets", []) if a["name"].endswith(".fpk")), None)
        if not fpk:
            continue
        digest = (fpk.get("digest") or "").replace("sha256:", "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            print(f"  ! 跳过 {rel['tag_name']}：GitHub 未提供 sha256 digest，无法保证校验安全", file=sys.stderr)
            continue
        out.append(
            {
                "version": norm_version(rel["tag_name"], fpk["name"]),
                "tag": rel["tag_name"],
                "download_url": fpk["browser_download_url"],
                "sha256": digest,
                "size": fpk["size"],
                "updated_at": rel.get("published_at"),
                "asset_updated_at": fpk.get("updated_at"),
                "changelog": (rel.get("body") or "").strip() or f"见 {rel['html_url']}",
                "html_url": rel["html_url"],
            }
        )
        if len(out) >= limit:
            break
    return out


def head_content_length(url, timeout=30):
    """取下载 URL（跟随 302 到 release-assets CDN）的 Content-Length。"""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "fndepot-gen"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        clen = resp.headers.get("Content-Length")
        return int(clen) if clen and str(clen).isdigit() else None


def load_published(path):
    """读取仓库里已发布的 fnpack.json，用于同 tag 漂移比对。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def report_drift(published, rels):
    """同一个版本号下 size/sha256 变了 = 上游重传同名资产，必须显式告警。"""
    known = {}
    if published:
        app = (published.get("apps") or {}).get(APP_NAME) or {}
        for ver, r in (app.get("releases") or {}).items():
            p = (r.get("packages") or {}).get("all") or {}
            if p.get("sha256"):
                known[ver] = (p.get("size"), p["sha256"])
    for r in rels:
        prev = known.get(r["version"])
        if not prev:
            continue
        old_size, old_sha = prev
        if old_sha != r["sha256"] or old_size != r["size"]:
            print(f"  [!] 上游同 tag 重传 {r['version']}：size {old_size} → {r['size']}，"
                  f"sha256 {str(old_sha)[:12]}… → {r['sha256'][:12]}…")


def stabilize(rels, settle_minutes=SETTLE_MINUTES, now=None, only_versions=None):
    """
    剔除「暂时不该收录」的版本，返回 (可发布列表, 跳过原因列表)。

    跳过两种情况：
      a) 资产在静默期内刚被替换 —— 此刻收录可能与 CDN 实际内容错位；
      b) HEAD 的 Content-Length 与 API size 对不上 —— 说明上传仍在进行或 CDN 未刷新，
         收录必然让客户端报「下载大小不完整」。

    only_versions：只对这些版本做 HEAD 对账（高频轮询时用于省请求，其余版本直接信任 API）。
    """
    now = now or datetime.now(timezone.utc)
    check = None if only_versions is None else set(only_versions)
    keep, skipped = [], []
    for r in rels:
        if check is not None and r["version"] not in check:
            keep.append(r)
            continue
        upd = parse_ts(r.get("asset_updated_at"))
        if settle_minutes > 0 and upd is not None:
            age = (now - upd).total_seconds() / 60.0
            if age < settle_minutes:
                skipped.append(f"{r['version']}：资产 {age:.0f} 分钟前刚被替换，等待 {settle_minutes} 分钟静默期")
                continue
        clen, ok = None, False
        for attempt in range(HEAD_RETRIES):
            try:
                clen = head_content_length(r["download_url"])
            except Exception as e:
                clen = None
                if attempt == HEAD_RETRIES - 1:
                    skipped.append(f"{r['version']}：HEAD 取不到 Content-Length（{e}）")
                    break
            if clen is not None and clen == r["size"]:
                ok = True
                break
            if attempt < HEAD_RETRIES - 1:
                time.sleep(HEAD_RETRY_SLEEP)
        if ok:
            keep.append(r)
        elif clen is not None:
            skipped.append(f"{r['version']}：Content-Length({clen}) 与 API size({r['size']}) 不一致，疑似重传中")
    return keep, skipped


def build(rels, owner):
    releases = {}
    for r in rels:
        releases[r["version"]] = {
            "changelog": r["changelog"][:2000],
            "updated_at": r["updated_at"],
            "packages": {
                # veenyi 只发布单一架构 FPK（无 _x86/_arm 后缀），用 all 作为通用包。
                "all": {
                    "download_url": r["download_url"],
                    "sha256": r["sha256"],
                    "size": r["size"],
                }
            },
        }
    return {
        "schema_version": "2",
        "source_info": {
            # 规范禁止把 "FnDepot" 用作源名/作者名，因此源名不含该词。
            "name": f"{owner} 的 {SOURCE_NAME_SUFFIX}",
            "author": owner,
            "homepage": f"https://github.com/{owner}/FnDepot",
            "description": "收录 veenyi/Fnos-Hermes-Studio 发布的 fnOS 原生 Hermes Studio FPK。",
        },
        "apps": {
            APP_NAME: {
                "display_name": "Hermes Studio",
                "desc": DESC,
                "platform": PLATFORM,
                "categories": CATEGORIES,
                "icon_url": ICON_URL,
                "preview_urls": PREVIEW_URLS,
                "readme_url": README_URL,
                "bug_report_url": BUG_REPORT_URL,
                "maintainer": "EKKOLearnAI",
                "maintainer_url": UPSTREAM,
                "distributor": "veenyi",
                "distributor_url": f"https://github.com/{REPO}",
                "run_as": RUN_AS,
                "install_type": INSTALL_TYPE,
                "is_docker": IS_DOCKER,
                "service_port": SERVICE_PORT,
                "details_updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "releases": releases,
            }
        },
    }


# ---------------- 校验 ----------------
FIXED_CATEGORIES = {
    "影音娱乐", "系统工具", "编程开发", "AI赋能", "生活服务",
    "智能智控", "教育学习", "游戏地带", "硬件驱动",
}
ALLOWED_ARCH = {"all", "x86", "arm"}
REQUIRED_APP = ["display_name", "desc", "platform", "categories", "icon_url", "run_as", "install_type", "is_docker", "releases"]


def validate(doc):
    errs, warns = [], []
    if doc.get("schema_version") != "2":
        errs.append("schema_version 必须是字符串 \"2\"")
    si = doc.get("source_info") or {}
    for k in ("name", "author"):
        if not si.get(k):
            errs.append(f"source_info.{k} 缺失")
    # 规范：不得使用 FnDepot 项目名作为源名/作者名，避免混淆
    for k in ("name", "author"):
        if re.search(r"fn\s*depot", str(si.get(k, "")), re.I):
            errs.append(f"source_info.{k} 不得包含 'FnDepot'（规范要求）")
    if re.search(r"fn\s*depot", str(si.get("description", "")), re.I):
        warns.append("source_info.description 含 'FnDepot'，仅描述性提及可接受，但建议改写")
    apps = doc.get("apps")
    if not isinstance(apps, dict) or not apps:
        errs.append("apps 必须是非空对象")
        return errs, warns
    for name, app in apps.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            errs.append(f"{name}: appname 非法字符")
        if name != APP_NAME:
            warns.append(f"{name}: appname 与 FPK manifest 不符（应为 {APP_NAME}）")
        for k in REQUIRED_APP:
            if k not in app:
                errs.append(f"{name}: 缺少必填字段 {k}")
        if "details_updated_at" in si:
            errs.append("details_updated_at 不得出现在 source_info")
        cats = app.get("categories", [])
        if not cats or any(c not in FIXED_CATEGORIES for c in cats):
            errs.append(f"{name}: categories 非法 {cats}")
        if len(cats) > 2:
            errs.append(f"{name}: categories 超过两个")
        for p in [app.get("platform")] if isinstance(app.get("platform"), str) else app.get("platform", []):
            if p not in ALLOWED_ARCH:
                errs.append(f"{name}: platform 非法值 {p}")
        if app.get("run_as") not in ("package", "root"):
            errs.append(f"{name}: run_as 必须是 package/root")
        if not isinstance(app.get("is_docker"), bool):
            errs.append(f"{name}: is_docker 必须是布尔值")
        if not isinstance(app.get("install_type"), str):
            errs.append(f"{name}: install_type 必须是字符串")
        if len(app.get("preview_urls", [])) > 8:
            errs.append(f"{name}: preview_urls 超过 8 张")
        rels = app.get("releases", {})
        if not rels:
            errs.append(f"{name}: releases 为空")
        for ver, r in rels.items():
            if ver.lower() in ("latest", "") or not re.match(r"^\d+(\.\d+)*(-\S+)?$", ver):
                errs.append(f"{name}.{ver}: 版本号不可比较")
            pkgs = r.get("packages") or {}
            if not pkgs:
                errs.append(f"{name}.{ver}: 缺少 packages")
            for arch, p in pkgs.items():
                if arch not in ALLOWED_ARCH:
                    errs.append(f"{name}.{ver}: 非法架构键 {arch}（V2 不允许 universal 等自定义键）")
                    continue
                if not str(p.get("download_url", "")).startswith(("http://", "https://")):
                    errs.append(f"{name}.{ver}.{arch}: download_url 非法")
                if "sha256" in p and not re.fullmatch(r"[0-9a-f]{64}", str(p["sha256"]).lower()):
                    errs.append(f"{name}.{ver}.{arch}: sha256 格式错误")
                if "size" in p and not isinstance(p["size"], int):
                    errs.append(f"{name}.{ver}.{arch}: size 必须是整数（字节），不能写 \"20 MB\"")
    return errs, warns


def verify_online(doc, deep=False):
    """联网校验：资源可达、FPK content-length 与 size 一致；deep=True 时下载并核对 sha256。"""
    app = next(iter(doc["apps"].values()))
    problems = []
    for label, url in [("icon", app["icon_url"]), ("preview", (app.get("preview_urls") or [""])[0]),
                       ("readme", app.get("readme_url", ""))]:
        if not url:
            continue
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "fndepot-gen"})
            with urllib.request.urlopen(req, timeout=20) as r:
                print(f"  [{'OK' if r.status == 200 else '??'}] {label}: HTTP {r.status}")
        except Exception as e:
            problems.append(f"{label} 不可达: {e}")

    for ver, r in app["releases"].items():
        p = r["packages"]["all"]
        url = p["download_url"]
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "fndepot-gen"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                clen = resp.headers.get("Content-Length")
                ranges = resp.headers.get("Accept-Ranges")
                ok = clen and int(clen) == p["size"]
                print(f"  [{'OK' if ok else '!!'}] {ver}: size={p['size']} content-length={clen} ranges={ranges}")
                if clen and not ok:
                    problems.append(f"{ver}: size({p['size']}) 与 content-length({clen}) 不一致")
        except Exception as e:
            problems.append(f"{ver}: FPK 不可达 {e}")
        if deep:
            # 边下边算，不把 330MB 读进内存；落盘到 TMPDIR，避免写 /tmp 撑爆内存盘
            h = hashlib.sha256()
            tmp = os.path.join(tempfile.gettempdir(), ".fndepot-verify.fpk")
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(url, headers={"User-Agent": "fndepot-gen"}), timeout=600
                ) as resp, open(tmp, "wb") as f:
                    for chunk in iter(lambda: resp.read(1 << 20), b""):
                        h.update(chunk)
                        f.write(chunk)
                got = h.hexdigest()
                print(f"  [{'OK' if got == p['sha256'] else '!!'}] {ver}: sha256 实测={got[:16]}…")
                if got != p["sha256"]:
                    problems.append(f"{ver}: sha256 不匹配！")
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=KEEP_VERSIONS, help=f"保留最近 N 个版本（默认 {KEEP_VERSIONS}）")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "fnpack.json"))
    ap.add_argument("--verify", action="store_true", help="生成后联网校验资源可达性与 size")
    ap.add_argument("--deep", action="store_true", help="实际下载核对 sha256（很慢）")
    ap.add_argument("--settle", type=int, default=SETTLE_MINUTES,
                    help=f"资产重传后的静默期分钟数，0 关闭（默认 {SETTLE_MINUTES}）")
    ap.add_argument("--owner", default=None, help="本源作者 GitHub 用户名（默认自动推断）")
    args = ap.parse_args()

    owner = args.owner or detect_owner()
    if owner == FALLBACK_OWNER:
        print("错误：无法推断本源作者用户名。请用 --owner <你的GitHub用户名> 或设置 FNDEPOT_OWNER。",
              file=sys.stderr)
        return 2

    print(f"源作者：{owner}")
    print(f"拉取 {REPO} 最近 releases …")
    rels = fetch_releases(args.limit + FETCH_EXTRA)
    if not rels:
        print("错误：没有可用 release（缺少 .fpk 资产或 sha256）", file=sys.stderr)
        return 1

    print("漂移检查（与已发布索引比对）：")
    report_drift(load_published(args.out), rels)

    print(f"稳定性检查（静默期 {args.settle} 分钟 + Content-Length 对账）：")
    stable, skipped = stabilize(rels, settle_minutes=args.settle)
    for s in skipped:
        print(f"  [跳过] {s}")
    if not stable:
        print("错误：所有版本都未通过稳定性检查，保留原 fnpack.json 不动。", file=sys.stderr)
        return 1
    stable = stable[:args.limit]

    doc = build(stable, owner)
    print(f"收录 {len(stable)} 个版本，最新 {stable[0]['version']}")

    errs, warns = validate(doc)
    for w in warns:
        print(f"  [warn] {w}")
    if errs:
        for e in errs:
            print(f"  [FAIL] {e}", file=sys.stderr)
        return 1
    print("  规范校验通过")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print(f"已写入 {args.out} ({os.path.getsize(args.out)} bytes)")

    if args.verify or args.deep:
        print("联网校验：")
        probs = verify_online(doc, deep=args.deep)
        for p in probs:
            print(f"  [FAIL] {p}", file=sys.stderr)
        if probs:
            return 1
        print("  联网校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
