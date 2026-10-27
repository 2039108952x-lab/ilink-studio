export type Account = {
  id: number;
  platform: 'qq' | 'wechat' | 'bilibili' | 'douyin';
  channel: string;
  display_name: string;
  platform_uid: string;
  expected_uid: string;
  auth_state: string;
  connection_state: string;
  send_health: string;
  enabled: boolean;
  is_active_channel: boolean;
  credentials_configured: boolean;
  last_auth_ok_at: number | null;
  last_send_error_safe: string | null;
  capabilities: Record<string, string>;
  prerequisite?: { supported: boolean; reason: string };
};

export type LoginSession = {
  id: string;
  account_id: number;
  state: string;
  message: string;
  platform_uid: string;
  verification_required: boolean;
  qr_image: string | null;
};

export type APIError = { code: string; message: string; retryable: boolean };
export type APIResponse<T> = { status: number; body: T & { error?: APIError } };

export type AutostartResult = { enabled: boolean; applied: boolean; reason: string };
export type AutostartStatus = {
  enabled: boolean;
  mode: 'dev' | 'packaged';
  path: string;
  args: string[];
};

// ---- 内容雷达（刷抖音）----
export type RadarQuota = { per_day: number; used: number; left: number };
export type RadarIdentity = {
  ok: boolean; logged_in?: boolean; nick?: string; uid?: string;
  error?: string; code?: string;
};
export type RadarSummary = {
  seen: number; picked: number; uncertain: number;
  dropped: Record<string, number>; elapsed: number; note: string;
};
export type RadarNode = { ok: boolean; path: string; version: string; error: string };
export type RadarStatus = {
  platform: string; quota: RadarQuota; report: string; report_day: string;
  has_report: boolean; running: boolean; ready: boolean;
  identity: RadarIdentity; last: RadarSummary | null; node?: RadarNode;
};
export type RadarComment = { text: string; likes: number };
export type RadarCandidate = {
  item_id: string; title: string; author: string; duration_sec: number | null;
  url: string; stats: Record<string, number>; score: number;
  worth: boolean | null; reason: string; tags: string[]; error: string;
  comments: RadarComment[];
};
export type RadarCandidates = {
  seen: number; picked: RadarCandidate[]; uncertain: RadarCandidate[];
  dropped: Record<string, number>; note: string;
};
export type RadarRound = {
  ok: boolean; report: string; quota: RadarQuota; summary: RadarSummary;
  candidates: RadarCandidates;
};
export type RadarReport = { day: string; path: string; markdown: string };
export type RadarShare = {
  item_id: string; to: string; sent: boolean; preview: string; detail: string;
};
export type RadarDoctorItem = { item: string; state: string; detail: string; fix: string };
export type RadarDoctor = {
  ok: boolean; items?: RadarDoctorItem[]; error?: string; code?: string;
  must_fix?: string[]; hint?: string;
};
export type RadarSettings = {
  top: number; notify: string; webhook_url: string;
  has_webhook: boolean; per_day: number; channels: string[];
};

// ---- B 站（刷B站）----
export type RadarPlatform = 'douyin' | 'bilibili';
/** 「它刷过的视频」：一条挑中的条目（从日报反解出来的） */
export type RadarVideo = {
  bvid: string; title: string; up: string; url: string;
  play: string; duration: string; grade: string; reason: string;
  one_line: string; day: string; status: string;
};
export type RadarVideos = {
  items: RadarVideo[]; total: number; page: number; size: number; pages: number;
  grade: string; status: string; grades: string[]; statuses: string[]; note?: string;
};
/** 四页签里的一项设置 */
export type RadarField = {
  key: string; label: string; kind: 'bool' | 'int' | 'list' | 'str' | string;
  options: string[] | null;
};
export type RadarSettingsView = {
  values: Record<string, unknown>;
  tabs: Record<string, RadarField[]>;
  tab_labels: Record<string, string>;
  dm_modes: string[];
  report_styles: string[];
  override_file: string;
};

declare global {
  interface Window {
    ilink: {
      request<T>(path: string, method?: string, body?: unknown): Promise<APIResponse<T>>;
      chooseSticker(): Promise<string | null>;
      quit(): Promise<void>;
      openLogFolder(): Promise<void>;
      chooseBackend(): Promise<string | null>;
      getAutostart(): Promise<AutostartStatus>;
      setAutostart(enabled: boolean): Promise<AutostartResult>;
    };
  }
}

