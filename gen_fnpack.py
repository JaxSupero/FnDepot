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
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

REPO = "veenyi/Fnos-Hermes-Studio"
UPSTREAM = "https://github.com/EKKOLearnAI/hermes-studio"
FALLBACK_OWNER = "CHANGE_ME"

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

SOURCE_NAME_SUFFIX = "fnOS 应用源"


# ---------------- 多应用源定义 ----------------
# 每个应用一份 spec。新增应用只需在 APP_SPECS 里追加一项，
# 生成 / 校验 / NAS 同步 / Actions 合规检查都会自动覆盖到它。
APP_SPECS = [
    {
        "app_name": "hermes-studio",  # 必须等于 FPK manifest 的 appname，区分大小写
        "repo": REPO,
        "upstream": UPSTREAM,
        # veenyi 只发布单一架构 FPK（无 _x86/_arm 后缀），用 all 作为通用回退包。
        "arch_key": "all",
        # 有意偏离上游 manifest 的 platform：manifest 写 all，但原生 node-pty 绑定
        # Linux x64 / Node24 ABI，打包脚本无 arm 逻辑，填 x86 可避免 arm 装完启动失败。
        "platform": ["x86"],
        "display_name": "Hermes Studio",
        "desc": DESC,
        "categories": ["AI赋能", "系统工具"],  # 固定分类，最多两个，第一项为主分类
        "icon_url": ICON_URL,
        "preview_urls": PREVIEW_URLS,
        "readme_url": README_URL,
        "bug_report_url": BUG_REPORT_URL,
        "maintainer": "EKKOLearnAI",
        "maintainer_url": UPSTREAM,
        "distributor": "veenyi",
        "distributor_url": f"https://github.com/{REPO}",
        "run_as": "package",  # config/privilege: defaults.run-as
        "install_type": "",   # 存储空间
        "is_docker": False,
        "service_port": "8648",
        "keep_versions": KEEP_VERSIONS,
    },
    {
        "app_name": "douyin_sparkflow",
        "repo": "JaxSupero/douyin-sparkflow-fpk",
        "upstream": "https://github.com/halfwaystudent/douyin-sparkflow",
        # 本 FPK 只出 x86_64（manifest platform=x86，包内是 x86-64 ELF），
        # 因此架构键用 x86 而非 all，避免 arm64 设备被推荐安装。
        "arch_key": "x86",
        "platform": ["x86"],
        "display_name": "抖音 SparkFlow",
        "desc": """抖音多账号火花自动维护工具（非官方第三方项目）：自动维护好友火花标记，支持多账号管理、定时发送与 Web 控制台。<br/><br/><strong>核心能力：</strong><br/>• 扫码登录：内置 noVNC 登录桌面，在 Web 页面内直接扫码、处理验证码<br/>• 多账号管理：同时维护多个账号，独立配置好友与发送策略<br/>• 定时调度：可配置发送窗口（如 10:00-18:00/20m），自动补偿漏发<br/>• 消息模板：一言、节日祝福、自定义文本随机化发送<br/>• 可视化面板：进度环、发送明细、日志查看与失败重试<br/><br/><strong>部署说明：</strong><br/>本包为飞牛 fnOS 原生部署（非 Docker）：依赖应用中心的 Python 3.12 与 Node.js v24 运行时；Python 依赖、Playwright Chromium 与 noVNC 登录环境已预置在包内，安装后无需联网拉取依赖、无需构建镜像。数据（账号、登录态、浏览器配置、日志）保存在应用数据目录，卸载或升级默认保留。<br/><br/><strong>免责声明：</strong><br/>本项目为第三方公开源码的非官方打包，与抖音及其关联方无任何关系；仅限个人非商业用途，使用者只能操作本人拥有或已获授权的账号，自动化操作可能触发平台风控（验证、限流、封禁等），风险自负。""",
        "categories": ["影音娱乐"],
        "icon_url": "https://raw.githubusercontent.com/JaxSupero/douyin-sparkflow-fpk/main/ICON_256.PNG",
        "readme_url": "https://raw.githubusercontent.com/JaxSupero/douyin-sparkflow-fpk/main/README.md",
        "bug_report_url": "https://github.com/JaxSupero/douyin-sparkflow-fpk/issues",
        "maintainer": "halfwaystudent",
        "maintainer_url": "https://github.com/halfwaystudent/douyin-sparkflow",
        "distributor": "JaxSupero",
        "distributor_url": "https://github.com/JaxSupero/douyin-sparkflow-fpk",
        "run_as": "package",
        "install_type": "",
        "is_docker": False,
        "service_port": "8787",
        "keep_versions": KEEP_VERSIONS,
    },
    {
        "app_name": "octop",
        "repo": "TencentCloud/Octop",
        "upstream": "https://github.com/TencentCloud/Octop",
        # Docker 版：包里只有 docker-compose，运行时按本机架构拉
        # ghcr.io/tencentcloud/octop:<ver>（manifest platform=all），x86/arm 共用一份。
        "arch_key": "all",
        "platform": ["all"],
        "display_name": "Octop",
        "desc": """腾讯开源的多用户多智能体 AI 助手平台，支持 Web 控制台、CLI、IM 渠道与定时任务，数据全部本地存储。<br/><br/><strong>安装后使用：</strong><br/>首次会从 ghcr.io 下载与本包版本对应的镜像（设备需能访问 GitHub Container Registry），请等到应用中心显示「运行中」再点「打开」，或访问 http://设备IP:8088，用安装向导里设置的账号登录。<br/>忘记密码可到数据目录 octop-login.txt 查看备份（仅记录安装或应用「设置」改密；网页改密后不会自动更新）。<br/><br/><strong>首次登录后：</strong><br/>请在控制台「设置 → 模型」配置 API Key，并按需配置 IM 渠道、远程桌面、浏览器等。<br/><br/>注意：Docker 版通过容器部署，会拉取 ghcr.io/tencentcloud/octop 上与本包版本相同的镜像。""",
        "categories": ["AI赋能", "系统工具"],
        "icon_url": "https://raw.githubusercontent.com/TencentCloud/Octop/main/fnos/docker/ICON_256.PNG",
        "readme_url": "https://raw.githubusercontent.com/TencentCloud/Octop/main/fnos/README.md",
        "bug_report_url": "https://github.com/TencentCloud/Octop/issues",
        "maintainer": "TencentCloud",
        "maintainer_url": "https://github.com/TencentCloud/Octop",
        "distributor": "TencentCloud",
        "distributor_url": "https://github.com/TencentCloud/Octop",
        "run_as": "package",   # config/privilege: defaults.run-as
        "install_type": "",
        "is_docker": True,
        "service_port": "8088",
        "keep_versions": KEEP_VERSIONS,
    },
    {
        "app_name": "octop-native",
        "repo": "TencentCloud/Octop",
        "upstream": "https://github.com/TencentCloud/Octop",
        # 本地版：复用飞牛「Python 3.12」，包内是各架构的 site-packages，
        # 因此 x86 与 ARM 是两个资产，同一个 appname（manifest 仅改 platform）。
        "arch_key": "x86",
        "arch_assets": [
            {"arch": "x86", "pattern": r"-fnos-native-(?!arm64)"},
            {"arch": "arm", "pattern": r"-fnos-native-arm64-"},
        ],
        "platform": ["x86", "arm"],
        "display_name": "Octop 本地版",
        "desc": """腾讯开源的多用户多智能体 AI 助手平台，支持 Web 控制台、CLI、IM 渠道与定时任务，数据全部本地存储。<br/><br/><strong>安装后使用：</strong><br/>请等到应用中心显示「运行中」再点「打开」，或访问 http://设备IP:8089，用安装向导里设置的用户名或邮箱登录。<br/>忘记密码时打开飞牛「文件管理」，进入应用共享里的 Octop 数据目录，查看 octop-login.txt（仅备份安装或应用「设置」改密；网页改密后不会自动更新）；也可在应用「设置」中改密。<br/><br/><strong>命令行管理：</strong><br/>支持官方 octop CLI（octop provider list / agent list / user list / skills / backup 等，详见 octop --help）。<br/><br/><strong>首次登录后：</strong><br/>请在控制台「设置 → 模型」配置 API Key，并按需配置 IM 渠道、远程桌面、浏览器等。<br/><br/>注意：本地版直接运行在飞牛主机上，安装前会自动关联安装「Python 3.12」开发工具。""",
        "categories": ["AI赋能", "系统工具"],
        "icon_url": "https://raw.githubusercontent.com/TencentCloud/Octop/main/fnos/native/ICON_256.PNG",
        "readme_url": "https://raw.githubusercontent.com/TencentCloud/Octop/main/fnos/README.md",
        "bug_report_url": "https://github.com/TencentCloud/Octop/issues",
        "maintainer": "TencentCloud",
        "maintainer_url": "https://github.com/TencentCloud/Octop",
        "distributor": "TencentCloud",
        "distributor_url": "https://github.com/TencentCloud/Octop",
        "run_as": "root",      # config/privilege: defaults.run-as（需要 sudo / 远程桌面）
        "install_type": "",
        "is_docker": False,
        "service_port": "8089",
        "keep_versions": KEEP_VERSIONS,
    },
]

