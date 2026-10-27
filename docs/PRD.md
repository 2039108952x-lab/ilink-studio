# PRD · iLink Studio 多平台 AI 聊天集成台

| 项    | 值                                                                        |
| ---- | ------------------------------------------------------------------------ |
| 版本   | v0.2 · 实施基线                                                              |
| 日期   | 2026-10-25                                                               |
| 状态   | 已拍板决策不变；需求、数据约束、接口边界与验收口径补齐，可按里程碑实施                                      |
| 代码基线 | `D:\legacy-project`；`gateway/` 17 个 Python 文件、4,453 行；306 项旧网关测试通过，2 项跳过           |
| 目标平台 | Windows 11 x64 单机、单用户、仅本机访问                                              |
| 一句话  | 把 QQ / 微信 / B站 / 抖音的小号接入同一个桌面软件，共用登录入口、消息库、人格、记忆和表情包，让已确认是同一个人的跨平台聊天接得上。 |

> 本文是产品与工程验收的共同基线。「现状」只描述本次代码审计得到的证据，不把代码完成等同于真机可用。「目标」均为待实现能力，不能写成已完成。

---

## 0. 阅读约定

### 0.1 需求用语

- **必须 / 不得**：发布门槛；不满足即对应需求未通过。
- **应当**：默认实现；偏离时必须在变更记录写明原因和替代验收。
- **可以**：可选能力，不影响主流程。
- **已验证**：有明确的代码、测试或真机证据；**待验证**：只有实现路径，尚无闭环证据。

### 0.2 优先级与发布关系

| 优先级 | 含义                                             |
| --- | ---------------------------------------------- |
| P0  | v1.0 发布阻断项：安全、登录、消息库、L1+L2、基础行为控制、桌面壳与打包       |
| P1  | v1.0 计划交付项：完整表情包、长期事实、预算界面等；延期必须显式改版本范围，不能静默消失 |
| P2  | v1.0 之后的增强，不进入本次验收                             |

M0～M5 是施工顺序，不代表每个 M 都是一个正式版本。M4 产出可测试安装目录，**M5 验收完成后才发布 v1.0**。

### 0.3 不可混淆的四个对象

| 对象         | 含义                             | 示例                                          |
| ---------- | ------------------------------ | ------------------------------------------- |
| `account`  | 软件里的一个平台接入槽位及其登录状态             | `wechat + official`、`wechat + wxauto` 是两个槽位 |
| `identity` | 平台上的联系人或机器人账号标识                | QQ 号、B站 UID、微信返回的用户 ID                      |
| `person`   | 本软件确认的「现实中的同一个人」               | 本人的 QQ 主号与 B站主号经手动绑定后指向同一 `person`          |
| `thread`   | **记忆隔离边界**，不是平台名称，也不是简单的聊天列表分组 | 私聊线程、某个群、某个评论分支                             |

`person` 不能靠昵称自动推断；`account` 不能直接当 `person`；`thread` 不能随平台切换而无条件合并。

---

## 1. 产品目标与范围

### 1.1 核心目标

| 编号 | 目标                            | v1.0 验收口径                                                        |
| -- | ----------------------------- | ---------------------------------------------------------------- |
| G1 | 一个窗口管理四个平台的登录、续期与状态           | 在满足外部前置条件的通道上，用户不用命令行完成一次登录或续期；不支持的通道明确报原因，不伪造「在线」               |
| G2 | 一个 SQLite 业务库保存消息、会话、记忆与表情元数据 | 四个平台中被产品接纳的消息都能统一筛选、查看、检索；非白名单正文按隐私规则不落库                         |
| G3 | 同一 `person` 的跨平台人格与上下文一致      | 固定的 20 条 QQ ↔ 抖音 L2 样本至少答对 18 条；未绑定身份的隔离样本不得串记忆                  |
| G4 | 一套表情库按平台能力发图或显式降级             | 同一表情引用在各平台得到 `sent / degraded / failed / unknown` 中明确的一种结果，不静默丢图 |
| G5 | 可双击启动、托盘常驻、可选开机自启、无控制台黑框      | 目标机热缓存条件下，连续 5 次冷启动均在 5 秒内出现可操作主界面；平台异步上线                        |
| G6 | 故障可见、数据可恢复、隐私有边界              | 重启不重放已知历史；发送结果不明时不自动重发；日志和导出不得泄露凭据                               |

### 1.2 v1 明确不做

- 不做多用户、多租户、权限组织、手机端、公网控制台、端口映射、反向代理或内网穿透。
- 不做直播弹幕；`ChatKind.ROOM` 仅保留兼容位。
- 不做 L3 消息桥接：不把 QQ 收到的内容自动转发到抖音或其他平台，也不让 AI 自行决定冒充本人向哪个联系人发言。
- 不做微信群聊；iLink 仍按现有一对一行为处理，不猜测群协议。
- 不自动合并「疑似同一个人」，不把群聊或评论区记忆带进私聊。
- 不代用户安装、降级微信或 NapCat，不代填平台密码，不绕过验证码、滑块或人工风控。
- 不承诺抖音 RPA、NapCat、B站 cookie 或 `wxauto` 的稳定性与账号安全；不承诺平台消息 exactly-once。
- 不做向量数据库、通用知识库 RAG、自动人物关系图谱或长期事实的自动无条件信任。
- 不把「主窗口可打开」包装成「所有平台都能收发」。外部依赖未满足时必须分开展示。

### 1.3 外部前置条件

| 依赖                  | v1 处理方式                                                                                 |
| ------------------- | --------------------------------------------------------------------------------------- |
| Reasonix CLI / 模型后端 | 外部必要依赖，不由安装包静默捆绑；首启检测并允许配置可执行文件与模型，或提供已验证的 HTTP 后端。未配置时界面、消息库仍可用，AI 状态显示「未就绪」，不得使用假模型冒充 |
| QQ NT + NapCat      | 由本人安装和配置；软件可检测并拉起已确认路径，不负责安装和账号密码                                                       |
| 微信 `wxauto`         | 仅支持符合其要求的微信 3.9.x；本机已核实为 `4.1.15.56`，当前必须提示「不支持」；是否降级由本人另行决定                            |
| Playwright Chromium | 开发环境复用匹配版本；打包时固定驱动与浏览器版本并纳入发布物，不在首启静默下载未知程序                                             |
| 平台账号                | 机器人使用小号；主号仅作为操作者/联系人。抖音等 RPA 通道仍可能需要本人扫码或人工验证                                           |

---

## 2. 当前基线与差距

### 2.1 当前链路

```text
QQ小号 ── NapCat / OneBot 11 ───────┐
微信   ── iLink（默认）/ wxauto ────┤
B站    ── cookie + 私信/通知轮询 ───┼─► Python 网关 127.0.0.1:8787
抖音   ── Playwright RPA ───────────┘          │
                                               ├─► Reasonix CLI（stdin prompt）
                                               └─► MCP server / 主动发送
```

### 2.2 能力证据表

以下沿用 README 与代码中较保守的证据口径；本次只跑旧测试，没有重新执行四平台真机收发。

| 平台 | 通道                              | 当前证据                                | 明确限制                                          |
| -- | ------------------------------- | ----------------------------------- | --------------------------------------------- |
| QQ | `ws://127.0.0.1:3001`，OneBot 11 | 私聊/群聊文字链路已接通；图片与表情有代码和单测            | 图片与表情仍需在 M3 做真机复核；NapCat 是外部前置条件              |
| 微信 | `official = iLink`              | 扫码、长轮询、带 `context_token` 的文字回复有闭环记录 | 当前媒体主要是占位符；图片发送未实现；无历史拉取接口；不支持本产品的微信群聊        |
| 微信 | `wxauto`                        | 保留 `build()` 分派和旧适配器                | 依赖微信 3.9.x；本机 4.x 不可作为可用通道验收；昵称不是稳定 UID       |
| B站 | SESSDATA 等 cookie               | 私信、@、评论回复有真机闭环记录；有通知去重文件            | 小号权限、验证码、cookie 失效会影响实际收发；发送自测必须发给本人确认的目标     |
| 抖音 | Playwright 持久化 profile          | DOM 已探测，收发代码存在，有首轮跳过与指纹去重           | 没有可重复的端到端真机验收；风控与 DOM 改版敏感；昵称 + 正文指纹不足以证明全局唯一 |

### 2.3 本 PRD 要解决的差距

| 编号 | 当前问题                                                     | 目标改动                                                        |
| -- | -------------------------------------------------------- | ----------------------------------------------------------- |
| H1 | 登录散落在 `wx_login.py`、`bili_login.py`、RPA profile 与 NapCat | 共用 provider、登录会话状态机、加密保险库、界面与单通道热重载                         |
| H2 | `session_key = platform:kind:chat_id` 按平台割裂              | `person / identity / thread / conversation` 分层，显式绑定后共享私聊上下文 |
| H3 | `sessions.json` 仅保留最近 12 轮                               | SQLite 全量业务库、迁移、分页、检索、恢复与审计                                 |
| H4 | 表情靠文件名与目录约定                                              | 表情元数据、多标签、稳定 ID、能力矩阵与可见降级                                   |
| H5 | 内存 `_seen` 与平台各自游标并存，重启语义不统一                             | 持久去重键、检查点、首轮登记、处理租约和发送不确定态                                  |
| H6 | 登录态、连接态和发送能力混为一个「在线」                                     | 三种状态分别采集与展示，避免「绿灯但发不出去」                                     |
| H7 | 本地 HTTP 仅有简单 Bearer 校验，Web 化后攻击面增大                       | 严格 origin/host、每会话凭据、CSP、Electron 隔离与异步 outbox API          |
| H8 | 「单进程 GUI + 网关」与「UI 崩溃不影响网关」互相矛盾                          | Electron 与 Python 网关分进程，网关独立保活和单实例                          |

