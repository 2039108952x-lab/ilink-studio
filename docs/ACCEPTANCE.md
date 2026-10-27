# iLink Studio 0.1.0-rc1 · 验收与发布复核

> 状态：初始候选版，不等于 v1.0 正式发布。本文随每次构建更新证据，不把 CI 当作 LIVE/MANUAL 通过。

## 当前证据

- 自动回归：**868 项通过，2 项环境跳过**（2026-10-27 复核；`python -m unittest discover -s tests -t .`，57 秒）。
- 前端与桌面：`npm run typecheck` 无错、`npm test` 16 项通过、`npm run build:web` 成功。
- 网关：PyInstaller onedir（`dist/gateway/gateway.exe`），含静态控制台与固定版本 Python 依赖。
- 浏览器：Playwright 1.64.1 配套 Chromium 1248、Headless Shell 1248、FFmpeg 1013，602 个文件随包，不由首启下载。
- 发布目录：`release/win-unpacked/iLink Studio.exe`，整包约 1.2 GB（含随包浏览器）。
- 原始项目：不从 `D:\legacy-project` 自动复制真实配置、Cookie、token、聊天正文或 Playwright profile；本轮实测全部使用隔离数据目录。

## 账号级通道启用（本轮新增）

- 启动不再全量创建适配器；控制面起来后只恢复「界面里启用 + 当前活动通道」的账号。
- 启用前置校验：首启已确认 → 非 wxauto → 已登录绑定（`bound` 且 UID 非空）→ UID 与预期一致 → 官方/cookie 通道的凭据在保险库中。任一条不满足返回 409，文案里写清缺哪一条。
- 停止/重启只影响单个账号：先停旧适配器再从 Router 原子解绑，同平台其他账号不受影响。
- `starting` 不等于在线：只有适配器握手成功才由平台侧上报连接状态。
- 界面不靠灰按钮让人猜：启用按钮被禁用时，卡片里直接写原因（未登录并核对 UID、非活动通道、客户端不支持）。
- QQ 的扫码不在本应用内：NapCat 自己负责登录 QQ 并暴露 OneBot。卡片上的状态是**探测出来的**，不是写死的话术——读 `%LOCALAPPDATA%\NapCat\config\webui.json` 与 `onebot11_*.json` 拿真实端口，再探 TCP 判断跑没跑：
  - 没装：提示先安装 NapCat，不编造地址
  - 装了没跑：直接给出启动脚本绝对路径（如 `launcher-user.bat`）和「跑起来后 OneBot 会监听 ws://127.0.0.1:3001」
  - 在跑：提示点「只读探活」核对机器人 UID
- 「检查 NapCat」按钮会实时重跑上述探测并显示结果；它不等于登录，登录只发生在 NapCat 侧。
- 预期 UID 默认留空 = 首次登录/探活以实际 UID 为准；0.1.0-rc1 早期版本写死的开发者 UID 会在启动时从「从未绑定成功」的槽位清空。

## 打包陷阱（已修，别再踩）

`--collect-submodules gateway` 在生成 spec 时执行，那一刻项目根还没进 `sys.path`，PyInstaller 6.20 实测返回空列表。适配器是运行时按账号动态导入的，静态分析看不到，结果是发布版一启用通道就报「平台依赖没装齐」。

现在 `scripts/build_gateway.py` 用显式 `--hidden-import gateway.adapters.*` 登记，并在构建后读 `PYZ-00.toc` 校验；缺模块直接让构建失败。`tests/test_packaging.py` 保证新增平台时不会漏登记。

另外两条构建经验：

- `electron-builder --dir` 在没有外网时会卡在签名证书发现阶段（表现是长时间无输出）。加 `CSC_IDENTITY_AUTO_DISCOVERY=false` 即可跳过。
- 重新打包前必须先退出正在运行的 `iLink Studio.exe`，否则 `release/win-unpacked` 被占用，报 `EBUSY: rmdir`。

## NapCat 集成（软件内启动）

「启动 NapCat」按钮直接拉起 `NapCatWinBootMain.exe <QQ.exe> <hook.dll>`，两个路径都来自探测结果与注册表读取，不接受任何外部参数；不再要求用户去双击 `launcher-user.bat`。启动时复刻 launcher 设的环境变量（`NAPCAT_*`），并开独立控制台。

卡片状态是三态，都由实测端口决定：

| 探测结果 | 卡片文案 | 主按钮 |
| --- | --- | --- |
| OneBot 端口在监听 | NapCat 正在运行，点「只读探活」核对 UID | 检查 NapCat 状态 |
| 只有 WebUI 端口在监听 | NapCat 已启动，等 QQ 登录：在弹出的 QQ 窗口里扫码 | 等待 QQ 扫码（点此重查） |
| 两个都没有 | NapCat 已安装但没在运行 | 启动 NapCat |

