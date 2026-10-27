# 抖音 MCP 通道 · 实测事实

> 本文只写**亲手调出来的事实**，不抄上游 README。凡是照文档写、但实测不符的，都单列出来。
>
> 实测环境：2026-10-26，Windows 11，Node 24.16.0，Python 3.13.13，
> 家目录 `%LOCALAPPDATA%\iLink Studio\data\douyin-home`，
> 登录账号 `demo_account`（uid `1000000000000001`）。

## 1. 上游与版本

| 项 | 值 |
| --- | --- |
| 仓库 | `https://github.com/NEWJO123/dsh-douyin-scroll` |
| 本地位置 | `vendor/dsh-douyin-scroll`（已进 `.gitignore` 的 `vendor/`） |
| commit | `7c6e2a2ff3443eee89c4935c1304df5822b40e65`（2026-10-26 20:14） |
| 版本 | `0.5.4` |
| 许可 | MIT |
| Node 要求 | `>= 22` |

selfcheck 结果：**352 通过 / 5 失败**。5 个失败项全部落在计划内**不引入**的 DSH 宿主层：

- 4 条 `prompts/persona.md` 人设检查（抽象形容词、没有收敛、没说称呼边界、第一行缺身份牌）
- 1 条 `core.hooksPath` 不是 `.githooks`

这两块我们都不接（`cordis.patch.yml`、`client.js`、`.githooks` 全不要），所以不影响 MCP 工具链。

## 2. 怎么接进来

```bash
node <插件目录>/tools/mcp-server.mjs      # stdio，逐行 JSON-RPC
```

环境变量只有两个要紧的：

| 变量 | 作用 | 我们的取值 |
| --- | --- | --- |
| `DOUYIN_HOME` | Cookie / venv / 配置 / 报告的全家 | `%LOCALAPPDATA%\iLink Studio\data\douyin-home` |
| `DOUYIN_NODE` | 指定 node 二进制 | 不设，用系统 node |

家目录分配规则（`tools/_home.py`）：只认 `DOUYIN_HOME`，没有才退 `DSH_HOME/douyin-duty`、再退 `~/.dsh/douyin-duty`，**不写死盘符**。

Python 侧只需要 `curl_cffi` 与 `httpx`，装在 `douyin-home/.venv`（实测 `curl_cffi 0.17.0`、`httpx 0.28.1`）。本机已装好，`douyin_doctor` 报 `主依赖 venv: ok`。

## 3. 协议层（写客户端必须知道）

### 3.1 帧格式

**逐行 JSON**，一行一条消息，没有 `Content-Length` 分帧。stdout 只写协议，日志走 stderr。

### 3.2 方法支持矩阵

| 方法 | 实测行为 |
| --- | --- |
| `initialize` | 支持 `2025-06-18` / `2025-03-26` / `2024-11-05`；其他值回落 `2025-06-18` |
| `notifications/initialized` | 无响应（正确） |
| `notifications/cancelled` | 无响应 |
| `ping` | 返回 `{}` |
| `tools/list` | 13 个工具 |
| `tools/call` | 见下 |
| `server/discover` | **`-32601 Method not found`** |
| 未知工具 | `-32602 Unknown tool: <name>` |
| 未知方法 | `-32601 Method not found: <method>` |

`server/discover` 是被故意拒掉的（源码注释：明确回"没有" ⇒ 新客户端自动回落 legacy initialize）。**客户端不能依赖 discover 探活，直接 initialize。**

### 3.3 错误分层（最容易踩）

业务失败**不走 JSON-RPC error**：

```
tools/call → result.content[0].text = '{"ok": false, "error": "..."}'
             result.isError = true        # 仅当返回对象 ok === false
```

也就是说要判两次：

1. 协议层：`error` 字段存在 ⇒ 方法/工具名错
2. 业务层：`content[0].text` 反序列化后 `ok === false` ⇒ 业务失败，读 `error` / `hint` / `stderr`

`content[0].text` 永远是**字符串**，必须二次 `JSON.parse`（`douyin_guide` 的 `doc` 是纯 Markdown，解析会失败，要按原文处理）。

## 4. 13 个工具逐个实测

### 4.1 只读（9 个，随便调）

| 工具 | 参数 | 实测耗时 | 关键返回 |
| --- | --- | --- | --- |
| `douyin_guide` | 无 | 38ms | `doc`：`AI-USAGE.md` 全文（Markdown） |
| `douyin_status` | 无 | 36ms | `loggedIn` / `venvReady` / `perDay` / `pickedToday` / `lastRunAt` / `report` / `hasReport` |
| `douyin_doctor` | 无 | 184ms | `items[]`：家目录、Node、Python、venv、Cookie、浏览器…每项 `state` + `fix` |
| `douyin_whoami` | 无 | 525ms | `logged_in` / `alive` / `nick` / `uid` / `http` / `breaker.open` |
| `douyin_forward` | `top`(1–30，默认10) | 数秒 | `picked` / `sources` / `dropped` / `list[]` |
| `douyin_comments` | `aweme_id`（必填） | 8.5s | `count` / `comments[]`（**文本行，不是 JSON**） |
| `douyin_notices` | `raw`(bool) | 7.5s | `counts`（按 type 分组）+ `items[]` |
| `douyin_at_me` | `limit`(1–30)、`include_seen` | 7.4s | `at_n` + `list[]` |
| `douyin_im_read` | `to` / `to_id` / `limit`(1–60) | 37ms（缺参） | `messages[]`（带 `from: me/them/unknown`）+ `context` 记忆块 |