旧网关的 306 项测试是**回归基线**，不是新功能已完成的证明，也不是禁止修改协议适配器的理由。

---

## 3. 用户、角色与核心场景

唯一操作用户是本人。软件没有第二个控制台用户，也不把平台联系人变成控制台用户。

| 角色     | 当前标识                                  | 规则                          |
| ------ | ------------------------------------- | --------------------------- |
| 操作者    | QQ `10000002`；B站 `10000001` | 保留 `[allowlist]`；修改白名单需界面确认 |
| QQ 机器人 | 小号 `10000004`                       | NapCat 扫码登录                 |
| 微信机器人  | 扫码所得账号                                | 默认 iLink；扫码前明确提示实际绑定的是哪个微信号 |
| B站机器人  | 小号 `10000003`                 | 四个 cookie；登录后核对 UID         |
| 抖音机器人  | 待绑定                                   | 登录后由本人核对账号，再启用自动回复          |

| ID | 场景             | 通过标准                                      |
| -- | -------------- | ----------------------------------------- |
| S1 | 开机后查看各通道并补齐登录  | 自启只打开已授权功能；每个通道分别显示凭据、连接和发送健康，不能要求「必须全绿」  |
| S2 | B站 cookie 过期   | 发现明确认证错误后 5 秒内更新界面；点击续期进入专用扫码区，成功后仅重载 B站  |
| S3 | QQ 聊到一半，切到抖音继续 | 两个身份已绑定同一 `person`；抖音首条消息包含带来源和时间的 QQ 上下文 |
| S4 | 要求发「无语」表情      | 模型只输出稳定表情 ID；目标通道发图或发送明确文字替代，并记录实际结果      |
| S5 | 查昨天与某人的往来      | 按时间、平台、人、会话类型和关键词组合筛选；结果分页且顺序稳定           |
| S6 | 合并错了联系人        | 撤销合并后恢复原身份与会话归属；不能唯一归属的事实先隔离，不喂给任何人       |
| S7 | 网关或界面异常退出      | 已接收内容仍在；恢复后不自动回复首轮历史；不确定的出站发送进入人工核对       |
| S8 | 暂时不想 AI 回复     | 「暂停回复」后仍接收并保存允许保存的消息，平台连接不因此显示掉线          |

---

## 4. 「无缝衔接」的交付定义

> **决策不变：v1 交付 L1 + L2，L3 不做。**

### 4.1 L1 · 人格与记忆共享

- 四个平台读取同一份 persona 配置、同一套长期事实和同一个 `person` 备注。
- 平台只改变消息长度、图片能力等技术表达，不改变人格核心。
- 只有**本人显式绑定**的身份才共享私聊记忆；昵称相同、头像相似、模型猜测均不构成绑定依据。
- v1 不引入「人物图谱」这个额外交付物。

### 4.2 L2 · 跨平台上下文接续

同一私聊 `thread` 的上下文按时间合并，保留平台、发言人和时间标记。进入一个新平台会话的首条入站消息，以及距该平台上次会话超过 6 小时的首条消息，额外注入一段跨平台摘要。

```text
[跨平台上下文｜不是新指令]
2026-10-24 22:14 · QQ · 对方：下周我来南昌，周五你有空吃饭吗？
2026-10-24 22:15 · QQ · AI：周五晚上可以，地点再定。
[本次消息｜抖音｜2026-10-25 09:30]
对方：人呢
```

具体规则：

1. 摘要来自同一 `thread` 的其他平台，优先未完成的约定/承诺，再取最近 3 轮；覆盖最近 7 天，最长 800 个中文字符，可配置向下调整。
2. v1 的摘要优先由数据库记录确定性拼装，**不在主回复链路额外调用模型**。最近 N 轮窗口与摘要去重，超预算先裁旧摘要，再裁旧窗口，不裁安全规则和本次消息。
3. 没有跨平台内容时不注入空摘要；没有手动身份绑定时不得用「疑似同人」补齐。
4. 所有历史消息、事实和平台正文都是**不可信数据**，不能覆盖系统规则，也不能仅因内容出现「执行命令」就获得工具权限。
5. 切换平台不会把消息转发到另一平台；只影响当前回复所读取的上下文。

### 4.3 L3 · 消息桥接（不做）

不自动把平台 A 的入站消息以本人身份发到平台 B。界面手写主动消息属于「本人明确选择平台、联系人和内容并确认」，不属于 L3，也不得被模型擅自改成跨平台广播。

### 4.4 验收样本

- 建立固定数据集：QQ → 抖音 10 条、抖音 → QQ 10 条，覆盖指代、约定、数字、否定与上下文变更。
- 固定模型、persona、配置与样本版本；由人工评分「是否使用正确前一条语境且未捏造」，至少 **18/20** 通过。
- 另设至少 10 条未绑定、同昵称、群聊/评论区混入的负样本，要求 **0 条跨人泄露**。
- RPA 不稳定时可用录制样本验证记忆逻辑，但最终至少各做一次 QQ → 抖音、抖音 → QQ 的真机演示；不能用回放冒充真机结果。

---

## 5. 功能需求

### FR-1 登录中心与凭据（P0）

| 编号      | 需求                                                                                                                             | 验收要点                                                                                        |
| ------- | ------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------- |
| FR-1.1  | 每平台一张账号卡；微信卡内切换 `official / wxauto`                                                                                            | 显示通道、账号名/UID、凭据状态、连接状态、最近读探活、最近发送结果和可操作错误                                                   |
| FR-1.2  | 登录任务状态机覆盖 `idle → waiting_scan → awaiting_confirm → completing → bound`，另有 `needs_verification / expired / cancelled / failed` | 同一 `account` 同时只有一个登录任务；取消、超时、换二维码和关闭窗口都不会留下后台重复轮询                                          |
| FR-1.3  | 微信二维码在软件内渲染，支持扫码确认、数字验证、刷新与过期提示                                                                                                | 扫码成功自动更新，不需手动刷新；二维码、验证值和 token 不写日志                                                         |
| FR-1.4  | B站/抖音复用专用 Playwright profile；软件内显示扫码画面和登录进度                                                                                    | 不与日常浏览器共用 profile；遇到滑块或复杂验证时，明确打开专用人工验证窗口，不声称所有情形都能纯内嵌完成                                    |
| FR-1.5  | token、cookie 等可迁移 API 凭据统一进 `data/vault.db`，默认 Windows DPAPI CurrentUser                                                       | 配置文件、业务数据库、日志、导出包中找不到 token、SESSDATA、`context_token` 等明文；Playwright profile 的会话数据按 §11 单独保护 |
| FR-1.6  | 认证错误与周期只读探活驱动状态更新                                                                                                              | 明确认证失败立即转 `expired/revoked`；正常运行且平台允许探活时，外部失效在 60 秒内可见；网络错误不得误报为凭据过期                        |
| FR-1.7  | 续期成功后只热重载对应 `account`                                                                                                          | 其他平台的消息处理与发送不中断；旧适配器先停止接收，再原子替换                                                             |
| FR-1.8  | NapCat 检测与拉起                                                                                                                   | 同时检查 loopback 端口与 OneBot 握手；可拉起本人确认的路径，不代填密码，不把「端口开着」当作登录成功                                 |
| FR-1.9  | 微信双通道状态与数据命名空间隔离                                                                                                               | `official` token/context 与 `wxauto` 窗口状态互不覆盖；同时只启用一个微信通道；切回不丢原通道凭据                          |
| FR-1.10 | 登录后核对机器人身份                                                                                                                     | UID 与配置期望不符时阻止启用自动回复；不得因扫码成功就直接向联系人发测试消息                                                    |

登录实现抽为 `login/providers/*`，原脚本保留无头入口并调用同一实现。B站不能凭空假定存在「二维码图片 API」；其实现是专用浏览器登录后采集 cookie。QQ 的第三方 NapCat 原生扫码窗口是明确的外部界面例外。

### FR-2 统一消息库与检索（P0）

| 编号      | 需求                                               | 验收要点                                                           |
| ------- | ------------------------------------------------ | -------------------------------------------------------------- |
| FR-2.1  | `data/mailbox.db` 使用 WAL，承载消息、会话、身份、事实、表情元数据与检查点 | 所有平台使用同一 schema 和迁移机制；凭据另放 `vault.db`，不是第二套消息库                 |
| FR-2.2  | 入站在交给模型前完成持久化；非白名单先过滤正文                          | 允许落库的消息在生成回复前可查询；拒绝消息仅存最少审计元数据，不存正文、附件和原始报文                    |
| FR-2.3  | 统一收件箱与稳定分页                                       | 按 `(timestamp, id)` 游标倒序；按平台、人、会话类型、方向和状态过滤，不因新消息插入而重复翻页       |
| FR-2.4  | FTS5 `trigram` 中文检索与短词精确子串回退                     | 「山中猛虎」能命中；1～2 字符查询走参数化 `LIKE` 并标注可能较慢，不把短词 `MATCH` 无结果当作不存在    |
| FR-2.5  | 持久去重与平台检查点                                       | 重启、平台重投、多实例竞争不会重复入队同一稳定消息 ID；无稳定 ID 时按 §7.6 处理                 |
| FR-2.6  | 首次同步只登记历史，不触发 AI 回复                              | QQ/微信/B站/抖音均使用一致的 `backlog` 原因；允许保存的历史可展示，`reply_policy=never` |
| FR-2.7  | 一次性导入 `data/sessions.json`                       | 源文件不变；重复导入结果一致；旧轮次标 `imported=1`、时间为导入时间，绝不自动回复                |
| FR-2.8  | 收发状态分离并保存安全错误信息                                  | 每条失败/跳过消息有可定位原因；原始 token、cookie、堆栈中的敏感参数不得直接展示                 |
| FR-2.9  | 数据备份、恢复、删除与导出                                    | 用 SQLite backup API；导出二次确认且默认不含凭据；删除正文同时维护 FTS、附件与相关事实         |
| FR-2.10 | 数据库故障必须显式                                        | 无法打开、迁移或写入时暂停 AI 处理并报警；不得自动退回空 JSON 或另一数据库继续收发                 |