实测（2026-10-26）：点击后 `NapCatWinBootMain.exe` 与 `QQ.exe` 起来，WebUI `127.0.0.1:6099` 立即可访问，OneBot `3001` 等 QQ 登录后才监听。启动会重启 QQ 并注入，按钮文案已写明这一点。

有一条坑必须记住：**判断「已在运行」要连 WebUI 端口一起看**。早先只看 OneBot 3001，QQ 没登录时 3001 永远不监听，于是点一次就再注入一个 QQ —— 实测真的开出两个 `NapCatWinBootMain.exe`。

### 扫码二维码直接显示在卡片里

NapCat 的登录二维码**只打在它自己的控制台**（`cache/qrcode.png` + 日志里一行「二维码解码URL」），用户根本看不到。现在：

- `GET /api/v1/napcat/qrcode` 读 `cache/qrcode.png`，超过 3 分钟的码不返回（免得给早就失效的图）
- 卡片在「NapCat 起来了但 QQ 未登录」时每 5 秒轮询一次，直接把二维码画在 QQ 卡片里，旁边附上可复制的解码链接
- 实测：点「启动 NapCat」→ 约 2 秒后卡片出现二维码 → 手机 QQ 扫码即可

## 谁才是「机器人账号」

白名单放行的是**给 bot 发消息的人**；要看到自动回复，必须由对方给 bot 本人发消息。

- 微信 bot：`ClawBot AI`
- B站 bot：`demo_user`（UID `10000003`）
- 抖音 bot：`10000005`
- QQ bot：`10000004`（NapCat 登录的号）

实测踩坑：在 B站私信里跟另一个账号聊天，一条消息都不会进网关 —— 直接查 B站服务端确认过，那段时间会话里最后一条是 bot 自己回的「在，咋了」，之后的新消息根本不在与 bot 的会话里。


默认策略是「默认拒绝」：`allowlist` 为空时所有入站消息只入库不回复，状态记为 `skipped / not_allowlisted`，正文不保存（只留审计元数据）。之前界面没有配置入口，等于自动回复永远开不了。

现在有两处入口：

- 账号页「自动回复白名单」面板：按平台填对方稳定 ID，已加入的以 chip 列出，可逐条移除。写入走 `POST/DELETE /api/v1/allowlist`，热生效，不用重启网关。
- 收件箱里对被拒的消息直接点「允许此人」，自动带上该消息发送者的稳定 ID。

判断依据只有稳定标识：`provisional`（只有昵称）永不自动回复；`opaque`（抖音会话标题这类）必须逐条显式列出。

白名单填错是真实踩过的坑，所以现在两头都兜：

- 输入会清洗：`UID:10000001`、全角空格、`uid：`/`mid:` 前缀都会去掉再存，避免「明明填了却不匹配」。
- 面板列出「最近来过」的候选（来自 `identity` 表，带消息条数），点一下就加入，抖音这种只有会话标题的平台不用猜该填什么。

## 工作区路径（曾让所有生成失败）

`brain.workspace` 默认值原写成 `data/im-workspace`，而解析基准本来就是数据目录，于是拼成 `<数据目录>/data/im-workspace`，CLI 一 `chdir` 就退出码 2，每条消息都记 `brain_unavailable`。

现在：默认值改为 `im-workspace`，`resolve_data_path` 会剥掉历史 `data/` 前缀，并且 `build_args` 在传 `--dir` 前把目录建出来。`tests/test_paths.py` 覆盖了这三条。

## 模型后端必须是 CLI

首启向导选的是「Reasonix 可执行文件」，要选命令行后端，不是桌面程序：

- 对：`%APPDATA%\npm\reasonix.cmd`（`npm i -g reasonix` 装出来的 CLI），或任何支持 `run --output-format json` 的 reasonix CLI
- 错：`C:\Program Files\Reasonix Studio\Reasonix Studio.exe` —— 那是 Electron 桌面程序，不认 `run` 参数，会被当成长跑进程直到 `brain.timeout` 超时，消息最终记为生成失败

## 验收步骤

1. 双击 `iLink Studio.exe`；应出现首启向导，不启用平台自动收发。
2. 在向导里选择本机 Reasonix 可执行文件、填模型名；勾选开机自启会真正写入本机登录启动项（非打包环境不写注册表）。
3. 各账号分别完成登录与机器人 UID 核对，再在账号卡片上单独点「启用自动收发」。
4. 完成首启后检查七区、日志、诊断导出、暂停回复、托盘隐藏与退出。
5. 强退 UI 后重启：原网关应继续存活，重接管后不出现第二网关或第二组轮询器。

## LIVE / MANUAL 复核表