export async function request<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  if (!window.ilink) {
    throw new Error('请从 iLink Studio 桌面窗口打开；普通浏览器没有本机操作权限');
  }
  const response = await window.ilink.request<T>(path, method, body);
  if (response.status < 200 || response.status >= 300) {
    throw new Error(response.body.error?.message || `请求失败（${response.status}）`);
  }
  return response.body;
}

export const authLabels: Record<string, string> = {
  unbound: '未绑定', waiting_scan: '等待扫码', awaiting_confirm: '等待确认',
  completing: '正在完成', bound: '凭据已绑定', needs_verification: '需人工验证',
  expired: '凭据过期', revoked: '已撤销', failed: '登录失败', cancelled: '已取消',
};
export const connectionLabels: Record<string, string> = {
  stopped: '已停止', starting: '启动中', online: '连接正常',
  reconnecting: '重连中', degraded: '降级', error: '连接异常',
};
export const sendLabels: Record<string, string> = {
  unknown: '未验证', success: '最近成功', failed: '最近失败', unsupported: '能力不支持',
};

export type Message = {
  id: number;
  direction: 'in' | 'out';
  platform: 'qq' | 'wechat' | 'bilibili' | 'douyin';
  platform_msg_id: string | null;
  body: string | null;
  attachments: { kind: string; path?: string; name?: string }[];
  storage_class: 'full' | 'metadata_only';
  processing_status: string;
  skip_reason: string | null;
  error_code: string | null;
  imported: boolean;
  reply_policy: 'normal' | 'never';
  occurred_at: number | null;
  ingested_at: number;
  time_source: 'platform' | 'received' | 'import';
  conversation_id: number;
  thread_id: number;
  conversation_kind: 'private' | 'group' | 'comment';
  chat_id: string;
  scope: string;
  person_name: string;
  sender_name: string;
  sender_uid: string;
  outbox?: { id: number; state: string; requested_kind: string; effective_kind: string | null; error_code: string | null; error_detail_safe: string | null } | null;
};

export type MessagePage = { items: Message[]; next_cursor: string | null; slow: boolean };
export type Conversation = {
  id: number; kind: string; chat_id: string; account_id: number; scope: string;
  thread_id: number; title: string; message_count: number; last_at: number | null;
  review_count: number;
};
export type InboxStats = {
  inbound: Record<string, number>;
  outbox: Record<string, number>;
  conversations: number;
};

export const statusLabels: Record<string, string> = {
  received: '已入库', queued: '排队中', processing: '生成中', replied: '已回复',
  skipped: '已跳过', failed: '失败', needs_review: '待核对', outbound: '已发出',
};
export const skipLabels: Record<string, string> = {
  backlog: '首次同步历史', reply_paused: '暂停回复期间', not_allowlisted: '非白名单',
  provisional_identity: '只有昵称', empty_body: '空消息', empty_reply: '模型没说话',
  reply_policy_never: '不自动回复', imported: '旧数据导入',
};
export const outboxLabels: Record<string, string> = {
  draft: '草稿', pending: '待发送', sending: '发送中', sent: '已送达',
  degraded: '降级发出', failed: '发送失败', cancelled: '已取消', unknown: '结果不明',
};

// ---- M2：身份与记忆 ----

export type IdentityItem = {
  id: number;
  person_id?: number;
  platform: 'qq' | 'wechat' | 'bilibili' | 'douyin';
  platform_uid: string;
  id_kind: 'stable' | 'opaque' | 'provisional';
  display_name: string | null;
};

export type Person = {
  id: number;
  display_name: string;
  note: string;
  is_self: boolean;
  identities: IdentityItem[];
  platforms: string[];
  private_thread_id: number | null;
  message_count: number;
  last_at: number | null;
  provisional_only: boolean;
};

export type PersonCandidate = Person & {
  same_name: boolean;
  cross_platform: boolean;
  eligible: boolean;
  reason: string;
};

export type Fact = {
  id: number;
  person_id: number;
  kind: 'event' | 'preference' | 'promise' | 'meme' | 'profile';
  content: string;
  confidence: number;
  status: 'active' | 'quarantined' | 'deleted';
  frozen: boolean;
  source_message_id: number | null;
  created_at: number;
  updated_at: number;
};