### FR-3 身份、会话与统一记忆（P0；长期事实界面见 FR-3.7～3.9 为 P1）

| 编号     | 需求                              | 验收要点                                                                      |
| ------ | ------------------------------- | ------------------------------------------------------------------------- |
| FR-3.1 | 每个平台身份先归属独立 `person`，由本人手动绑定/合并 | 同昵称不自动合并；操作需预览影响范围并二次确认                                                   |
| FR-3.2 | 私聊、群聊、评论使用独立记忆边界                | 私聊只在确认后的同一 `person` 内跨平台；群按群隔离；评论按评论分支隔离，均不注入私聊事实                         |
| FR-3.3 | 最近 N 轮跨平台短期上下文                  | 默认 12 轮，按平台时间/接收时间稳定排序，带平台和发言人；同 `thread` 串行处理                            |
| FR-3.4 | L2 跨平台摘要                        | 触发、范围、长度与隔离符合 §4.2；不会重复塞满 prompt                                          |
| FR-3.5 | 人格单例与版本记录                       | 所有平台读取同一版本；修改只影响新消息，历史回复不改写                                               |
| FR-3.6 | 合并/拆分可撤销并保留审计                   | 按日志恢复原 `identity` 和 `conversation` 归属；不能唯一归属的事实进入 `quarantined`           |
| FR-3.7 | 回复成功后异步抽取长期事实                   | `event / preference / promise / meme / profile` 带来源消息、置信度与时间；失败不影响已产生的主回复 |
| FR-3.8 | 事实纠错                            | 可编辑、软删除、冻结、恢复和查看来源；删除/隔离后下一次 prompt 立即不再包含；冻结只是禁止自动改写，不是删除                |
| FR-3.9 | 事实注入可解释、有上限                     | 最多 12 条，能查看本次注入来源；优先置信度、关键词命中和时间新鲜度，过期/冲突事实不得无条件覆盖新消息                     |

### FR-4 表情包库（P1）

| 编号     | 需求                                       | 验收要点                                                                     |
| ------ | ---------------------------------------- | ------------------------------------------------------------------------ |
| FR-4.1 | 表情使用稳定 `sticker_id`、名称、多标签、哈希、MIME、尺寸和来源 | 文件改名不改变模型引用；一个表情多标签可查                                                    |
| FR-4.2 | 导入校验                                     | 通过文件选择器导入；校验真实 MIME、大小、图片解码和 SHA-256；不能仅凭扩展名信任文件                         |
| FR-4.3 | 适配器声明能力矩阵                                | 至少包含收发文本/图片、大小限制、是否需上传、评论与主动私信；`unknown` 不得当作支持                          |
| FR-4.4 | 模型输出结构化表情引用，网关校验后发送                      | 模型不能提供任意本地路径；不存在或未授权 ID 返回可见错误，不静默删除标记                                   |
| FR-4.5 | 不可发图时显式降级                                | 发送文字替代（如「（无语脸）」），`outbox` 记录 `requested=image`、`effective=text` 和原因      |
| FR-4.6 | 聊天图片经本人点击后入库                             | 模型可建议标签，但入库是用户操作；保留来源，不自动收藏所有图片                                          |
| FR-4.7 | 微信 iLink 图片上传作为协议验证项                     | AES-128-ECB + CDN 上传须先按真实协议验证；未通过前 `supports_image_send=false`，不宣称微信已能发图 |
| FR-4.8 | 抖音 DOM 文件上传作为真机验证项                       | 选择器未探明或发送回读不确定时返回 `unknown/failed`，不盲点页面、不自动反复重发                         |

### FR-5 回复行为、主动发送与预算（基础项 P0，完整预算界面 P1）

| 编号     | 需求                        | 验收要点                                                         |
| ------ | ------------------------- | ------------------------------------------------------------ |
| FR-5.1 | 默认拒绝，白名单决定 AI 是否响应        | 非白名单不调用模型、不自动回复、不保存正文；只有稳定平台标识才能参与自动白名单匹配                    |
| FR-5.2 | 群聊 @ 才动、私聊直答、评论可配置        | 机器人自己的消息、回显和空消息不进入自问自答循环                                     |
| FR-5.3 | 无法稳定识别发送者或会话时 fail closed | 可保存为待核对内容，但不得自动回复，也不得建立跨平台身份绑定                               |
| FR-5.4 | 保留 `/send` 与 MCP，新增界面手写发送 | 新 outbox API 要求幂等键；界面确认平台、联系人、正文和表情后才发送；不向平台历史自动广播           |
| FR-5.5 | 全局/单平台暂停回复与停止连接是两个动作      | 暂停回复仍可接收；停止连接才终止轮询；界面持续显示当前模式                                |
| FR-5.6 | 速率限制、队列上限与同线程串行           | 同 `thread` 串行，跨 `thread` 有界并发；平台每分钟限制在模型调用前生效，队列满时显式标记而非静默丢弃 |
| FR-5.7 | 每日 token/费用预算             | 统计主回复、事实抽取和工具调用；预算不足时停止新模型调用并提示，不能在回复后才悄悄超额                  |
| FR-5.8 | 人工验证与风控退避                 | 抖音遇可见验证码转 `needs_human` 并暂停该通道自动动作；不绕过验证，不以高频重试硬闯            |


### FR-6 桌面控制台与运维（P0）

| 编号     | 需求                                  | 验收要点                                                                                                 |
| ------ | ----------------------------------- | ---------------------------------------------------------------------------------------------------- |
| FR-6.1 | 单窗口六区：账号 / 收件箱 / 会话 / 记忆 / 表情包 / 日志 | 每区有空、加载、错误状态；设置和首启向导用弹层，不增加无效主导航                                                                     |
| FR-6.2 | 托盘常驻与受控退出                           | 关窗默认隐藏到托盘；菜单含状态、暂停、打开、退出；退出先停新任务并保存检查点                                                               |
| FR-6.3 | 单实例与 UI 崩溃恢复                        | 第二次启动聚焦已有窗口；UI 崩溃不杀网关；重启 UI 可重新接管，不出现两个轮询器                                                           |
| FR-6.4 | Electron 安全默认值                      | `contextIsolation=true`、`sandbox=true`、`nodeIntegration=false`、`webSecurity=true`；主窗口不加载第三方远程页面      |
| FR-6.5 | 首启向导和平台风险确认                         | 明示非官方通道风险、数据位置、模型后端、自启选项；未确认前不启用自动收发                                                                 |
| FR-6.6 | 打包与资源路径                             | PyInstaller onedir 网关嵌入 Electron 发布目录；不依赖当前工作目录；网关不要求系统另装 Python/Node，Reasonix 等外部 AI 后端仍按 §1.3 单独配置 |
| FR-6.7 | 适配器自愈与有界退避                          | 单平台异常不拖垮全局；指数退避有上限和抖动；认证失败/人工验证不得按普通异常无限重启                                                           |
| FR-6.8 | 分级滚动日志与实时查看                         | 界面轮询尾随、按平台/级别筛选；敏感字段脱敏；排障不必开终端                                                                       |
| FR-6.9 | 版本与诊断包                              | 显示应用、schema、适配器和模型后端版本；导出诊断前预览并默认排除正文、附件和凭据                                                          |

### FR-7 设置与配置（P0）

| 编号     | 需求                                                                                       | 验收要点                                  |
| ------ | ---------------------------------------------------------------------------------------- | ------------------------------------- |
| FR-7.1 | 保留 `config.toml` 开发用法，新增 `[ui]`、`[memory]`、`[budget]`、`[stickers]`、`[logging]`、`[paths]` | 旧配置有明确迁移结果，新增段有默认值和校验规则               |
| FR-7.2 | 图形界面修改配置经过 schema 校验、原子写入和单项/单通道热重载                                                      | 不整文件覆盖用户手工添加的其他段；失败时保留修改前配置           |
| FR-7.3 | 密钥字段只接受「写入保险库」操作                                                                         | 读取设置只返回 `configured=true/false`，不回显秘密 |
| FR-7.4 | 监听地址只能为数值 loopback，默认 `127.0.0.1:8787`                                                   | `0.0.0.0`、局域网地址和反向代理配置直接拒绝            |
| FR-7.5 | 生产配置不得开启 `allow_all`                                                                     | 旧配置中的该值只可用于开发测试；发布配置校验拒绝并说明原因         |

---

## 6. 目标架构与关键流程

### 6.1 进程边界

```text
┌──────────────────────── iLink Studio ────────────────────────┐
│ Electron 主进程                                               │
│ 窗口 / 托盘 / 单实例 / 自启 / 文件选择器 / 网关生命周期       │
│        │ 本地 HTTP + 每会话凭据                               │
│        ▼                                                      │
│ Python 网关独立进程（127.0.0.1:8787）                         │
│ ├─ API 层：静态控制台 + /api/v1 + 健康检查                    │
│ ├─ 应用层：Router / Outbox / Memory / Sticker / Login         │
│ ├─ 适配层：QQ / WeChat official / WeChat wxauto / Bili / RPA  │
│ └─ 存储层：SQLite 单写者 + 迁移 + FTS5 + Vault                │
│        │                       │                    │         │
│   Reasonix CLI            Playwright 浏览器       NapCat      │
└──────────────────────────────┼────────────────────┼───────────┘
                               ▼                    ▼
                       平台 HTTPS / DOM        OneBot loopback WS
```