`douyin_forward` 每条候选的完整字段：

```json
{
  "aweme_id": "7699857912785187043",
  "desc": "接受，是每个成年人的必修课……",
  "author": "Tao.",
  "duration_s": 29.2,
  "age_h": 73.4,
  "digg": 232226,
  "comment": 1593,
  "collect": 30055,
  "share": 63431,
  "share_url": "https://www.iesdouyin.com/share/video/<id>/?...&share_sign=...",
  "web_url": "https://www.douyin.com/video/<id>"
}
```

`douyin_comments` 的返回是**纯文本行**，得自己解析：

```
cid=7700255535778972442  Marco  [接受自己的平庸[戳手手]]  赞29  1792855455
└ reply cid=7700023712707904256 Tao. [yes[流泪]]  1792801478
```

正则建议：`^cid=(\d+)\s+(.+?)\s+\[(.*)\]\s+赞(\d+)\s+(\d+)$`；楼中楼前缀固定 `└ reply cid=`，且**没有赞数**字段。

`douyin_notices` 每条 item：

```json
{
  "type": 45, "type_name": "@了你（评论区）",
  "nid": 7605855298846688000, "create_time": 1770876185,
  "nickname": "demo_friend", "uid": "1000000000000002",
  "content": "@demo_account",
  "aweme_id": "7605469750864635883",
  "comment_id": "7605855263023711025",
  "aweme_desc": "...",
  "root_author": "", "root_text": "",
  "reply_to_nickname": "", "reply_to_text": "",
  "video_url": "https://www.douyin.com/video/<id>",
  "comment_url": "https://www.douyin.com/video/<id>?comment_id=<cid>"
}
```

### 4.2 会发东西（4 个，**必须 `confirm: true`**）

| 工具 | 必填 | 可选 | 无 `confirm` 时的返回 |
| --- | --- | --- | --- |
| `douyin_share_card` | 目标（`to` 或 `to_id`，或配置 `dmTo`/`dmToId`） | `aweme_id`、`top`(1–3)、`reason`(≤300)、`reason_delay_sec`(默认5)、`dry` | `needsConfirm:true` + `preview[]`（aweme_id/desc/author/share_url） |
| `douyin_dm` | `text` + 目标 | `dry` | `needsConfirm:true` + `draft` + `len` |
| `douyin_comment_reply` | `aweme_id`、`text`(≤500) | `comment_id`（留空=顶层）、`root_comment_id` | `needsConfirm:true` + `draft` + `how` |
| `douyin_comment_mention` | `aweme_id`、`name`、`text`(≤500) | `dry` | `needsConfirm:true` + `draft`（自动加 `@name` 前缀） |

四个都实测了无 `confirm` 的预览路径，返回正确、**均未发送**。

## 5. 五个关键未知的答案

### ① `douyin_im_read` 能不能发现**新**会话

**不能。** 它是"读指定的某一个会话"，`to` / `to_id` 必须给（或落到配置 `dmTo`/`dmToId`），没有"列出所有会话"的能力。

实测：`douyin_im_read {}` → `{"ok": false, "error": "不知道读谁的会话"}`

> **对我们的硬约束**：收私信不能靠轮询会话列表，只能靠 `douyin_notices` 发现新消息，再用 `im_read` 按昵称/uid 拉正文。

### ② `douyin_share_card` 的收件人怎么指定

| 参数 | 语义 |
| --- | --- |
| `to` | 好友昵称，**只用于显示和搜索** |
| `to_id` | 好友稳定 id（sec_uid），**重名时必须给** |
| 都不给 | 回落配置 `dmTo` / `dmToId` |

没有"从会话列表里选一个"的接口 —— 目标得由我们这边提供（界面选会话或手填昵称）。

### ③ `douyin_forward` 有没有可直接跳转的链接

**有两个**：

- `share_url` — `https://www.iesdouyin.com/share/video/<id>/?...&share_sign=...`，带签名可直接打开
- `web_url` — `https://www.douyin.com/video/<id>`，干净、无签名、适合放进界面

### ④ `douyin_at_me` 的 `comment_id` / `root_comment_id` 稳不稳

- `comment_id`：**稳定可用**。19 位数字字符串，来源是通知 `schema_url` 里的 `cid=` 参数。
- `root_comment_id`：**拿不到**。`at_me` 的 `list[]` 只有 `nickname`/`content`/`aweme_id`/`comment_id`/`video_url`；`notices` 虽然有 `root_author`/`root_text`/`reply_to_nickname`/`reply_to_text` 四个字段，实测**全是空字符串**。