export type Persona = { name: string; content: string; version: number; updated_at: number };
export type PersonaVersion = {
  version: number; name: string; content: string; created_at: number; note: string | null;
};

export type MergePreview = {
  ok: boolean;
  source: { id: number; display_name: string; platforms: string[]; message_count: number };
  target: { id: number; display_name: string; platforms: string[]; message_count: number };
  identities: IdentityItem[];
  conversations: { id: number; kind: string; chat_id: string; platform: string; message_count: number }[];
  facts: { id: number; kind: string; content: string; action: 'move' | 'quarantine' }[];
  message_count: number;
  blocked: string[];
};

export type MergeRecord = {
  id: number;
  source_person_id: number;
  target_person_id: number;
  source_name: string | null;
  target_name: string | null;
  created_at: number;
  reverted_at: number | null;
};

export type PersonDetail = {
  person: Person;
  facts: Fact[];
  window: {
    id: number; direction: string; platform: string; sender_name: string;
    body: string; occurred_at: number | null; ingested_at: number;
  }[];
  candidates: PersonCandidate[];
};

export const idKindLabels: Record<string, string> = {
  stable: '平台 UID', opaque: '平台内句柄', provisional: '只有昵称',
};
export const factKindLabels: Record<string, string> = {
  event: '事件', preference: '偏好', promise: '约定', meme: '梗', profile: '资料',
};
export const factStatusLabels: Record<string, string> = {
  active: '生效中', quarantined: '已隔离', deleted: '已删除',
};

// ---- M3：表情与能力 ----

export type Sticker = {
  id: number;
  name: string;
  mime: string;
  w: number | null;
  h: number | null;
  bytes: number;
  sha256: string;
  tags: string[];
  enabled: boolean;
  source: string;
  source_message_id: number | null;
  created_at: number;
  updated_at: number;
};

export type CapabilitySet = {
  text_receive?: string;
  text_send?: string;
  image_receive?: string;
  image_send?: string;
  max_image_bytes?: number | null;
  needs_upload?: boolean | null;
  comment_send?: string;
  proactive_private?: string;
};

export type Capabilities = Record<string, CapabilitySet>;

export const capabilityLabels: Record<string, string> = {
  supported: '支持', degraded: '降级', unsupported: '不支持', unknown: '未确认',
};




export type NapcatState = {
  installed: boolean;
  dir: string;
  webui_url: string;
  webui_running: boolean;
  onebot_ws: string;
  onebot_running: boolean;
  launcher: string;
  accounts: string[];
  message: string;
};

export type NapcatQrcodeState = {
  available: boolean;
  data_url?: string;
  age_seconds?: number;
  decoded_url?: string;
  reason?: string;
};

export type AllowlistCandidate = {
  platform: string; platform_uid: string; id_kind: string;
  name: string; hits: number; last_at: number;
};
export type AllowlistEntries = {
  allow_all: boolean;
  entries: Record<string, string[]>;
  candidates?: AllowlistCandidate[];
};

// ---- M4：首启与运行状态 ----

export type Bootstrap = {
  app: string;
  version: string;
  sections: string[];
  ai: { ready: boolean; backend: string; model: string; command?: string; reason?: string; workspace?: string };
  onboarding: { confirmed: boolean; data_dir: string; autostart: boolean; risks: string[] };
  privacy: string;
};

// ---- M4：日志与诊断 ----

export type LogEntry = { ts: string; level: string; logger: string; message: string };
export type Diagnostics = {
  app: { name: string; version: string };
  database: { file?: string; bytes?: number; schema?: number; queue?: Record<string, number>; health?: { ok: boolean; error_code: string | null } };
  metrics: {
    counters: Record<string, number>;
    latency_seconds: Record<string, { count: number; p50: number; p95: number }>;
    tokens: { day: string; input: number; output: number; total: number; estimated_calls: number };
    cost_usd: number | null;
    uptime_seconds: number;
  };
  adapters: Record<string, { connection_state: string | null; send_health: string | null; capabilities: Record<string, unknown> }>;
  onboarding: { confirmed: boolean };
  budget: { daily_tokens: number; daily_cost_usd: number; platform_per_minute: number; queue_limit: number };
  privacy: string;
};