**架构修正：不再是单进程。** Electron 负责桌面体验，Python 网关负责消息可靠性。窗口/渲染进程崩溃时网关继续工作；托盘随 Electron 消失不等于网关必须退出。显式「退出」才按顺序停止网关。

Playwright 浏览器只承载平台页面，不能拥有 Electron 的 Node 能力。B站/抖音远程网页不得嵌入具有本机权限的主控制台。

### 6.2 技术选型

| 层      | 决定                                       | 理由与约束                                                  |
| ------ | ---------------------------------------- | ------------------------------------------------------ |
| 服务     | 标准库 `ThreadingHTTPServer`                | 延续现有实现；发送改异步 outbox，避免 HTTP 线程等待长任务                    |
| 前端     | Vite + Preact + TypeScript，产物 `web/dist` | 复用成熟小依赖，构建后由网关托管，不需要目标机安装 Node                         |
| 桌面     | Electron                                 | 已拍板；只做壳与本机能力，不把业务逻辑复制一份到 JS                            |
| 数据库    | Python `sqlite3` + 顺序迁移                  | 不引 ORM；单写者、多读者、事务和 FTS 触发器可测试                          |
| 凭据     | `ctypes` 调 DPAPI                         | CurrentUser；显式可选 Windows 密码库型 `keyring` 后端，但绝不自动退回文本文件 |
| 打包     | PyInstaller onedir + Electron 发布目录       | Python 运行时可随包；Electron 本身不是单文件 exe，双击的是目录内启动程序         |
| 浏览器自动化 | 保留 Playwright                            | 专用 profile、固定版本、人工处理挑战，不另造浏览器框架                        |

外部开源实现可以参考，但必须核对许可证、维护状态和实际协议；REA 仅作为分析外部二进制/运行时的开发工具，不成为本产品运行依赖。

### 6.3 入站处理流程

```text
适配器收到事件
  → 解析最小身份与策略信息
  → 非白名单？仅写 metadata_only 审计，结束
  → 生成稳定去重键 / 标记不可信身份
  → BEGIN IMMEDIATE：消息 + 检查点 + received/queued
  → COMMIT（此后才允许调用模型）
  → 按 thread 获取处理租约
  → 组装 persona + 身份 + 事实 + 时间窗口 + L2 摘要 + 本次消息
  → 预算/速率检查 → Reasonix → 候选回复
  → 同一事务保存候选回复与 outbox(pending)
  → 单线程发送 worker 领取任务
  → 适配器返回 → sent / degraded / failed / unknown
  → 更新入站状态和审计
```

适配器通知路由与后台 worker 解耦；`emit()` 不应让平台长轮询被整段模型生成阻塞。检查点与消息写入必须协调，避免「游标已前进但消息没落库」。

### 6.4 状态机

**入站 `processing_status`**：

```text
received → queued → processing → replied
                     ├──────────→ skipped（规则跳过/空回复/历史登记）
                     ├──────────→ failed（明确失败；可按策略重试）
                     └──────────→ needs_review（关联出站结果不明）
```

- `received` 是已提交、尚未入队的可恢复状态。
- `processing` 带 `lease_owner + lease_expires_at`；进程死亡后租约可恢复，不能永久卡住。
- `skipped` 必须有 `skip_reason`；`failed` 必须有 `error_code`、重试次数和下次重试时间。
- 后台事实抽取失败不改变已经成功的主回复状态。

**出站 `outbox.state`**：

```text
draft → pending → sending → sent
                  ├────────→ degraded（实际以降级形式成功）
                  ├────────→ failed（平台明确拒绝，且未发出）
                  ├────────→ cancelled（尚未发送便取消）
                  └────────→ unknown（超时/断线/崩溃，实际结果不确定）
```

`sent` 只代表适配器确认平台已接受，不代表对方已读。`unknown` 是终态候选，**禁止自动重发**；用户核对平台后可标记「已补发/确认已发/忽略」。只有平台明确支持同一幂等键重试时，才能用原键重试。

### 6.5 崩溃恢复语义

| 崩溃点                       | 恢复行为                                 |
| ------------------------- | ------------------------------------ |
| 入站提交后、模型调用前               | 从 `received/queued` 恢复；不会重复写入稳定消息 ID |
| 模型生成后、候选回复提交前             | 可重新生成，可能多一次模型成本，但尚未对平台发送             |
| `outbox=pending`、尚未获得发送租约 | 可继续发送一次                              |
| `sending` 但平台明确未接受        | 记 `failed`，按明确可重试错误处理                |
| 平台已调用，结果未写库便崩溃            | 转 `unknown`，界面提示人工核对，不自动再发           |
| UI 进程退出                   | 网关不动；UI 重启重新读取状态，不重放消息               |
| 数据库不可用                    | 停止新模型与发送动作，保留适配器为降级/暂停状态，先修数据库       |

跨 SQLite 与外部平台没有分布式事务，因此承诺的是「持久化、幂等入队、明确结果与人工恢复」，不是 exactly-once。

### 6.6 状态与能力展示

登录卡同时显示三组数据：

1. **凭据**：未绑定、扫码中、有效、过期、需人工验证。
2. **连接**：停止、启动中、在线、重连中、降级、错误。
3. **发送健康**：未知、最近成功、最近失败、能力不支持。

只读探活成功不能证明有发私信/评论权限。手动连通性自测必须选择本人控制的目标并二次确认；不能给陌生人自动发测试消息。

---

## 7. 数据模型与一致性约束


### 7.1 核心表（逻辑模型）

字段名是实施基线；可加索引和审计字段，不得削弱约束。时间用 Unix epoch 秒存储、界面按 `Asia/Shanghai` 展示；另存时间来源，导入数据不得伪造原始时间。

```sql
CREATE TABLE schema_migration (
  version INTEGER PRIMARY KEY,
  checksum TEXT NOT NULL,
  applied_at REAL NOT NULL
);

CREATE TABLE account (                  -- 接入槽位，不是现实人物
  id INTEGER PRIMARY KEY,
  platform TEXT NOT NULL,               -- qq|wechat|bilibili|douyin
  channel TEXT NOT NULL,                -- onebot|official|wxauto|cookie|rpa
  platform_uid TEXT NOT NULL DEFAULT '',
  display_name TEXT,
  auth_state TEXT NOT NULL DEFAULT 'unbound',
  enabled INTEGER NOT NULL DEFAULT 0,
  last_auth_ok_at REAL,
  last_error_code TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(platform, channel)             -- v1 每通道一个槽位
);

CREATE TABLE person (
  id INTEGER PRIMARY KEY,
  display_name TEXT NOT NULL,
  is_self INTEGER NOT NULL DEFAULT 0 CHECK (is_self IN (0,1)),
  note TEXT,
  created_at REAL NOT NULL
);

CREATE TABLE identity (
  id INTEGER PRIMARY KEY,
  person_id INTEGER NOT NULL REFERENCES person(id),
  platform TEXT NOT NULL,
  platform_uid TEXT NOT NULL,
  id_kind TEXT NOT NULL,                -- stable|opaque|provisional
  display_name TEXT,
  is_bot INTEGER NOT NULL DEFAULT 0 CHECK (is_bot IN (0,1)),
  UNIQUE(platform, id_kind, platform_uid)
);

CREATE TABLE thread (
  id INTEGER PRIMARY KEY,
  scope TEXT NOT NULL CHECK (scope IN ('private','group','comment')),
  person_id INTEGER REFERENCES person(id),
  title TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  CHECK ((scope='private' AND person_id IS NOT NULL)
      OR (scope IN ('group','comment') AND person_id IS NULL))
);
CREATE UNIQUE INDEX ux_private_thread
  ON thread(person_id) WHERE scope='private';

CREATE TABLE conversation (
  id INTEGER PRIMARY KEY,
  thread_id INTEGER NOT NULL REFERENCES thread(id),
  account_id INTEGER NOT NULL REFERENCES account(id),
  kind TEXT NOT NULL CHECK (kind IN ('private','group','comment')),
  chat_id TEXT NOT NULL,                -- 规范化后的平台会话/评论分支键
  created_at REAL NOT NULL,
  UNIQUE(account_id, kind, chat_id)
);

CREATE TABLE message (
  id INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES conversation(id),
  account_id INTEGER NOT NULL REFERENCES account(id),
  platform TEXT NOT NULL,
  platform_msg_id TEXT,
  dedup_key TEXT,                       -- 无平台 ID 时的受控替代键
  direction TEXT NOT NULL CHECK (direction IN ('in','out')),
  sender_identity_id INTEGER REFERENCES identity(id),
  body TEXT,
  attachments_json TEXT NOT NULL DEFAULT '[]',
  storage_class TEXT NOT NULL CHECK (storage_class IN ('full','metadata_only')),
  -- 入站：received|queued|processing|replied|skipped|failed|needs_review
  -- 出站：outbound；发送结果以 outbox.state 为准，不套用入站状态机
  processing_status TEXT NOT NULL,
  skip_reason TEXT,
  error_code TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL,
  lease_owner TEXT,
  lease_expires_at REAL,
  imported INTEGER NOT NULL DEFAULT 0 CHECK (imported IN (0,1)),
  reply_policy TEXT NOT NULL DEFAULT 'normal', -- normal|never
  occurred_at REAL,                     -- 平台原始时间，未知则为 NULL
  ingested_at REAL NOT NULL,
  time_source TEXT NOT NULL,            -- platform|received|import
  CHECK ((storage_class='full' AND body IS NOT NULL)
      OR (storage_class='metadata_only' AND body IS NULL))
);
CREATE UNIQUE INDEX ux_message_platform_id ON message(account_id, platform_msg_id)
  WHERE platform_msg_id IS NOT NULL AND platform_msg_id <> '';
CREATE UNIQUE INDEX ux_message_dedup ON message(account_id, dedup_key)
  WHERE dedup_key IS NOT NULL AND dedup_key <> '';
CREATE INDEX ix_message_inbox ON message(ingested_at DESC, id DESC);

CREATE VIRTUAL TABLE message_fts USING fts5(
  body, content='message', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER message_fts_insert AFTER INSERT ON message
  WHEN new.storage_class='full' AND new.body IS NOT NULL BEGIN
  INSERT INTO message_fts(rowid, body) VALUES (new.id, new.body);
END;
CREATE TRIGGER message_fts_delete AFTER DELETE ON message BEGIN
  INSERT INTO message_fts(message_fts, rowid, body)
    VALUES ('delete', old.id, old.body);
END;
CREATE TRIGGER message_fts_update AFTER UPDATE OF body, storage_class ON message BEGIN
  INSERT INTO message_fts(message_fts, rowid, body)
    VALUES ('delete', old.id, old.body);
  INSERT INTO message_fts(rowid, body)
    SELECT new.id, new.body
    WHERE new.storage_class='full' AND new.body IS NOT NULL;
END;

CREATE TABLE outbox (
  id INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL UNIQUE REFERENCES message(id),
  source_message_id INTEGER REFERENCES message(id),
  client_request_id TEXT NOT NULL UNIQUE,
  idempotency_key TEXT NOT NULL UNIQUE,
  requested_kind TEXT NOT NULL,         -- text|image
  effective_kind TEXT,                  -- text|image
  state TEXT NOT NULL,                  -- draft|pending|sending|sent|degraded|failed|cancelled|unknown
  attempt_count INTEGER NOT NULL DEFAULT 0,
  lease_owner TEXT,
  lease_expires_at REAL,
  next_attempt_at REAL,
  error_code TEXT,
  error_detail_safe TEXT,
  platform_msg_id TEXT,
  sent_at REAL,
  created_at REAL NOT NULL
);
CREATE UNIQUE INDEX ux_outbox_auto_reply ON outbox(source_message_id)
  WHERE source_message_id IS NOT NULL;
-- 手动发送的 source_message_id 为 NULL；一条入站至多一个自动回复 outbox。
-- 候选回复重试时只更新已有草稿，直到平台返回明确失败才允许新建后续任务。

CREATE TABLE adapter_checkpoint (
  account_id INTEGER NOT NULL REFERENCES account(id),
  stream TEXT NOT NULL,
  cursor TEXT,
  state_json TEXT NOT NULL DEFAULT '{}', -- 禁止放 token/cookie/context_token
  primed INTEGER NOT NULL DEFAULT 0 CHECK (primed IN (0,1)),
  updated_at REAL NOT NULL,
  PRIMARY KEY(account_id, stream)
);

CREATE TABLE memory_fact (
  id INTEGER PRIMARY KEY,
  person_id INTEGER NOT NULL REFERENCES person(id),
  kind TEXT NOT NULL,                   -- event|preference|promise|meme|profile
  content TEXT NOT NULL,
  source_message_id INTEGER REFERENCES message(id),
  source_identity_id INTEGER REFERENCES identity(id),
  confidence REAL NOT NULL DEFAULT 0.7 CHECK (confidence BETWEEN 0 AND 1),
  status TEXT NOT NULL DEFAULT 'active',-- active|quarantined|deleted
  frozen INTEGER NOT NULL DEFAULT 0 CHECK (frozen IN (0,1)),
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  last_used_at REAL
);

CREATE TABLE sticker (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  mime TEXT NOT NULL,
  w INTEGER, h INTEGER, bytes INTEGER,
  source TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE sticker_tag (
  sticker_id INTEGER NOT NULL REFERENCES sticker(id) ON DELETE CASCADE,
  tag TEXT NOT NULL,
  PRIMARY KEY(sticker_id, tag)
);

CREATE TABLE merge_log (
  id INTEGER PRIMARY KEY,
  source_person_id INTEGER NOT NULL,
  target_person_id INTEGER NOT NULL,
  payload_json TEXT NOT NULL,           -- 修改前后 identity/conversation/fact 归属
  created_at REAL NOT NULL,
  reverted_at REAL
);

CREATE TABLE audit_event (
  id INTEGER PRIMARY KEY,
  event_type TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}', -- 脱敏；不得保存秘密或非白名单正文
  created_at REAL NOT NULL
);
```