| 项目 | 结果 | 证据/说明 |
| --- | --- | --- |
| 非项目 cwd 启动、无 Python/Node 依赖 | ✅ 通过 | 在 `C:\Windows\Temp` 下、PATH 只留 `C:\Windows\System32;C:\Windows` 启动，`/healthz` 返回 `{"ok":true,"version":"0.1.0-rc1"}` |
| 首启向导与七区 | ✅ 通过 | CDP 实测：向导可弹出、可选可执行文件、可填模型、四项确认、确认后写回设置；账号/收件箱/会话/刷抖音/记忆/表情包/日志七区均可渲染，前端无 console 错误 |
| UI 强退与网关重接管 | ✅ 通过 | 强杀 `iLink Studio.exe` 后 `gateway.exe` 仍存活且 `/healthz` 200；重启应用日志为 `ownedGateway=false`，`gateway.exe` 进程数仍为 1 |
| 主动退出停止网关 | ✅ 通过 | 经 UI 桥接调用退出后，应用与网关一并退出，网关日志 `gateway exit: code=0` |
| 启用/停用单账号通道 | ✅ 通过 | 发布版真机点击微信卡片「启用自动收发」→ `已启用 + 启动中`，日志出现 `微信 iLink 已启动（游标为空）`；点停用立即回到 `未启用` |
| QQ 卡片状态与禁用原因 | ✅ 通过 | 卡片显示探测结果：NapCat 装在 `%LOCALAPPDATA%\NapCat`、当前未运行、给出 `launcher-user.bat` 绝对路径与 `ws://127.0.0.1:3001`；未绑定时启用按钮旁直接写原因 |
| 托盘图标与「关窗口只隐藏」 | 待本人确认 | 纯 GUI 交互，工具侧无法点击；代码上关闭窗口会隐藏而非退出 |
| 微信 official 扫码、取消、过期、UID 核对 | 待本人扫码 | 不代填密码，不绕过验证 |
| QQ NapCat OneBot 握手 | 待 LIVE | 端口开放不算登录成功 |
| B站 Cookie 登录与续期 | 待 LIVE | 只重载 B站，不动其他通道 |
| 抖音通道（MCP 后端）登录态 | ✅ 通过 | 真机 `douyin_whoami`：nick=demo_account、uid=1000000000000001、http=200、breaker.open=false；家目录 `%LOCALAPPDATA%\iLink Studio\data\douyin-home` |
| 内容雷达刷一轮出日报 | ✅ 通过 | 真机 36.2 秒跑完：MCP 拉回 6 条候选 → 硬筛 → 2 条进模型判断 → 日报落 `data/reports/<日期>.md`；两条因把握 0.55 / 0.45 低于阈值 0.6 进「判断不了」，没有硬凑推荐 |
| 抖音失败文案（空家目录 / 未登录） | ✅ 通过 | 空家目录下 `douyin_whoami` 返回 `ok:false` +「还没有登录」+ 扫码提示，界面按 `not_logged_in` 显示，不静默失败 |
| 私信收发 / 评论回复（level=2 进楼）/ 分享卡片 | 待本人授权 | 这三项会真往账号发东西，需指定受控对象并当场同意；代码侧已走确认链（不带 `confirm` 只回预览），发送闸与 403 冷却有单测覆盖 |
| QQ ↔ 抖音双向 L2 真机演示 | 待 LIVE + MANUAL | 20 条样本人工评分至少 18/20；隔离负样本 0 泄露 |
| 表情 sent/degraded/failed/unknown | 待 LIVE | unknown 禁止自动重发 |
| 冷启动耗时 | ⚠️ 首次 8.24s，后续 1.22–1.24s | 5 次测量（隔离数据目录、独立端口）；首次为冷缓存，未达 5 秒目标，需在目标机复测 |
| 20 文字 + 10 图片性能 | 待目标机测量 | 热缓存；模型与平台故障单列 |
| 全新 Windows 用户 DPAPI 边界 | 待人工 | 不生成明文备选；旧凭据需重新登录 |

## 内容雷达（抖音 MCP 后端）

抖音通道换成了 `dsh-douyin-scroll` 自带的 MCP server（MIT，`vendor/dsh-douyin-scroll` @ `7c6e2a2`），
随包进 `resources/douyin-mcp`。协议与工具细节见 `docs/DOUYIN-MCP.md`。

**RPA 已退役，且不可恢复。** 换掉 RPA 是本轮目标之一，但删除实际发生在接手之前：
`gateway/adapters/douyin.py` 从 1199 行降到 340 行，`PlaywrightDriver` / `_ensure_dialog` /
`window_delta` / `WINDOW_MISMATCH_RESET` 全部不在。本工作区没有版本控制，`release.prev-*`
里只有 `gateway.exe` + `_internal`（无源码），所以原计划里「RPA 留作可切换 driver、
LIVE 全绿后再删」**无法执行**。实际结果是单一 MCP 通道，配 `mode = "rpa"` 不再有效。