> `AI-USAGE.md` 里写"楼根从 `douyin_notices` / `douyin_at_me` 的返回里直接拿"—— **与实测不符**。
>
> 结果是：回楼中楼时只能依赖 `comment-api.mjs` 的自动判定（翻顶层列表 → 找楼中楼 → 问接口），
> 三条路都判不出来就返回 `need_root: true` 并**拒绝发送**（不会瞎猜）。
> 我们的界面必须能显示这个状态，不能自动重试。

### ⑤ 装依赖要多久

本机在接入前已装好（`douyin-home/.venv`，2026-10-26 22:12），精确耗时无法回溯。
现值：`curl_cffi 0.17.0` + `httpx 0.28.1` + `cffi 2.1.1` + `certifi 2026.10.23`，`doctor` 报 venv ok。
**结论：本轮不需要再装。**

## 6. 对我们设计的直接影响

1. **入站只能靠 notices 驱动**：私信发现不了新会话，必须以 `douyin_notices`（以及 `at_me`）作为唯一入口，`im_read` 只用于按需拉正文。
2. **发送闸上游已有**：api 通道 45 秒、ui 通道 20 秒、跨通道 15 秒；撞 403 进 10 分钟冷却并返回 `rate_limited`。我们不要在客户端再叠一层更严的闸，但**必须把冷却状态显示出来**、且不自动重试。
3. **不做回读核实**：MCP 版发完只报"发出去了"。评论回复例外 —— `comment-api.mjs` 会返回 `placement`，`level=2` 才算真进楼，`level=1` 说明楼根填错（这条要透到界面）。
4. **不做日报**：上游 MCP 版明确不提供「跑一轮 → 写日报」，日报是我们自己的 `data/reports/<date>.md`。
5. **没有 `douyin_remember`**：`AI-USAGE.md` 提到它，但 13 个工具里没有、源码 `TOOLS` 数组也没有。记忆只能靠 `im_read` / `dm` / `share_card` / `comment_reply` 自动入库，**不要按文档去调它**。
6. **`comment_reply` 没有 `mode` 字段**：`AI-USAGE.md` 大量写 `mode:"api"` / `mode:"ui"`，但 `inputSchema` 的 properties 里没有 `mode`，且 `additionalProperties: false`。传了就只是被忽略。真 @ 要用 `douyin_comment_mention`。

## 7. 陷阱清单

| 现象 | 真相 |
| --- | --- |
| `server/discover` 报 -32601 | 故意的，用 `initialize` |
| 工具失败但 JSON-RPC 没 error | 业务错误在 `content[0].text` 里，看 `isError` + `ok` |
| `comments` 解析不出结构化字段 | 它是给人看的文本行，自己正则 |
| `at_me` 想拿楼根 | 拿不到，只能靠自动判或 `need_root` |
| 想调 `douyin_remember` | 不存在，别写进代码 |
| `comment_reply` 传 `mode` | schema 不认，静默忽略 |
| 以为 MCP 版能出日报 | 不出，日报在我们这边 |
| `Guide.MD` 说能读画面 | 不能，无 ASR/OCR，只有文案/标签/作者/时长/互动/高赞评论 |

## 8. 接入后的实现事实（2026-10-26 完成）

| 位置 | 干什么 |
| --- | --- |
| `gateway/mcp_client.py` | 通用 stdio JSON-RPC 客户端：逐调用超时杀进程、崩溃按重启上限恢复、错误压缩成 code + hint、日志只记工具名与耗时 |
| `gateway/douyin_mcp.py` | 抖音特有部分：`DOUYIN_HOME`、随包优先的 `mcp-server.mjs` 查找 |
| `gateway/radar/douyin.py` | 13 个工具封装成 `RadarProvider`；另加 `remember()` 直写上游记忆文件 |
| `gateway/radar/gate.py` | 发送闸：同通道 45s / 跨通道 15s / 403 冷却 600s 写盘 |
| `gateway/radar_service.py` | 服务层：把同步 HTTP 调用桥回网关主 loop（MCP 子进程绑在那个 loop 上，不能每次 `asyncio.run`） |

真机实测（2026-10-26，账号 demo_account，`top=6`）：

- 一轮 **36.2 秒**跑完，拉回 6 条候选、硬筛掉 0 条；
- 2 条送进模型判断，理由引用了具体证据（「转发 234.6 万，达到点赞 275.3 万的 85%」）；
- 两条都因把握 0.55 / 0.45 低于阈值 0.6 归入「判断不了」——**证据不足不推**在真机上按预期工作，没有硬凑推荐；
- 日报写入 `data/reports/<日期>.md`，分「值得看」与「判断不了（带卡在哪）」两区。

失败路径同样实测：空家目录（无 Cookie）下 `douyin_whoami` 返回 `ok:false` +「还没有登录」+ 扫码提示，
分类为 `not_logged_in`，界面不会把它显示成「正常」。