`credential` 位于独立的 `vault.db`，不能与业务库做跨库外键假设：

```sql
CREATE TABLE credential (
  account_key TEXT NOT NULL,             -- 如 wechat:official；逻辑关联 account
  key TEXT NOT NULL,                     -- sessdata|bili_jct|bot_token|context_token|...
  crypto_backend TEXT NOT NULL,          -- dpapi-current-user|windows-keyring
  crypto_version INTEGER NOT NULL,
  blob BLOB NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY(account_key, key)
);
```

### 7.2 身份归并与隔离规则

| 情形           | 规则                                                        |
| ------------ | --------------------------------------------------------- |
| 同一个人在 QQ 和抖音 | 默认各一个 `person`；界面展示候选，但只有本人确认后才合并私聊 thread                |
| 昵称相同或只有昵称    | `id_kind=provisional`，不自动进白名单、不跨平台合并、不自动回复                |
| 群聊           | 一个群一个 thread；群成员可有 identity，但群消息不进入任何人的私聊 thread          |
| 评论区          | 一个评论分支一个 thread；不能按整个视频把多人混入，也不进入私聊事实                     |
| 合并错误         | 通过 `merge_log` 事务性恢复 identity/conversation 的原归属，不复制或删除原消息 |
| 合并期间产生的事实    | 仅来源身份明确的事实可随绑定迁移；归属不明确的先 `quarantined`，人工确认后才能使用          |
| 机器人换登录小号     | 新 `account.platform_uid` 需重新确认；历史联系人消息不因此删除，也不自动换绑操作者     |

「宁可漏，不可串」优先于跨平台覆盖率。

### 7.3 合并事务

1. 预览将移动的私聊身份、会话、消息数量和可能受影响的事实；群/评论不出现在可合并列表。
2. `BEGIN IMMEDIATE` 写 `merge_log`，再修改 `identity.person_id` 与私聊 `conversation.thread_id`；消息正文和原始消息 ID 不动。
3. 旧 thread 保留为审计空壳或标归档，不能级联删除消息。
4. 撤销时严格按 `payload_json` 恢复，不重新猜测归属；若后续已有人工改动，先提示冲突，不覆盖。
5. 下一次 prompt 立即使用新边界；正在生成的任务在发送前重查边界版本，过期结果不得发送给错误对象。

### 7.4 SQLite 与检索规则

- 每个连接执行 `PRAGMA foreign_keys=ON`、`busy_timeout=5000`；库使用 `journal_mode=WAL`、默认 `synchronous=FULL`。
- 一个专用 writer 串行写事务；读者不持有长事务。写状态用条件更新实现租约，不靠进程内锁代替数据库原子性。
- 迁移只向前，启动时拒绝更高版本的未知 schema；迁移前用 SQLite backup API 备份。开发环境与打包环境使用同一迁移序列。
- `message_fts` 是 external-content 表，必须验证更新和删除触发器；不能只测新增搜索。
- 检索输入先参数化。三个及以上字符可走 trigram `MATCH`；短词走带转义的 `instr(body, ?)` 或 `LIKE`，限制结果数并显示性能提示。
- 业务库正文默认是明文 SQLite，DPAPI **不加密聊天全文**。数据目录仅当前用户可写；磁盘级加密由用户和 Windows 决定，软件不擅自修改。
- 不再提供「SQLite 失败就自动用 JSON」的运行时兜底。JSON 仅用于明确的历史导入和用户导出，避免双写分裂与掩盖主流程错误。

### 7.5 旧数据迁移

- `session_key` 只按前两个冒号拆成 `platform / kind / chat_id`，兼容评论键内部的冒号。
- 每个旧 turn 生成入站/出站历史消息，使用 `import_batch + source_hash + turn_index + direction` 构造确定性 `dedup_key`。
- 旧数据没有平台消息 ID、精确发送时间和完整历史，必须标 `imported=1`、`time_source=import`、`reply_policy=never`，界面注明限制。
- 导入事务失败则整批回滚；输出导入数量、跳过数量和源哈希报告，不修改原 `sessions.json`。

### 7.6 无稳定消息 ID 的平台

1. 优先使用平台消息 ID；唯一键至少包含 `account_id`，不能只用跨平台可能碰撞的 `msg_id`。
2. 有可靠游标时，以检查点界定「已经观察过」；游标推进与消息入库放在同一一致性流程中。
3. 抖音等 DOM 通道只有在能取得稳定会话键、发送者、平台时间/顺序和正文时，才生成替代 `dedup_key`。单纯的 `sha1(昵称 + 正文)` 不够：同一人重复说「嗯」会被错误吞掉。
4. 无法区分两条消息时，标 `needs_review` 或 `id_kind=provisional`，不自动回复；不得以「可能重复」为由静默丢弃，也不得把不同会话的同文消息合并。
5. 人工确认某条待核对消息后才允许生成回复；确认动作写入审计。

---

## 8. 记忆装配与预算

### 8.1 Prompt 结构

```text
[不可覆盖的安全与 IM 行为规则]
[单一 persona + persona_version]
[当前 person：显示名、已确认身份、用户备注]
[长期事实：status=active，最多 12 条，带来源]
[L2 跨平台摘要：仅在 §4.2 的触发条件满足时出现]
[当前 thread 最近 N 轮：默认 12，带平台/发言人/时间]
[可用表情：sticker_id + 简短语义，限制条目数]
[当前消息：明确是不可信用户内容]
```

