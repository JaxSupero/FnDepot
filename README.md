# FnDepot 应用源 · Hermes Studio

符合 [FnDepot 外部应用源 V2 规范](https://github.com/EWEDLCM/FnDepot/blob/main/README.md) 的第三方应用源，
收录 **Hermes Studio** 在 fnOS（飞牛）上的原生 FPK 安装包。

## 在客户端中添加本源

FnDepot 客户端版本需 **> 0.0.7**（V1 兼容正在逐步取消）。在本页右上角 **Code → Copy raw file content**，
或直接填仓库根地址：

```text
https://github.com/<你的用户名>/FnDepot
```

客户端会自动读取默认分支根目录下名为 `fnpack.json` 的文件（**文件名大小写必须完全一致**，客户端不探测其他索引名）。

## 收录内容

| 应用 | appname | 架构 | 打包者 | 上游 |
|---|---|---|---|---|
| Hermes Studio | `hermes-studio` | x86 | [veenyi](https://github.com/veenyi/Fnos-Hermes-Studio) | [EKKOLearnAI/hermes-studio](https://github.com/EKKOLearnAI/hermes-studio) |

本仓库不托管任何 FPK 文件，只提供索引。安装包直接从 veenyi 的 GitHub Releases 下载，
因此包体（约 330MB/版本）不会占本仓库空间。

### 关于架构

veenyi 的每个 Release 只发布**一个** FPK，文件名不带架构后缀；其 README 说明原生
`node-pty` 模块绑定 **Linux x64 / Node 24 ABI**，打包脚本中也没有 arm 构建逻辑。

因此本源的 `platform` 声明为 `["x86"]`，安装包放在 `packages.all`（V2 中作为通用回退包）。
这与上游 manifest 里的 `platform = all` 存在**有意差异**，目的是避免 arm64 设备
装得上却启动失败。若上游后续提供 arm 构建，将在此处补充对应的 `packages.arm`。

## fnpack.json 为什么是自动生成的

veenyi/Fnos-Hermes-Studio 配置了 `auto-update.yml`，**每天自动发版**。由于 FnDepot 客户端
在 `sha256` 存在时会**强制校验**，手动维护的索引必然在数天内失效。

本仓库的 `fnpack.json` 由 `gen_fnpack.py` 生成，并通过 GitHub Actions 每 20 分钟同步一次：

```bash
python3 gen_fnpack.py --verify        # 生成 + 联网校验
python3 gen_fnpack.py --verify --deep # 额外实际下载核对 sha256（较慢）
```

脚本的行为要点：

- 从 GitHub Releases API 抓取最近版本，保留最近 10 个
- **跳过没有 sha256 digest 的 Release** —— 填入错误的哈希比不填更危险
- 校验分类白名单、架构键白名单、版本号可比较性、`size` 为整数等规范要求
- 若 `source_info.name` / `author` 中出现 "FnDepot" 字样会**直接报错**（规范明令禁止，避免与官方混淆）
- **同 tag 重传检测**：与已发布的 `fnpack.json` 比对，同一版本号下 `size` / `sha256` 变化会显式告警

源作者用户名通过 `FNDEPOT_OWNER` 环境变量注入，CI 中由 `${{ github.repository_owner }}` 提供，
本地可用 `--owner` 覆盖。

## 「下载大小不完整」故障与对策

真实遇到过一次的错误：

```text
[App] 下载安装包失败: Hermes Studio | 应用=hermes-studio | 原因=下载大小不完整: expected=343501170 actual=343503444
```

**成因**：veenyi 在**同一个 tag（`v0.7.25-1`）下重传了同名 FPK**。GitHub Release 的 tag 与版本号不变，
但资产字节数从 `343501170` 变成 `343503444`，sha256 也随之改变。索引仍写着旧的 `size`，
客户端下载完整后按旧 `size` 判定，直接拒绝安装。

值得注意的是，重传也可能让**索引里的 size 偏大**——若索引先于 CDN 刷新就收录了 API 元数据，
用户会看到 expected 大于 actual。因此两边都必须防。

对策有三层：

1. **静默期（`--settle`，默认 20 分钟）**：资产刚被替换的版本暂不收录，等 CDN 内容刷新完再入库。
2. **Content-Length 对账**：写盘前用 HEAD 跟随 302 取 `Content-Length`，与 API 的 `size` 反复核对
   （3 次重试、间隔 3 秒）；不一致则跳过该版本，**宁可少收录一个版本，也不让用户装不上**。
3. **同步频率 20 分钟**：GitHub Actions 的 `cron: "13,33,53 * * * *"`，把重传窗口压到最小。

```bash
python3 gen_fnpack.py --settle 0   # 关闭静默期（仅本地排障用）
```

每次运行会打印跳过原因，例如：

```text
[!] 上游同 tag 重传 0.7.25-1：size 343501170 → 343503444，sha256 ce62015f7646… → 9f44e3a1ef13…
[跳过] 0.7.26-1：资产 7 分钟前刚被替换，等待 20 分钟静默期
```

## 定时任务不触发？（踩过的坑）

上线当天遇到的现象：**`sync-upstream.yml` 一次定时运行都没有**，但手动 `workflow_dispatch` 跑得好好的。

排查结论：**`schedule` 从未被 GitHub 注册过**。判据不是 API 里的 `state` —— 它一直是 `active`，
而 `active` 只表示"没有被手动停用"，**不代表定时已生效**。真正的判据是：

```text
GET /repos/{owner}/{repo}/actions/runs?event=schedule   →  total_count = 0
```

当时的实测证据链：

| 检查项 | 结果 |
|---|---|
| 文件位置 `.github/workflows/sync-upstream.yml` | ✅ 正确 |
| 默认分支 `main` | ✅ 正确 |
| 文件字节（无 BOM / 无 CRLF / 无控制字符） | ✅ 干净 |
| 本地与远端 md5 | ✅ 完全一致 |
| 工作流 `state` | `active` |
| Actions 平台状态 | All Systems Operational |
| `event=schedule` 运行数 | ❌ **0**（16 个档位全数错过） |

**处置办法**（GitHub 文档：*改动 cron 表达式的那次提交会（重新）激活定时工作流*）：

1. 在网页上编辑 `.github/workflows/sync-upstream.yml`，**改一下 cron 的分钟数**（换个值即可）并提交；
2. 或到 Actions → 该工作流 → ⋯ → **Disable workflow**，再 **Enable workflow**；
3. 之后确认下一个档位真的触发了。

**自查脚本**（同时检查"定时是否注册"和"索引是否过期"）：

```bash
python3 verify_schedule.py
```

另外要知道 GitHub 定时的两个固有局限，别指望它像 cron 那样准：

- **尽力而为**：负载高时排队、延迟甚至丢弃，整点前后尤其明显（所以示例避开整点）；
- **公库 60 天无活动会被自动停用**：源仓库只有工作流自己在提交，若上游长期不发版，
  定时可能被 GitHub 停掉，需要重新激活。

## 目录结构

```text
FnDepot/
├── fnpack.json                          # 应用源索引（自动生成，勿手工编辑）
├── gen_fnpack.py                        # 生成 + 校验脚本
└── .github/workflows/sync-upstream.yml  # 每 20 分钟同步上游发版
```

## 规范遵循说明

- `schema_version` 为字符串 `"2"`
- `apps` 的键 `hermes-studio` 与 FPK manifest 中的 `appname` 完全一致（区分大小写）
- `categories` 取自九个固定分类，且不超过两个
- 每个安装包均提供 `download_url`、`size`（字节整数）与 `sha256`
- `source_info` 未使用 "FnDepot" 作为源名或作者名；`maintainer` 与 `distributor` 分别标注上游开发者与打包者

## 免责声明

本源由用户自行添加，仅在用户本地客户端中生效。FnDepot 官方不审核、不担保外部源中
应用代码、安装包的安全性或稳定性。

Hermes Studio FPK 由第三方打包，以 `package` 身份（系统用户 `hermes-studio`）运行。
安装前请自行评估，并仅从你信任的来源获取安装包。