# 兼容旧调用点（nas_sync.py / verify_schedule.py 曾直接读这些名字）
APP_NAME = APP_SPECS[0]["app_name"]
KNOWN_APP_NAMES = {s["app_name"] for s in APP_SPECS}


def app_spec(name):
    """按 appname 取 spec；不存在返回 None。"""
    for s in APP_SPECS:
        if s["app_name"] == name:
            return s
    return None


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


def gh_headers(url=None, extra=None):
    """按 URL 构造 GitHub 请求头。

    认证是**可选**的：设了 GITHUB_TOKEN（或 GH_TOKEN）就在**访问 api.github.com 时**
    带上 Authorization，没设也能跑，但落在匿名配额上——未认证只有 60 次/小时，
    而且该配额在所有 GitHub 托管 runner 的出口 IP 之间共享，多应用源每轮拉
    releases + assets 很容易偶发 `HTTP Error 403: rate limit exceeded`。
    带 token 是 5,000 次/小时（默认 github.token 即可读公开仓库的 Releases）。

    为什么不能无脑带：release 资产的 browser_download_url 是 github.com 域名，
    会 302 到带签名的 release-assets CDN。把 API 的 Authorization 一起送过去，
    两套凭据冲突，实测直接返回 **401 Unauthorized**（HEAD/GET 都一样）。
    所以只有 api.github.com 的 REST 调用才附加凭据，其余（含 icon/preview/readme
    的 raw 地址、FPK 下载）一律匿名。
    """
    h = {"User-Agent": "fndepot-gen"}
    host = urllib.parse.urlsplit(url).hostname if url else "api.github.com"
    if host == "api.github.com":
        h["Accept"] = "application/vnd.github+json"
        tok = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
        if tok:
            h["Authorization"] = f"Bearer {tok}"
    if extra:
        h.update(extra)
    return h