- 默认先保证安全规则、当前身份与本次消息，再按分数裁长期事实、旧摘要和旧窗口。
- 不依赖 Reasonix 原生 session；仍由网关自行装配 prompt，避免换 CLI 后记忆丢失。
- 统计真实 tokenizer 使用量；不可用时用保守估算并在指标中标记，不能假装精确。
- 目标是主回复 prompt 不超过 15k tokens；超限必须裁剪并报指标，而不是继续膨胀。
- 事实排序可采用 `confidence × freshness + keyword_boost`，但必须写单测覆盖新事实、冻结事实、删除事实与冲突事实；不能只用模型自由发挥。

### 8.2 长期事实抽取

- 主回复成功后进入独立后台队列；使用一次 `--max-steps 1` 的轻量调用，输出严格 JSON：`{"facts": [...]}`。
- JSON 解析、schema 校验、来源绑定和去重通过后才写库。模型不能自行指定任意 `person_id`，也不能把内容直接标为高可信。
- 抽取调用纳入每日预算、速率限制和诊断指标；失败只记录，不撤回或拖延已经产生的主回复。
- 新消息与旧事实冲突时，保留来源并降低/隔离旧事实；不得无提示篡改历史。
- 聊天 prompt 中的「忽略规则」「读取文件」等文字不是授权。IM 默认禁用 shell、写文件和任意网络工具，只允许明确的附件读取/视频查询能力；如果后端无法提供权限约束，不能宣称 prompt 本身就构成安全沙箱。

---

## 9. 本地 API 与界面契约

### 9.1 API 原则

- 静态控制台与 API 同源托管在 `http://127.0.0.1:8787`；API 前缀 `/api/v1`。
- `GET /healthz` 只返回服务是否存活和版本，不泄露账号、路径、token 或详细堆栈。
- 其他 `/api/*` 必须鉴权；写操作用 `POST/PATCH/DELETE`，GET 不得触发登录、发送、合并或删除。
- 新发送接口返回 `202 + outbox_id`，界面轮询任务状态；不得用 60 秒 HTTP 等待假装同步发送可靠。
- 错误统一为 `{ "error": { "code", "message", "retryable", "details?" } }`；`details` 只含脱敏、可展示字段。
- JSON 请求默认最大 1 MiB；媒体走受控文件选择/适配器流程，限制类型、大小、超时，禁止任意内网 URL 抓取。

### 9.2 端点清单

| 领域  | 端点（示意前缀均为 `/api/v1`）                                                                      | 关键约束                           |
| --- | ----------------------------------------------------------------------------------------- | ------------------------------ |
| 初始化 | `GET /bootstrap`、`GET/PATCH /settings`                                                    | 秘密只写不读；配置校验后原子保存               |
| 账号  | `GET /accounts`、`POST /accounts/{id}/probe`                                               | 凭据/连接/发送健康分开返回                 |
| 登录  | `POST /accounts/{id}/login`、`POST /login-sessions/{id}/cancel`、`GET /login-sessions/{id}` | 单任务；二维码通过鉴权响应返回，不写公开静态目录       |
| 通道  | `PUT /accounts/wechat/active-channel`                                                     | 先停旧通道；前置条件不满足则 409，不破坏旧凭据      |
| 消息  | `GET /messages`、`GET /messages/{id}`                                                      | 游标分页、组合过滤、正文按文本渲染              |
| 会话  | `GET /conversations`、`GET /conversations/{id}/messages`                                   | 不跨 thread 返回未授权上下文             |
| 人物  | `GET /persons`、`POST /persons/{id}/merges`、`POST /merges/{id}/revert`                     | 预览、确认、审计、可撤销                   |
| 记忆  | `GET /memory-facts`、`PATCH /memory-facts/{id}`                                            | 编辑/冻结/软删除/恢复；保留下次注入可见性         |
| 表情  | `GET /stickers`、`POST /stickers`、`PATCH /stickers/{id}`                                   | 文件由 Electron 选择器或已入站附件明确授权     |
| 发送  | `POST /outbox`、`GET /outbox/{id}`、`POST /outbox/{id}/reconcile`                           | 幂等键；`unknown` 只能人工核对，无通用 retry |
| 运维  | `GET /logs`、`GET /diagnostics`、`POST /backup`                                             | 脱敏、分页；备份/导出需确认                 |

旧 `POST /send`、`POST /video` 在迁移期保留 Bearer token 和现有 MCP 兼容性；`/send` 不自动重试并标记弃用，新界面不得继续依赖它。

### 9.3 Loopback 不等于授权

必须同时做到：

- 绑定数值 `127.0.0.1`，校验 `Host`，拒绝 DNS rebinding 和非 loopback 地址。
- 每会话随机 UI 凭据至少 256 bit，经 Electron 主进程与网关的受控通道传递；不得放在 URL、静态 JS、截图或日志中。
- preload 通过最小 `contextBridge` API 转发请求；渲染进程不获得 Node、任意文件读取或任意进程执行能力。
- 若请求带 `Origin`，必须精确匹配控制台 origin；不允许通配 CORS，不接受跨站简单表单驱动写操作。token 不得作为查询参数。
- 内容安全策略至少 `default-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'`，连接只允许控制台与明确需要的资源。
- 拦截主窗口非预期导航和 `window.open`；聊天正文按纯文本渲染，禁止把平台 HTML 直接注入 `innerHTML`。
- 生产环境关闭远程调试入口，不开放 CDP 端口；Electron 发布配置关闭不必要的 `RunAsNode` 能力。

普通本机浏览器即使能访问 8787，也因没有每会话凭据而不能操作 API；界面中仍可提供只读健康状态。

---

## 10. 非功能需求

| 类别    | 目标与测量口径                                                       |
| ----- | ------------------------------------------------------------- |
| 文字延迟  | 固定模型与工作集、队列空闲、无工具调用，20 条样本的端到端 P95 目标 ≤ 12 秒；网络/平台故障单列，不算正常样本 |
| 带图延迟  | 已缓存附件、可用图像后端，10 条样本 P95 目标 ≤ 45 秒；下载/CDN/风控耗时单独记录             |
| 成本    | 单条纯文字总成本目标 ≤ $0.001，必须包含主回复及分摊的事实抽取；未配置模型价格时只报告 token，不编造美元数字 |
| 冷启动   | 双击到主窗口可切换六区、读取本地状态 ≤ 5 秒；目标机热缓存连续 5 次，均达标；平台上线与模型预热异步，不阻塞首屏   |
| 可靠性   | 入站允许保存的内容先提交再处理；稳定 ID 重启不重复入队；`unknown` 不自动重发                 |
| 数据完整性 | 迁移前后行数/抽样哈希核对；FTS 增删改一致；备份可恢复到临时目录并通过 integrity check         |
| 隐私    | 非白名单无正文；日志默认无正文和秘密；业务库正文明文这一事实在首启说明；导出需确认                     |
| 网络    | 服务只绑 loopback；平台 HTTPS 与明确配置的本地 OneBot/代理除外，不新增监听端口           |
| 权限    | 不以管理员运行，不写系统级环境变量，不修改系统代理/防火墙，不静默安装软件                         |
| 可维护性  | 旧 306 项测试保持通过；新增核心逻辑有单元/集成测试；平台协议变更用适配器测试隔离                   |
| 可观测性  | 指标至少含各通道最近心跳、队列深度、处理/发送延迟、重试次数、token/费用、降级次数和数据库错误            |

性能是固定环境下的验收目标，不是对第三方平台或模型服务的 SLA 承诺。

---

## 11. 安全、隐私与合规

| 项           | 必须执行的规则                                                                                                                      |
| ----------- | ---------------------------------------------------------------------------------------------------------------------------- |
| 小号与授权       | 自动收发使用本人控制的小号和测试联系人；主号不交出密码。平台条款和封号风险首启明确展示                                                                                  |
| 非官方通道       | NapCat、B站 cookie、抖音 RPA、`wxauto` 标为非官方路径；默认微信仍是已确认的 iLink 官方通道                                                               |
| 凭据          | DPAPI 默认不带 `CRYPTPROTECT_LOCAL_MACHINE`，使用 CurrentUser 与禁止 UI 标志；失败时要求重新登录，不写明文备选文件                                          |
| DPAPI 边界    | 通常仅同一 Windows 用户、同一机器可解密；备份到另一用户/机器后需重新登录，不承诺可迁移凭据                                                                           |
| 浏览器 profile | Playwright profile 中的 cookie/localStorage 是敏感登录态，可能由浏览器自行加密，不能声称全部由 vault 加密。存入仅当前用户可写的数据目录，不进日志、诊断包或普通导出，也不向 Reasonix 工作区开放 |
| 旧凭据迁移       | 用户确认后读取 `wx-bot.json`、`bili-cookies.json` 等已知文件，写入 vault 并验证；清理由用户明确选择，优先回收站/安全备份，不自动删除                                      |
| 日志          | SESSDATA、cookie、token、`context_token`、Authorization、二维码内容和完整协议报文统一脱敏；错误码可保留                                                  |
| 聊天正文        | 仅本机处理与发送给已配置的模型服务；首启说明模型提供方意味着正文会离开本机，不能写「正文永不离开电脑」                                                                          |
| 最小工作区       | Reasonix 使用独立工作目录，仅暴露本轮明确需要的附件；不得默认把整个项目、`vault.db` 或数据目录交给 agent                                                            |
| 主动动作        | 发送、合并、删除、导出、切换通道均有清晰确认与审计；模型无权限代替本人点击这些确认                                                                                    |
| 人工挑战        | 验证码/滑块只提示本人处理；不自动绕过，不将挑战页文本误当用户消息                                                                                            |
| 数据删除        | 删除前预览范围；数据库、FTS、附件、事实和导出的一致性可验证；凭据注销与消息删除是不同操作                                                                               |

---

## 12. 里程碑与依赖

估算是单人 + AI 协作的粗略工作量，不是日期承诺。每个阶段都要保留旧测试，并补对应新测试。