后端限制（写清楚，免得后面有人以为它能干更多）：

| 限制 | 说明 |
| --- | --- |
| 读不到画面 | 没有 ASR / OCR / 抽帧。判断依据只有文案、标签、作者、时长、互动四件套与高赞评论 |
| 关键词搜索不可用 | 候选只来自推荐流，不能按关键词检索 |
| 推荐流每轮不同 | 同一时刻刷两次，候选可能完全不同；日报不可复现，验收标准是「能否稳定产出」而非「内容一致」 |
| 发送有闸 | 同通道 45 秒、跨通道 15 秒；撞 403 进 10 分钟冷却（写盘，跨重启生效），冷却中直接报 `rate_limited`，不自动重试 |
| 无法枚举私信会话 | `douyin_im_read` 必须指定会话（昵称或 sec_uid）；发现新会话只能靠 `douyin_notices` |
| 楼根 | 顶层评论靠 `douyin_notices` 的 `root_text` / `reply_to_nickname` 判（都为空 ⇒ 它自己就是楼根）；楼中楼没有楼根时仍交给 MCP 自动判，判不出返回 `need_root` 并**拒绝发送**（不瞎猜，猜错会变成一条冒出来的顶层评论）。2026-10-27 首条评论回复已 `sent` |
| Node 不随包 | 发布版要求系统 Node ≥ 22；缺失时界面明确报出路径与版本，不静默失败 |
| 没有 `douyin_remember` | 上游文档提到但工具不存在；记忆由我们自己写 `memory/dm-<safeKey>.json`（形状对齐上游 `lib/memory.mjs`） |

### B 站（已落地，不再是骨架）

`gateway/radar/bilibili.py` 现在是完整的 `RadarProvider` 实现，走内置 `bilibili-mcp` 的 34 个工具：

| RadarProvider | 实际怎么取 |
| --- | --- |
| `candidates()` | `video.get_ranking` / `video.get_region_ranking` / `search.search_by_type`；`keywords` 留空才退回推荐流 |
| `comments()` | `comment.get_comments`（含楼中楼）→ `CommentSnapshot(text, likes)` |
| `send_share()` | 私信或动态；复用 `adapters/bilibili.py` 的发送路径 |
| `send_text()` | 私信，本地先按 490 字上限拦（超了整条被拒，返回 21007） |
| `whoami()` | 现有 Cookie 登录态 |

判断层用的是**字幕原文**：`video.get_subtitle` 拿 CC 字幕当证据塞进 `build_prompt`，
没字幕的视频默认跳过，报告末尾写「因无字幕跳过 N 条」；开了本地转写才会补。

已由单元测试覆盖：`tests/test_radar_bilibili.py`、`test_radar_bili_duty.py`、
`test_bili_home.py`、`test_bili_mcp.py`、`test_radar_bili_settings.py`。
**登录后的真实工具返回仍缺真机证据**（本机尚未扫码），这一条不算验收通过。

## 图片与表情（2026-10-27 真机核对）

| 能力 | QQ | B站 | 微信 iLink | 抖音 |
| --- | --- | --- | --- | --- |
| 收图 | ✅ supported | ✅ supported（CDN url 直取） | ⚠️ degraded（占位符） | ⚠️ unknown（读不到） |
| 发图/表情 | ✅ supported | ✅ supported（PICTURE + msg_key） | ❌ unsupported | ❌ unsupported |

真机证据：
- QQ：`send_private_msg` 带 image 段 → `{"retcode":0,"data":{"message_id":760919656}}`
- B站：`send_msg(EventType.PICTURE, Picture.from_file(...))` → `{"msg_key":7701204858998340959}`
- 表情库：`D:/legacy-project/data/stickers` 33 张全部入库（格式校验 + sha256 去重 + 语义标签）
- 抖音发图放弃：`im-send.mjs` 只有 `Input.insertText`，且 headless 打开私信页只得到空壳（bodyLen=19），无法验证上传
- 微信发图放弃：协议要 AES-128-ECB + CDN 上传，代码里没有上传入口

## 发布边界

- 服务仅绑定数值 `127.0.0.1`，无公网映射或反向代理。
- 不静默安装软件、不代降级微信/NapCat、不要求管理员权限。
- 模型后端是外部必要依赖，未配置时明确显示未就绪（向导里必须选到可执行文件才能继续）。
- LIVE 只使用本人控制的小号与受控联系人；不会向陌生人自动发测试消息。
- 初始候选版不承诺 NapCat、Cookie、外部 MCP 后端或第三方平台的稳定性与账号安全。
- 网关是后台服务：UI 崩溃不会带走它，下次启动接管；持续无人接管会一直占用端口，可用托盘「退出」或结束 `gateway.exe` 停止。