def http_json(url, timeout=30):
    req = urllib.request.Request(url, headers=gh_headers(url))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            limit = ""
            try:  # GitHub 把剩余额度与重置时间放在响应头里，读出来才有诊断价值
                limit = f"（{exc.headers.get('X-RateLimit-Remaining', '?')}"
                reset = exc.headers.get("X-RateLimit-Reset")
                if reset:
                    limit += f"，{int(reset) - int(datetime.now(timezone.utc).timestamp())}s 后重置"
                limit += "）"
            except Exception:
                pass
            print(
                f"::error::GitHub API 403（大概率是速率限流{limit}）。"
                f"请给本工作流设置 GITHUB_TOKEN 环境变量（env 或 secret）后重跑。",
                file=sys.stderr,
            )
        raise


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

    Octop 的 tag 形如 v1.0.3b1 —— "b1" 前面没有连字符，不是合法 SemVer 预发布号，
    也过不了 validate 的 `^\d+(\.\d+)*(-\S+)?$`。这里规范化成 1.0.3-b1：
    只影响索引里的版本键，下载地址/sha256/size 都不变。
    """
    v = tag.lstrip("v")
    m = re.fullmatch(r"(\d+(?:\.\d+)*)[bB](\d+)", v)
    if m:
        return f"{m.group(1)}-b{m.group(2)}"
    return v


def arch_entries(spec):
    """spec 声明的架构清单；老 spec 只有 arch_key，视为单一架构、按第一个 .fpk 取名。"""
    return spec.get("arch_assets") or [{"arch": spec["arch_key"]}]


def pick_packages(spec, rel):
    """
    按 arch_entries 从 release 资产里挑出每个架构的 FPK，返回 {arch: {url,sha256,size,...}}。

    为什么要按 pattern 挑：Octop 一个 release 里挂了三个 FPK
    （docker / native / native-arm64），而 "Octop-fnos-native-arm64-…" 也包含
    "fnos-native-"，所以 x86 的 pattern 用负向前查 (?-arm64) 排除掉 arm64 包；
    否则 x86 位会拿到 arm64 的字节，装错架构必然启动失败。
    """
    assets = rel.get("assets") or []
    picked = {}
    for ent in arch_entries(spec):
        arch = ent["arch"]
        pat = ent.get("pattern")
        cand = None
        for a in assets:
            if not a["name"].endswith(".fpk"):
                continue
            if pat and not re.search(pat, a["name"]):
                continue
            cand = a
            break
        if not cand:
            continue
        digest = (cand.get("digest") or "").replace("sha256:", "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            print(f"  ! {rel['tag_name']}/{cand['name']}：GitHub 未提供 sha256 digest，跳过该架构",
                  file=sys.stderr)
            continue
        picked[arch] = {
            "download_url": cand["browser_download_url"],
            "sha256": digest,
            "size": cand["size"],
            "asset_name": cand["name"],
            "asset_updated_at": cand.get("updated_at"),
        }
    return picked


def fetch_releases(spec, limit):
    releases = http_json(f"https://api.github.com/repos/{spec['repo']}/releases?per_page={max(limit, 30)}")
    primary = arch_entries(spec)[0]["arch"]
    out = []
    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        pkgs = pick_packages(spec, rel)
        if not pkgs:
            continue
        if primary not in pkgs:
            # 主架构缺失的版本不能收录：客户端会拿不到能装的包
            print(f"  ! 跳过 {rel['tag_name']}：缺少主架构 {primary} 的 FPK", file=sys.stderr)
            continue
        upd = max((p.get("asset_updated_at") or "" for p in pkgs.values()), default="") or None
        out.append(
            {
                "version": norm_version(rel["tag_name"], pkgs[primary]["asset_name"]),
                "tag": rel["tag_name"],
                "packages": pkgs,
                "updated_at": rel.get("published_at"),
                "asset_updated_at": upd,
                "changelog": (rel.get("body") or "").strip() or f"见 {rel['html_url']}",
                "html_url": rel["html_url"],
            }
        )
        if len(out) >= limit:
            break
    return out


def head_content_length(url, timeout=30):
    """取下载 URL（跟随 302 到 release-assets CDN）的 Content-Length。"""
    req = urllib.request.Request(url, method="HEAD", headers=gh_headers(url))
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


def report_drift(published, spec, rels):
    """同一个版本号下 size/sha256 变了 = 上游重传同名资产，必须显式告警。"""
    known = {}
    if published:
        app = (published.get("apps") or {}).get(spec["app_name"]) or {}
        for ver, r in (app.get("releases") or {}).items():
            archs = {}
            for arch, p in (r.get("packages") or {}).items():
                if p.get("sha256"):
                    archs[arch] = (p.get("size"), p["sha256"])
            if archs:
                known[ver] = archs
    for r in rels:
        prev = known.get(r["version"])
        if not prev:
            continue
        for arch, p in r["packages"].items():
            if arch not in prev:
                print(f"  [+] {spec['app_name']} {r['version']} 新增架构 {arch}（size {p['size']}）")
                continue
            old_size, old_sha = prev[arch]
            if old_sha != p["sha256"] or old_size != p["size"]:
                print(f"  [!] {spec['app_name']} 上游同 tag 重传 {r['version']}[{arch}]："
                      f"size {old_size} → {p['size']}，"
                      f"sha256 {str(old_sha)[:12]}… → {p['sha256'][:12]}…")


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
    skip_head = os.environ.get("FNDEPOT_SKIP_HEAD") == "1"
    if skip_head:
        print("  ! FNDEPOT_SKIP_HEAD=1：本轮只信任 API 的 size，不做 Content-Length 对账"
              "（仅限本机网络打不开 github.com 时排障用；CI/定时同步必须走完整对账）",
              file=sys.stderr)
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
        if skip_head:
            keep.append(r)
            continue
        clen, ok = None, False
        # 一个版本可能挂多个架构的包（Octop 一个 release 挂 3 个 FPK），
        # 任何一份对不上就跳过整个版本：装到不对的字节比少收录一个版本糟。
        bad = None
        for arch, pkg in r["packages"].items():
            clen = None
            ok = False
            for attempt in range(HEAD_RETRIES):
                try:
                    clen = head_content_length(pkg["download_url"])
                except Exception as e:
                    clen = None
                    if attempt == HEAD_RETRIES - 1:
                        bad = f"{r['version']}[{arch}]：HEAD 取不到 Content-Length（{e}）"
                        break
                if clen is not None and clen == pkg["size"]:
                    ok = True
                    break
                if attempt < HEAD_RETRIES - 1:
                    time.sleep(HEAD_RETRY_SLEEP)
            if not ok:
                if clen is not None:
                    bad = (f"{r['version']}[{arch}]：Content-Length({clen}) 与 "
                           f"API size({pkg['size']}) 不一致，疑似重传中")
                break
        if ok:
            keep.append(r)
        else:
            skipped.append(bad or f"{r['version']}：稳定性检查未通过")
    return keep, skipped


def build(entries, owner):
    """
    entries: [(spec, 稳定版本列表), ...]，每个 spec 生成一个 app 条目。

    架构键取自 spec["arch_key"]：hermes-studio 用 all（通用回退包），
    douyin_sparkflow 用 x86（包内是 x86-64 ELF，不给 arm 设备推荐）。
    """
    apps = {}
    for spec, rels in entries:
        releases = {}
        for r in rels:
            releases[r["version"]] = {
                "changelog": r["changelog"][:2000],
                "updated_at": r["updated_at"],
                "packages": {
                    arch: {
                        "download_url": p["download_url"],
                        "sha256": p["sha256"],
                        "size": p["size"],
                    }
                    for arch, p in r["packages"].items()
                },
            }
        app = {
            "display_name": spec["display_name"],
            "desc": spec["desc"],
            "platform": spec["platform"],
            "categories": spec["categories"],
            "icon_url": spec["icon_url"],
            "readme_url": spec["readme_url"],
            "bug_report_url": spec["bug_report_url"],
            "maintainer": spec["maintainer"],
            "maintainer_url": spec["maintainer_url"],
            "distributor": spec["distributor"],
            "distributor_url": spec["distributor_url"],
            "run_as": spec["run_as"],
            "install_type": spec["install_type"],
            "is_docker": spec["is_docker"],
            "service_port": spec["service_port"],
            "details_updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "releases": releases,
        }
        if spec.get("preview_urls"):
            app["preview_urls"] = spec["preview_urls"]
        apps[spec["app_name"]] = app
    return {
        "schema_version": "2",
        "source_info": {
            # 规范禁止把 "FnDepot" 用作源名/作者名，因此源名不含该词。
            "name": f"{owner} 的 {SOURCE_NAME_SUFFIX}",
            "author": owner,
            "homepage": f"https://github.com/{owner}/FnDepot",
            "description": "收录 " + "、".join(s["display_name"] for s, _ in entries)
                           + " 的 fnOS 原生 FPK 安装包。",
        },
        "apps": apps,
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
        if name not in KNOWN_APP_NAMES:
            warns.append(f"{name}: appname 不在 APP_SPECS 中（应为 {sorted(KNOWN_APP_NAMES)} 之一）")
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
    problems = []
    for name, app in doc["apps"].items():
        print(f"  —— {name} ——")
        for label, url in [("icon", app.get("icon_url", "")),
                           ("preview", (app.get("preview_urls") or [""])[0]),
                           ("readme", app.get("readme_url", ""))]:
            if not url:
                continue
            try:
                req = urllib.request.Request(url, method="HEAD", headers=gh_headers(url))
                with urllib.request.urlopen(req, timeout=20) as r:
                    print(f"  [{'OK' if r.status == 200 else '??'}] {label}: HTTP {r.status}")
            except Exception as e:
                problems.append(f"{name}.{label} 不可达: {e}")

        for ver, r in app["releases"].items():
            for arch, p in r["packages"].items():
                url = p["download_url"]
                try:
                    req = urllib.request.Request(url, method="HEAD", headers=gh_headers(url))
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        clen = resp.headers.get("Content-Length")
                        ranges = resp.headers.get("Accept-Ranges")
                        ok = clen and int(clen) == p["size"]
                        print(f"  [{'OK' if ok else '!!'}] {ver}.{arch}: size={p['size']} "
                              f"content-length={clen} ranges={ranges}")
                        if clen and not ok:
                            problems.append(f"{name}.{ver}.{arch}: size({p['size']}) "
                                            f"与 content-length({clen}) 不一致")
                except Exception as e:
                    problems.append(f"{name}.{ver}.{arch}: FPK 不可达 {e}")
                if deep:
                    # 边下边算，不把 330MB 读进内存；落盘到 TMPDIR，避免写 /tmp 撑爆内存盘
                    h = hashlib.sha256()
                    tmp = os.path.join(tempfile.gettempdir(), ".fndepot-verify.fpk")
                    try:
                        with urllib.request.urlopen(
                            urllib.request.Request(url, headers=gh_headers(url)), timeout=600
                        ) as resp, open(tmp, "wb") as f:
                            for chunk in iter(lambda: resp.read(1 << 20), b""):
                                h.update(chunk)
                                f.write(chunk)
                        got = h.hexdigest()
                        print(f"  [{'OK' if got == p['sha256'] else '!!'}] {ver}.{arch}: sha256 实测={got[:16]}…")
                        if got != p["sha256"]:
                            problems.append(f"{name}.{ver}.{arch}: sha256 不匹配！")
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
    published = load_published(args.out)

    entries = []
    for spec in APP_SPECS:
        name = spec["app_name"]
        print(f"\n=== {name}（{spec['repo']}）===")
        print(f"拉取 {spec['repo']} 最近 releases …")
        rels = fetch_releases(spec, spec["keep_versions"] + FETCH_EXTRA)
        if not rels:
            print(f"  跳过 {name}：没有可用 release（缺少 .fpk 资产或 sha256）", file=sys.stderr)
            continue

        print("漂移检查（与已发布索引比对）：")
        report_drift(published, spec, rels)

        print(f"稳定性检查（静默期 {args.settle} 分钟 + Content-Length 对账）：")
        stable, skipped = stabilize(rels, settle_minutes=args.settle)
        for s in skipped:
            print(f"  [跳过] {s}")
        if not stable:
            print(f"  跳过 {name}：所有版本都未通过稳定性检查", file=sys.stderr)
            continue
        stable = stable[:spec["keep_versions"]]
        print(f"  收录 {len(stable)} 个版本，最新 {stable[0]['version']}")
        entries.append((spec, stable))

    if not entries:
        print("错误：没有任何应用产出可用版本，保留原 fnpack.json 不动。", file=sys.stderr)
        return 1

    doc = build(entries, owner)

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