| 阶段         | 主要交付                                                        | 出口判据                                                         | 粗略估算  |
| ---------- | ----------------------------------------------------------- | ------------------------------------------------------------ | ----- |
| M0 · 壳与登录  | Electron 主/预加载/托盘、独立网关生命周期、账号卡、登录 provider、DPAPI vault、通道隔离 | 满足前置条件的通道可不用终端登录/续期；本机 `wxauto` 明确不支持；UI 强退不影响网关；安全单测通过      | 1.5 周 |
| M1 · 消息底座  | schema/migrations、单写者、消息/outbox、检查点、旧 JSON 导入、收件箱、FTS、备份    | 四适配器录制事件写同库；故障注入和重启恢复通过；非白名单正文为 0；短词/中文检索通过                  | 1.5 周 |
| M2 · L1+L2 | identity/person/thread、手动合并撤销、跨平台窗口与摘要、人格单例                 | 20 条 L2 样本 ≥18 通过；隔离负样本 0 泄露；合错人可恢复                          | 2 周   |
| M3 · 表情    | 表情表、标签、结构化引用、能力矩阵、降级、入库；微信/抖音发图预研                           | 四平台结果明确；不支持的发送必降级；微信/抖音发图只有真机通过才改能力声明                        | 1 周   |
| M4 · 可交付构建 | onedir 网关 + Electron 发布目录、自启、单实例、日志尾随、资源路径、首启向导             | 干净 Windows 测试用户双击可进界面；无 Python/Node 安装依赖；断网/换 cwd/ UI 崩溃场景通过 | 1 周   |
| M5 · 完整候选版 | 长期事实、纠错、预算界面、诊断、全量回归与真机验收                                   | 事实可增删冻结且有来源；预算可阻断；AC 全部门径完成或有明确版本变更                          | 1 周   |

依赖关系：

- M0 与 M1 可并行，但数据库 migration 必须统一编号、串行合入；M0 先拥有 account/vault，M1 拥有消息域。
- M1 是 M2 的前置；没有持久消息和身份边界，不能先做「跨平台记忆」。
- M3 的微信图片上传是独立协议预研，不阻塞其他平台的表情降级。
- M0 就要有开发启动脚本；M4 是发布级打包，不应到 M4 才第一次验证进程边界。
- M5 后做 v1.0 发布复核；不能在 M4 仅因「有 exe」就宣布所有目标完成。

---

## 13. 验收矩阵

`CI` 表示可在无真实账号的自动测试完成；`LIVE` 表示必须使用本人小号和受控联系人；`MANUAL` 表示需要人工判断。任何 LIVE 测试都不得向陌生账号群发。

| AC    | 场景与操作                                          | 期望                                                       | 方式                 | 阶段    |
| ----- | ---------------------------------------------- | -------------------------------------------------------- | ------------------ | ----- |
| AC-01 | 微信 official 与 wxauto 各保存不同状态，来回切换              | 凭据/context 不覆盖；同时最多一个通道运行；4.x 选择 wxauto 得到明确不支持          | CI + MANUAL        | M0    |
| AC-02 | 登录中取消、二维码过期、重复点击登录、扫码 UID 不符                   | 无重复轮询；状态准确；UID 不符不启用自动回复                                 | CI + LIVE          | M0    |
| AC-03 | B站续期时持续注入 QQ 测试消息                              | 仅 B站重载，QQ 消息不丢失、不重复                                      | CI + LIVE          | M0    |
| AC-04 | 杀掉 Electron 渲染/主进程，再重启 UI                      | 网关仍处理消息；恢复后只有一个网关和一组轮询器                                  | CI + MANUAL        | M0/M4 |
| AC-05 | 非白名单发送含秘密标记的正文和附件                              | 不调用模型、不自动回复；数据库、FTS、日志、导出均查不到正文                          | CI                 | M1    |
| AC-06 | 同一稳定消息 ID 并发投递两次并重启后再投                         | 只有一条消息、至多一个处理任务，无重复自动回复                                  | CI                 | M1    |
| AC-07 | 无平台 ID，同一 DOM 会话连续两条相同「嗯」                      | 不仅凭正文吞掉第二条；无法区分则进入待核对而非自动回复                              | CI                 | M1    |
| AC-08 | 分别在落库后、生成后、发送后回写前制造崩溃                          | 前两阶段可恢复；最后阶段 `unknown`，不会自动重发                            | CI                 | M1    |
| AC-09 | 首次登录/空游标同步历史                                   | 历史按策略展示且 `reply_policy=never`，模型调用数为 0                   | CI + LIVE          | M1    |
| AC-10 | 导入同一 `sessions.json` 两次，再重启                    | 行数不重复、原文不变、无自动回复；导入时间不被冒充为平台时间                           | CI                 | M1    |
| AC-11 | 搜索「山中猛虎」「猛虎」「嗯」及包含 `%_` 的词                     | 长中文命中；短词精确子串命中；通配符不会扩大语义；增删后索引一致                         | CI                 | M1    |
| AC-12 | 未绑定的两个同昵称身份分别说不同秘密                             | 各自 thread 只能看到自己的内容，prompt 中无另一人的秘密                      | CI                 | M2    |
| AC-13 | 显式合并 QQ/抖音身份后从另一平台追问                           | 带来源的上下文被使用；20 条固定样本至少 18 条正确                             | CI + LIVE + MANUAL | M2    |
| AC-14 | 群聊、评论与同一个人的私聊同时存在                              | 群/评论内容不会进入私聊 prompt；不同评论分支不互串                            | CI                 | M2    |
| AC-15 | 合并后产生事实，再撤销合并                                  | identity/conversation 恢复；归属不明事实被隔离，不进入任何 prompt          | CI                 | M2/M5 |
| AC-16 | 同一表情在支持、不支持、明确拒绝、超时四种适配器上发送                    | 分别 `sent/degraded/failed/unknown`；降级和错误可见；超时不自动重发        | CI + LIVE          | M3    |
| AC-17 | 模型返回不存在的表情 ID 或本地任意路径                          | 网关拒绝，不读取路径、不静默丢标记、不发送错误文件                                | CI                 | M3    |
| AC-18 | 删除/冻结/修改事实并构造下一次 prompt                        | 删除立即消失；冻结不被自动改写；修改有审计和来源                                 | CI                 | M5    |
| AC-19 | 达到平台速率、队列和每日预算边界                               | 在模型调用前阻断或排队；界面提示；不继续产生隐藏费用                               | CI                 | M5    |
| AC-20 | 恶意网页/聊天正文尝试导航、跨源 POST、注入 HTML、读取文件             | 导航与跨源写被拒；正文不执行；渲染进程无 Node/任意文件能力                         | CI + MANUAL        | M0/M4 |
| AC-21 | 用错误 host、无 token、token 放 URL 的方式访问 API         | 除最小健康检查外均失败；日志不记录 token；成功请求不能只凭来源 IP 获得授权               | CI                 | M0    |
| AC-22 | 暂停回复后接收消息，再停止连接                                | 暂停时仍落库但不生成；停止连接后才停收；两种 UI 状态不混淆                          | CI + LIVE          | M1/M5 |
| AC-23 | 从非项目 cwd 启动打包程序、切换 Windows 用户运行旧数据             | 资源可找到；无控制台黑框；DPAPI 不解密时提示重新登录，不生成明文副本                    | MANUAL             | M4    |
| AC-24 | 备份后升级 schema，再恢复到临时目录                          | 迁移可重复受控、integrity check 通过、行数与抽样内容一致                     | CI                 | M1/M4 |
| AC-25 | 抖音出现可见验证码/挑战壳                                  | 状态转 `needs_human`，不调用模型、不将挑战文本入库为聊天、不连续自动点击              | CI + LIVE          | M0/M5 |
| AC-26 | 图形界面修改一项设置并模拟写入失败；提交非 loopback 或生产 `allow_all` | 合法修改热重载且不覆盖无关配置；失败保留原配置；危险配置被明确拒绝                        | CI                 | M0/M4 |
| AC-27 | 目标机热缓存下双击启动，连续测 5 次首屏；随后做 20 条文字、10 条已缓存图片回复   | 5 次首屏均 ≤5 秒且平台异步上线；记录文字 P95、图片 P95 与模型/token 成本，按 §10 判定 | LIVE + MANUAL      | M4/M5 |

发布门槛：所有 P0 对应 AC 必须通过；P1 若延期，必须在版本变更中明确删除对应目标，不能用「基本可用」替代。

---

## 14. 风险与对策

| #   | 风险                  | 早期信号                              | 对策与剩余风险                                                       |
| --- | ------------------- | --------------------------------- | ------------------------------------------------------------- |
| R1  | 抖音 RPA 风控/DOM 改版    | 挑战壳、选择器失效、回读不一致                   | 状态标不稳定、退避、人工验证、选择器配置化、录制回放测试；不承诺 SLA                          |
| R2  | 微信发图协议未补齐           | 上传响应不符、图片不可见                      | 先协议 spike；未通过就保持不支持并降级，绝不把 API 200 当作图片已可见                    |
| R3  | B站小号权限不足            | 已登录但私信/评论被拒                       | 凭据与发送健康分离；本人确认目标后做手动自测；不盲目重试验证码                               |
| R4  | 身份归并错误造成串号          | 同昵称、不稳定 UID、会话切换异常                | 默认不合并、fail closed、预览确认、可撤销、事实隔离、负样本验收                         |
| R5  | 跨系统发送造成重复回复         | 超时后平台实际已发出                        | outbox 租约与幂等键；`unknown` 人工核对；不承诺分布式 exactly-once              |
| R6  | 记忆注入膨胀或污染           | token 超预算、历史内容诱导工具                | K/N/摘要上限、来源标记、最小工具权限、预算裁剪、注入测试                                |
| R7  | 串行队列导致延迟            | 同线程长任务、跨线程 CLI 过多                 | 同线程串行、跨线程有界并发、队列指标与上限、超时取消自己的子进程                              |
| R8  | UI 与网关生命周期耦合        | 关窗全掉线、重启重复轮询                      | 分进程、单实例、受控退出、UI 重接管、网关健康检查                                    |
| R9  | 本地 Web 被恶意页面或其他进程操作 | DNS rebinding、跨源 POST、token 泄漏    | loopback + Host/Origin + 每会话 token + CSP + 最小 preload；不开放远程调试 |
| R10 | 凭据或正文进入日志/导出        | 错误堆栈含 cookie，诊断包含全文               | 集中脱敏、秘密只写不读、导出预览、敏感标记自动测试                                     |
| R11 | 数据迁移或双写分裂           | SQLite 与 JSON 数量不一致               | 单一运行存储、顺序迁移、备份恢复、禁止静默 JSON 兜底                                 |
| R12 | 外部依赖升级破坏兼容性         | Electron/Playwright/Reasonix 行为变化 | 锁版本、记录运行时版本、分阶段升级、旧基线 + 适配器 smoke                             |
| R13 | 打包后路径/权限变化          | 安装目录只写、DPAPI 换用户失效                | 可写数据放 `%LOCALAPPDATA%`，资源与数据分根，非管理员测试，重新登录提示                  |
| R14 | 模型成本失控              | 主回复后重复抽取、无限重试                     | 所有模型调用计预算、最大尝试次数、后台抽取可被预算暂停                                   |

---

## 15. 现有代码改造映射

| 现有模块                           | 目标改造                                                                                 |
| ------------------------------ | ------------------------------------------------------------------------------------ |
| `gateway/models.py`            | 保留统一消息结构和 `session_key` 兼容读取；新增 account/identity/thread 解析结果与明确来源，不让 brain 直接猜数据库 ID |
| `gateway/router.py`            | 过滤前置、持久去重、落库后入队、同 thread 串行、跨 thread 有界并发、状态机与错误分类                                   |
| `gateway/brain.py`             | `SessionStore` 替换为 SQLite 记忆读取；`build_prompt` 按 §8 重排；JSON 仅做显式导入/导出                 |
| `gateway/server.py`            | 扩展 `/api/v1`、鉴权/Host/Origin/CSP、分页、异步 outbox；保留旧 MCP 端点的迁移兼容                         |
| `gateway/main.py`              | 独立进程生命周期、账号级热重载、单实例、状态上报、受控退出； `_supervise` 增加错误分类                                   |
| `gateway/adapters/base.py`     | 增加 `capabilities()`、健康状态、检查点与登录 provider 接口；不让能力 `unknown` 变成 `true`                 |
| `gateway/adapters/wechat*.py`  | token/context 入 vault；official/wxauto 命名空间隔离；wxauto 版本前置检查与弱身份 fail closed           |
| `gateway/adapters/bilibili.py` | cookie 入 vault；通知去重/检查点进入统一事务；保留现有平台协议行为与轮询下限                                        |
| `gateway/adapters/douyin.py`   | 首轮登记、稳定身份/消息键、挑战状态、发送回读与 `unknown`；未探明选择器不得乱点                                        |
| `gateway/adapters/qq.py`       | OneBot 健康与 NapCat 联动；图片能力在真机复核后再标 supported                                          |
| `gateway/token.py`             | 旧控制 token 向当前用户保护的会话/保险库机制迁移；URL 和日志永不携带 token                                       |
| `scripts/*login.py`            | 保留 CLI 外壳，内部调用 `login/providers/*`，不再各自维护一套相互漂移的登录逻辑                                 |
| `scripts/ilink-gateway.vbs`    | 改为无黑框启动 Electron 发布入口，不另起第二套网关                                                       |
| `config.example.toml`          | 新增加配置段与安全示例；不修改或提交真实 `config.toml` 中的私人值                                             |

四平台协议细节、DoH、现有 token 兼容与旧测试不是「冻结区」，而是「不得被无关重构破坏的行为基线」。必要改动必须带聚焦测试和迁移说明。

### 15.1 打包数据位置

- 开发模式默认沿用项目 `data/`，真实配置继续不提交。
- 发布模式可写数据默认放 `%LOCALAPPDATA%\iLink Studio\`，其下分 `data/` 与 `logs/`；资源从只读发布目录解析。
- 不使用当前工作目录推断资源；Python  frozen 资源按 `__file__` / `sys._MEIPASS` 规则处理，可执行程序位置用 `sys.executable`。
- `mailbox.db`、`vault.db`、贴纸二进制和日志不得打进只读资源目录；升级不覆盖用户数据。

---

## 16. 需求追踪

| 目标        | 主要需求             | 里程碑   | 核心验收                 |
| --------- | ---------------- | ----- | -------------------- |
| G1 登录     | FR-1、FR-6.5、FR-7 | M0、M4 | AC-01～04、23、26       |
| G2 消息库    | FR-2、§9 API      | M1    | AC-05～11、24          |
| G3 记忆     | FR-3、§4、§8       | M2、M5 | AC-12～15、18          |
| G4 表情     | FR-4             | M3    | AC-16～17             |
| G5 软件化    | FR-6、FR-7、§10    | M0、M4 | AC-04、20、23、26～27    |
| G6 可靠性/隐私 | FR-2、FR-5、§9～11  | 全阶段   | AC-05～09、16、19～22、25 |

> 实施工单沿用 `FR-1.x～FR-7.x` 编号。后续修改编号时同步本表与 AC，不允许只有需求文字没有验收。

---

## 17. 决策记录

| #  | 决策                              | 状态           | 影响                                      |
| -- | ------------------------------- | ------------ | --------------------------------------- |
| D1 | 本地 Web 控制台 + Electron 窗口/托盘     | 已拍板，保留       | Vite 构建产物由 8787 托管；Electron 不承担业务状态     |
| D2 | 微信双通道：默认 `official`，可切 `wxauto` | 已拍板，并于本轮再次确认 | 不改成「全部非官方」；两通道隔离；本机 4.x 不支持 wxauto 必须明示 |
| D3 | 无缝衔接交付 L1+L2，不做 L3              | 已拍板，保留       | 共享记忆和上下文，不自动跨平台转发                       |
| D4 | Electron 与 Python 网关分进程         | 实施基线         | 解决 R8 与「单进程」矛盾；显式退出才停止网关                |
| D5 | SQLite 是唯一运行时业务库                | 实施基线         | JSON 只做导入/导出；数据库故障不静默换存储                |
| D6 | 跨平台身份只手动合并，私聊/群/评论隔离            | 实施基线         | 宁可漏接，不可串人；合并可撤销                         |
| D7 | 出站引入 `unknown`，不确定时不自动重发        | 实施基线         | 用可见人工恢复替代不现实的 exactly-once 承诺           |
| D8 | DPAPI 失败不退回明文                   | 实施基线         | 解密失败要求重新登录；备份凭据不承诺跨用户/机器可恢复             |

D1～D3 的变更需要本人重新拍板；D4～D8 是为满足既定安全与可靠性目标补上的工程约束。

---

## 18. 变更记录与术语

### 18.1 变更记录

| 版本      | 日期         | 变更                                                                                   |
| ------- | ---------- | ------------------------------------------------------------------------------------ |
| v0.1    | 2026-10-25 | 初稿：现状、四大硬伤、功能需求、数据模型、里程碑、风险                                                          |
| v0.1-d1 | 2026-10-25 | 拍板 Electron、微信双通道、L1+L2                                                              |
| v0.2    | 2026-10-25 | 核对实际代码与 306 项基线；补进程边界、状态机、身份隔离、outbox、迁移/FTS 约束、API 安全、数据路径、验收矩阵与需求追踪；修正未经证实的全平台能力表述 |

### 18.2 术语

| 词            | 含义                                   |
| ------------ | ------------------------------------ |
| 小号           | 各平台用于机器人收发的独立账号，与本人主号分离              |
| 通道 / channel | 接入平台的技术路径，如 OneBot、iLink、cookie、RPA  |
| account      | 一个 `platform + channel` 的接入槽位，不是现实人物 |
| identity     | 平台账号标识；稳定性不足时必须标 provisional         |
| person       | 经本人确认的现实人物，可绑定多个 identity            |
| conversation | 某接入槽位上的具体平台会话                        |
| thread       | 记忆隔离边界；私聊可按确认后的 person 跨平台归并         |
| L1 / L2 / L3 | 记忆共享 / 上下文接续 / 消息桥接；v1 仅前两者          |
| outbox       | 待发送及发送结果记录，与入站处理状态分开                 |
| unknown      | 平台结果不确定；不知道是否已发出，禁止自动重发              |
| degraded     | 原能力不可用后以明确替代形式完成，例如图片变文字             |
| backlog      | 首次同步看到的历史消息，只登记、不因登记动作自动回复           |
| 降级           | 必须可见且有日志，不等于静默失败                     |
| 全量落库         | 对产品允许保存的消息全量保存；非白名单仍只存最少元数据          |

---

## 附录 A · 技术依据

以下是实施时应重新核对的官方资料，不作为真机能力已验证的替代证据：

- Electron Security：<https://www.electronjs.org/docs/latest/tutorial/security>
- Electron `app`（单实例、登录启动等）：<https://www.electronjs.org/docs/latest/api/app>
- SQLite FTS5（trigram、external content、同步删除）：<https://www.sqlite.org/fts5.html>
- Windows `CryptProtectData`：<https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata>
- PyInstaller 运行时资源路径：<https://pyinstaller.org/en/stable/runtime-information.html>

本次环境抽查：Python 自带 SQLite 为 3.50.4，FTS5 trigram 可创建并命中连续中文「山中猛虎」。少于 3 个 Unicode 字符的全文 `MATCH` 仍须按官方限制走短词回退，不能以一次中文长词实验推断所有查询都适用。
