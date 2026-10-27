import { render } from 'preact';
import { useCallback, useEffect, useState } from 'preact/hooks';
import {
  Account, authLabels, AutostartStatus, capabilityLabels, Capabilities, connectionLabels, Conversation,
  Fact, factKindLabels, factStatusLabels, idKindLabels, InboxStats, LoginSession,
  MergePreview, MergeRecord, Message, MessagePage, outboxLabels, Person, PersonCandidate,
  AllowlistCandidate, AllowlistEntries, Bootstrap, Diagnostics, LogEntry, NapcatQrcodeState, NapcatState, Persona, PersonaVersion, request, sendLabels, skipLabels,
  RadarCandidate, RadarCandidates, RadarDoctor, RadarField, RadarPlatform, RadarReport,
  RadarRound, RadarSettings, RadarSettingsView, RadarShare, RadarStatus, RadarVideo, RadarVideos,
  Sticker, statusLabels,
} from './api';
import './style.css';

type Section = 'accounts' | 'inbox' | 'conversations' | 'radar' | 'memory' | 'stickers' | 'logs';
const sections: [Section, string, string][] = [
  ['accounts', '账号', '连接与登录'], ['inbox', '收件箱', '消息记录'],
  ['conversations', '会话', '身份与上下文'], ['radar', '内容雷达', '刷抖音 / 刷B站'],
  ['memory', '记忆', '人格与事实'],
  ['stickers', '表情包', '素材与降级'], ['logs', '日志', '运行与诊断'],
];
const platformNames = { qq: 'QQ', wechat: '微信', bilibili: 'B站', douyin: '抖音' };
const notifyLabels: Record<string, string> = {
  file: '只写文件', toast: '系统通知', webhook: 'Webhook', both: '文件 + 系统通知',
};
const channelNames: Record<string, string> = { onebot: 'NapCat / OneBot 11', official: '官方 iLink', wxauto: 'wxauto', cookie: 'Cookie', rpa: 'Playwright RPA' };

function Badge({ type, children }: { type: 'good' | 'warn' | 'muted' | 'bad'; children: unknown }) {
  return <span class={`badge ${type}`}><i />{children}</span>;
}
function healthType(value: string) {
  if (['bound', 'online', 'success'].includes(value)) return 'good';
  if (['expired', 'failed', 'revoked', 'error'].includes(value)) return 'bad';
  if (['unbound', 'unknown', 'stopped', 'cancelled', 'unsupported'].includes(value)) return 'muted';
  return 'warn';
}
function AccountCard({ account, napcat, onRefresh, onError }: { account: Account; napcat?: NapcatState | null; onRefresh: () => void; onError: (message: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [channelBusy, setChannelBusy] = useState(false);
  const [napcatBusy, setNapcatBusy] = useState(false);
  const [qrcode, setQrcode] = useState<NapcatQrcodeState | null>(null);
  const [session, setSession] = useState<LoginSession | null>(null);
  const [verifyCode, setVerifyCode] = useState('');
  const [uid, setUid] = useState('');
  const [probeMessage, setProbeMessage] = useState('');
  const activeSession = session && !['bound', 'failed', 'cancelled', 'expired'].includes(session.state);
  const canEnable = account.auth_state === 'bound' && account.is_active_channel
    && account.prerequisite?.supported !== false;

  const pollSession = useCallback(async (id: string) => {
    try {
      const next = await request<LoginSession>(`/api/v1/login-sessions/${id}`);
      setSession(next);
      if (['bound', 'failed', 'cancelled', 'expired'].includes(next.state)) onRefresh();
    } catch (error) { onError((error as Error).message); }
  }, [onRefresh, onError]);
  useEffect(() => {
    if (!session || !activeSession) return;
    const timer = window.setInterval(() => pollSession(session.id), 1000);
    return () => window.clearInterval(timer);
  }, [session?.id, activeSession, pollSession]);

  async function login() {
    setBusy(true); setProbeMessage('');
    try {
      const next = await request<LoginSession>(`/api/v1/accounts/${account.id}/login`, 'POST', {});
      setSession(next);
    } catch (error) { setProbeMessage((error as Error).message); }
    finally { setBusy(false); }
  }
  async function action(action: 'cancel' | 'verify' | 'confirm') {
    if (!session) return;
    setBusy(true);
    try {
      const body = action === 'verify' ? { code: verifyCode } : action === 'confirm' ? { platform_uid: uid } : {};
      setSession(await request<LoginSession>(`/api/v1/login-sessions/${session.id}/${action}`, 'POST', body));
      if (action === 'verify') setVerifyCode('');
    } catch (error) { onError((error as Error).message); }
    finally { setBusy(false); }
  }
  async function probe() {
    setBusy(true);
    try {
      const result = await request<{ message?: string }>(`/api/v1/accounts/${account.id}/probe`, 'POST', {});
      setProbeMessage(result.message || '只读探活完成；发送能力另行验证');
      onRefresh();
    } catch (error) { setProbeMessage((error as Error).message); onRefresh(); }
    finally { setBusy(false); }
  }
  useEffect(() => {
    const waiting = account.channel === 'onebot' && napcat?.webui_running === true
      && napcat?.onebot_running === false;
    if (!waiting) { setQrcode(null); return; }
    let alive = true;
    const tick = async () => {
      try {
        const next = await request<NapcatQrcodeState>('/api/v1/napcat/qrcode');
        if (alive) setQrcode(next);
      } catch { if (alive) setQrcode(null); }
    };
    tick();
    const timer = window.setInterval(tick, 5000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [account.channel, napcat?.webui_running, napcat?.onebot_running]);

  async function launchNapcat() {
    // 这一步会重启 QQ 并重新注入，已登录的 QQ 要重新扫码 —— 必须先问一句。
    // 真机上误点过一次，直接把登录好的 QQ 踢下线了。
    const confirmed = window.confirm(
      '启动 NapCat 会重启 QQ 并重新注入，已登录的 QQ 需要重新扫码。\n\n只有 NapCat 完全没在运行时才需要点这个；如果只是等扫码，看界面上的二维码就行。\n\n继续吗？');
    if (!confirmed) return;
    setNapcatBusy(true); setProbeMessage('');
    try {
      const result = await request<{ message: string }>('/api/v1/napcat/launch', 'POST', {});
      setProbeMessage(result.message);
      // 启动后轮询：QQ 重启 + 注入需要几十秒，起来后按钮会自己变成「检查 NapCat 状态」
      let tries = 0;
      const timer = window.setInterval(() => {
        tries += 1;
        onRefresh();
        if (tries >= 20) window.clearInterval(timer);
      }, 3000);
    } catch (error) { setProbeMessage((error as Error).message); }
    finally { setNapcatBusy(false); }
  }
  async function toggleChannel() {
    setChannelBusy(true);
    try {
      const next = await request<{ enabled: boolean; connection_state: string }>(
        `/api/v1/accounts/${account.id}/enabled`, 'PUT', { enabled: !account.enabled });
      setProbeMessage(next.enabled
        ? '已启用：连接状态会在握手成功后变为「连接正常」'
        : '已停用；此账号不再接收或发送');
      onRefresh();
    } catch (error) { onError((error as Error).message); onRefresh(); }
    finally { setChannelBusy(false); }
  }
  const loginHint = account.channel === 'onebot' ? '检查 NapCat' : account.channel === 'wxauto' ? '当前客户端不支持' : account.channel === 'official' ? '扫码绑定微信小号' : '打开专用登录窗口';  // 启用按钮为什么点不了：把原因写在按钮旁边，而不是留一个灰掉的按钮。
  const enableHint = account.enabled ? ''
    : account.prerequisite?.supported === false ? (account.prerequisite.reason || '此通道不支持')
      : !account.is_active_channel ? '当前激活的不是这条通道，先切换过去再启用'
        : account.auth_state !== 'bound'
          ? (account.channel === 'onebot'
            ? '先在 NapCat 里登录 QQ，再点「只读探活」核对机器人 UID'
            : '先完成登录并核对机器人 UID')
          : '';
  return <article class="account-card">
    <div class="card-top">
      <div class={`platform-icon ${account.platform}`}>{platformNames[account.platform].slice(0, 1)}</div>
      <div class="card-title"><h3>{platformNames[account.platform]}</h3><span>{channelNames[account.channel]}</span></div>
      {account.is_active_channel && <span class="active-channel">当前通道</span>}
    </div>
    <div class="account-identity">
      <span class="label">机器人账号</span>
      <span>{account.platform_uid || '尚未绑定'}</span>
      {account.credentials_configured && <span class="credential-note">已存入保险库</span>}
    </div>
    <div class="health-grid">
      <div><span>凭据</span><Badge type={healthType(account.auth_state)}>{authLabels[account.auth_state] || account.auth_state}</Badge></div>
      <div><span>连接</span><Badge type={healthType(account.connection_state)}>{connectionLabels[account.connection_state] || account.connection_state}</Badge></div>
      <div><span>发送健康</span><Badge type={healthType(account.send_health)}>{sendLabels[account.send_health] || account.send_health}</Badge></div>
      <div><span>自动收发</span><Badge type={account.enabled ? 'good' : 'muted'}>{account.enabled ? '已启用' : '未启用'}</Badge></div>
    </div>
    {account.channel === 'onebot' && qrcode?.available && qrcode.data_url &&
      <div class="qr-block">
        <img class="qr-image" src={qrcode.data_url} alt="NapCat 登录二维码" />
        <span class="muted-note">用手机 QQ 扫码登录；二维码每几秒自动刷新{qrcode.decoded_url ? '。' : ''}</span>
        {qrcode.decoded_url && <span class="block-note">扫不出来时，把这个链接贴到二维码网站：{qrcode.decoded_url}</span>}
      </div>}
    {account.channel === 'onebot' && qrcode && !qrcode.available && napcat?.webui_running &&
      <div class="muted-note">正在等 NapCat 出二维码（{qrcode.reason || '准备中'}）…</div>}
    {account.prerequisite && <div class="notice warning">{account.prerequisite.reason}</div>}
    {account.channel === 'onebot' && <div class={`notice ${napcat?.onebot_running ? 'success' : 'warning'}`}>
      {napcat ? napcat.message : '正在检测本机 NapCat…'}
      {napcat?.installed && !napcat.onebot_running &&
        <span class="block-note">NapCat 目录：{napcat.dir}　WebUI（NapCat 跑起来后可用）：{napcat.webui_url}</span>}
    </div>}
    {probeMessage && <div class="notice">{probeMessage}</div>}
    {session && activeSession && <div class="login-panel">
      <div class="login-status"><strong>{authLabels[session.state] || session.state}</strong><span>{session.message}</span></div>
      {session.qr_image && <img class="qr-image" src={session.qr_image} alt="本次登录二维码" />}
      {session.verification_required && <div class="inline-form"><input aria-label="数字验证值" inputMode="numeric" placeholder="微信显示的数字验证值" value={verifyCode} onInput={e => setVerifyCode(e.currentTarget.value)} /><button class="small-button" disabled={!verifyCode} onClick={() => action('verify')}>提交验证</button></div>}
      {session.state === 'awaiting_confirm' && (account.platform === 'douyin' || account.channel === 'onebot') && <div class="inline-form">
        {account.platform === 'douyin'
          ? <input aria-label="核对后的抖音 UID" placeholder="核对后的抖音 UID" value={uid} onInput={e => setUid(e.currentTarget.value)} />
          : <span class="muted-note">NapCat 报告的机器人 QQ：{session.platform_uid || '未知'}</span>}
        <button class="small-button"
          disabled={account.platform === 'douyin' && !uid.trim()}
          onClick={() => action('confirm')}>{account.platform === 'douyin' ? '确认小号' : '确认这个 QQ'}</button>
      </div>}
      <button class="text-button" onClick={() => action('cancel')}>取消登录</button>
    </div>}
    {session && !activeSession && <div class={`notice ${session.state === 'bound' ? 'success' : 'warning'}`}>{session.message}</div>}
    <div class="card-actions">
      <button class="primary-button" disabled={busy || napcatBusy || !!activeSession || account.channel === 'wxauto'} onClick={account.channel === 'onebot' && !(napcat?.onebot_running) && !(napcat?.webui_running) ? launchNapcat : login}>{account.channel === 'onebot' ? (napcat?.onebot_running ? '检查 NapCat 状态' : napcat?.webui_running ? '等待 QQ 扫码（点此重查）' : '启动 NapCat') : activeSession ? '登录进行中' : session?.state === 'bound' ? '重新登录' : loginHint}</button>
      <button class="secondary-button" disabled={busy || !!activeSession} onClick={probe}>只读探活</button>
      <button class="secondary-button" disabled={channelBusy || !!activeSession || (!account.enabled && !canEnable)} title={enableHint || '启用后连接与发送能力仍按平台实际握手结果展示'} onClick={toggleChannel}>{account.enabled ? '停用自动收发' : '启用自动收发'}</button>
    </div>
    {enableHint && <div class="notice">{enableHint}</div>}
    <p class="card-footnote">登录成功不等于能发消息。启用自动收发需在首启向导确认后进行；启用后仍需平台握手成功才算连接正常。</p>
  </article>;
}

function formatTime(message: Message) {
  const stamp = message.occurred_at ?? message.ingested_at;
  const text = new Date(stamp * 1000).toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  });
  return message.time_source === 'import' ? `导入于 ${text}` : text;
}

function InboxSection() {
  const [items, setItems] = useState<Message[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [stats, setStats] = useState<InboxStats | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [platform, setPlatform] = useState('');
  const [status, setStatus] = useState('');
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const [slow, setSlow] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const load = useCallback(async (nextCursor: string | null, append: boolean) => {
    const params = new URLSearchParams({ limit: '30' });
    if (platform) params.set('platform', platform);
    if (status) params.set('status', status);
    if (search) params.set('q', search);
    if (append && nextCursor) params.set('cursor', nextCursor);
    setLoading(true);
    try {
      const page = await request<MessagePage>(`/api/v1/messages?${params.toString()}`);
      setItems(previous => append ? [...previous, ...page.items] : page.items);
      setCursor(page.next_cursor);
      setSlow(page.slow);
      setError('');
    } catch (err) { setError((err as Error).message); }
    finally { setLoading(false); }
  }, [platform, status, search]);

  const refreshMeta = useCallback(async () => {
    // stats 和「最近会话」互不拖累：以前用 Promise.all，一个失败两个结果都丢，
    // 界面上就固定显示 0 / 0 / 0 —— 库里有消息也看不出来。
    const [statsResult, conversationResult] = await Promise.allSettled([
      request<InboxStats>('/api/v1/stats'),
      request<{ conversations: Conversation[] }>('/api/v1/conversations?limit=20'),
    ]);
    if (statsResult.status === 'fulfilled') setStats(statsResult.value);
    if (conversationResult.status === 'fulfilled') setConversations(conversationResult.value.conversations);
    for (const result of [statsResult, conversationResult]) {
      if (result.status === 'rejected') {
        setError((result.reason as Error).message);
        break;
      }
    }
  }, []);

  useEffect(() => { load(null, false); }, [load]);
  useEffect(() => { refreshMeta(); }, [refreshMeta]);

  async function confirm(message: Message) {
    try {
      await request(`/api/v1/messages/${message.id}/confirm`, 'POST', {});
      setNotice(`第 ${message.id} 条已确认，下一轮会按正常流程回复`);
      await load(null, false);
      refreshMeta();
    } catch (err) { setError((err as Error).message); }
  }
  async function saveSticker(message: Message) {
    const image = message.attachments.find(item => item.kind === 'image');
    if (!image) return;
    const fallback = (image.name || `消息${message.id}`).replace(/\.[^.]+$/, '');
    try {
      const result = await request<{ sticker: Sticker; created: boolean }>(
        '/api/v1/stickers', 'POST', { source_message_id: message.id, name: fallback },
      );
      setNotice(result.created
        ? `已入库为表情「${result.sticker.name}」（${result.sticker.id}）`
        : `这张图已经在库里：${result.sticker.name}（${result.sticker.id}）`);
    } catch (err) { setError((err as Error).message); }
  }
  async function allowSender(message: Message) {
    if (!message.sender_uid) return;
    try {
      await request('/api/v1/allowlist', 'POST', {
        platform: message.platform, platform_uid: message.sender_uid,
      });
      setNotice(`已允许 ${message.sender_name || message.sender_uid}（${platformNames[message.platform]}）：请对方再发一条，就会开始回复`);
      setError('');
    } catch (err) { setError((err as Error).message); }
  }
  async function backup() {
    try {
      const result = await request<{ path: string; integrity: boolean }>('/api/v1/backup', 'POST', {});
      setNotice(`备份完成（integrity=${result.integrity ? 'ok' : '失败'}）：${result.path}`);
      setError('');
    } catch (err) { setError((err as Error).message); }
  }
  async function importLegacy() {
    try {
      const result = await request<{ imported: number; skipped: number }>('/api/v1/imports/sessions', 'POST', {});
      setNotice(`旧数据导入完成：新增 ${result.imported} 条，跳过 ${result.skipped} 次`);
      await load(null, false);
      refreshMeta();
    } catch (err) { setError((err as Error).message); }
  }

  const inbound = stats?.inbound ?? {};
  const outbox = stats?.outbox ?? {};
  const filterButton = (active: boolean) => `filter-chip${active ? ' selected' : ''}`;

  return <div class="inbox-view">
    <section class="overview">
      <div>
        <span class="eyebrow">统一消息库</span>
        <h3>收到的每一句，先入库再回答。</h3>
        <p>正文只在这里；非白名单消息只留审计元数据。发送结果按平台回执分成已送达、明确失败和结果不明。</p>
      </div>
      <div class="overview-metrics">
        <div><strong>{stats?.conversations ?? 0}</strong><span>会话</span></div>
        <div><strong>{inbound.needs_review ?? 0}</strong><span>待核对</span></div>
        <div><strong>{(outbox.failed ?? 0) + (outbox.unknown ?? 0)}</strong><span>发送异常</span></div>
      </div>
    </section>

    <div class="inbox-toolbar">
      <div class="filter-group" aria-label="平台筛选">
        {[['', '全部'], ['qq', 'QQ'], ['wechat', '微信'], ['bilibili', 'B站'], ['douyin', '抖音']].map(([value, label]) =>
          <button key={value || 'all'} class={filterButton(platform === value)} onClick={() => setPlatform(value)}>{label}</button>)}
      </div>
      <div class="filter-group" aria-label="状态筛选">
        {[['', '全部状态'], ['needs_review', '待核对'], ['failed', '失败'], ['replied', '已回复']].map(([value, label]) =>
          <button key={value || 'any'} class={filterButton(status === value)} onClick={() => setStatus(value)}>{label}</button>)}
      </div>
      <form class="inline-form" onSubmit={event => { event.preventDefault(); setSearch(query.trim()); }}>
        <input aria-label="检索消息正文" placeholder="搜正文，长短词都行" value={query} onInput={event => setQuery(event.currentTarget.value)} />
        <button class="small-button" type="submit">检索</button>
      </form>
      <button class="secondary-button" onClick={backup}>备份数据库</button>
      <button class="secondary-button" onClick={importLegacy}>导入旧 sessions.json</button>
    </div>

    {error && <div class="error-banner" role="alert">{error}</div>}
    {notice && <div class="notice success">{notice}</div>}
    {slow && <div class="notice warning">1～2 字检索走精确子串匹配，结果准确但可能稍慢。</div>}

    <div class="inbox-body">
      <ul class="message-list">
        {items.map(message => <li key={message.id} class={`message-row ${message.direction}`}>
          <div class="message-meta">
            <span class={`badge ${message.direction === 'in' ? 'good' : 'muted'}`}>{message.direction === 'in' ? '收到' : '发出'}</span>
            <span class="message-platform">{platformNames[message.platform]}</span>
            <span>{message.direction === 'in' ? (message.sender_name || message.person_name) : '我'}</span>
            <span class="muted-note">{formatTime(message)}</span>
            <span class="badge muted">{statusLabels[message.processing_status] || message.processing_status}</span>
            {message.skip_reason && <span class="muted-note">{skipLabels[message.skip_reason] || message.skip_reason}</span>}
            {message.imported && <span class="muted-note">旧数据</span>}
          </div>
          <p class="message-body">{message.body ?? '（正文未保存：非白名单只留审计元数据）'}</p>
          {message.attachments.length > 0 && <p class="muted-note">附件 {message.attachments.length} 个</p>}
          {message.outbox && <p class="muted-note">发送：{outboxLabels[message.outbox.state] || message.outbox.state}{message.outbox.error_detail_safe ? ` · ${message.outbox.error_detail_safe}` : ''}</p>}
          {message.processing_status === 'needs_review' &&
            <button class="small-button" onClick={() => confirm(message)}>确认后生成回复</button>}
          {message.direction === 'in' && message.skip_reason === 'not_allowlisted' && message.sender_uid &&
            <button class="small-button" onClick={() => allowSender(message)}>允许 {message.sender_name || message.sender_uid}</button>}
          {message.direction === 'in' && message.attachments.some(item => item.kind === 'image') &&
            <button class="small-button" onClick={() => saveSticker(message)}>存为表情</button>}
        </li>)}
      </ul>
      <aside class="conversation-panel">
        <h4>最近会话</h4>
        {conversations.length === 0 && <p class="muted-note">还没有会话。开启一个平台并收到第一条消息后会出现在这里。</p>}
        {conversations.map(conversation => <button class="conversation-item" key={conversation.id} onClick={() => setPlatform(conversation.kind === 'private' ? '' : platform)}>
          <span>{conversation.title}</span>
          <small>{conversation.kind === 'private' ? '私聊' : conversation.kind === 'group' ? '群聊' : '评论'} · {conversation.message_count} 条{conversation.review_count ? ` · ${conversation.review_count} 待核对` : ''}</small>
        </button>)}
      </aside>
    </div>

    {items.length === 0 && !loading && !error && <div class="empty-state"><div class="empty-icon">·</div><h3>还没有消息</h3><p>消息先落库再交给模型；这里会显示每条消息的处理状态与发送结果。</p></div>}
    {loading && <div class="loading">正在读取本机消息库…</div>}
    {cursor && !loading && <button class="secondary-button load-more" onClick={() => load(cursor, true)}>加载更早的消息</button>}
  </div>;
}

function ConversationsSection() {
  const [persons, setPersons] = useState<Person[]>([]);
  const [merges, setMerges] = useState<MergeRecord[]>([]);
  const [sourceId, setSourceId] = useState<number | null>(null);
  const [candidates, setCandidates] = useState<PersonCandidate[]>([]);
  const [preview, setPreview] = useState<MergePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const load = useCallback(async () => {
    try {
      const [personResult, mergeResult] = await Promise.all([
        request<{ persons: Person[] }>('/api/v1/persons'),
        request<{ merges: MergeRecord[] }>('/api/v1/merges?limit=20'),
      ]);
      setPersons(personResult.persons);
      setMerges(mergeResult.merges);
      setError('');
    } catch (err) { setError((err as Error).message); }
  }, []);
  useEffect(() => { load(); }, [load]);

  async function openCandidates(person: Person) {
    setSourceId(person.id); setPreview(null); setNotice('');
    try {
      const result = await request<{ candidates: PersonCandidate[] }>(`/api/v1/persons/${person.id}/candidates`);
      setCandidates(result.candidates);
    } catch (err) { setError((err as Error).message); }
  }
  async function showPreview(target: PersonCandidate) {
    if (sourceId === null) return;
    setBusy(true);
    try {
      const result = await request<MergePreview>(
        `/api/v1/persons/${target.id}/merges`, 'POST', { source_person_id: sourceId },
      );
      setPreview(result);
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  }
  async function confirmMerge() {
    if (preview === null || sourceId === null) return;
    setBusy(true);
    try {
      const result = await request<{ merge_id: number }>(
        `/api/v1/persons/${preview.target.id}/merges`, 'POST',
        { source_person_id: sourceId, confirm: true, note: '界面确认' },
      );
      setNotice(`已合并（记录 #${result.merge_id}），不满意可以在这里撤销`);
      setPreview(null); setCandidates([]); setSourceId(null);
      await load();
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  }
  async function revertMerge(id: number) {
    setBusy(true);
    try {
      await request(`/api/v1/merges/${id}/revert`, 'POST', {});
      setNotice(`已撤销合并 #${id}，身份与会话按日志恢复`);
      await load();
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  }

  const crossPlatform = persons.filter(person => person.platforms.length > 1).length;
  return <div class="memory-view">
    <section class="overview">
      <div>
        <span class="eyebrow">身份边界</span>
        <h3>同一个人，只有你确认了才算同一个人。</h3>
        <p>默认每个平台身份各自一个人。合并只移动私聊身份与会话；群聊和评论永远不串。撤销严格按日志恢复。</p>
      </div>
      <div class="overview-metrics">
        <div><strong>{persons.length}</strong><span>人物</span></div>
        <div><strong>{crossPlatform}</strong><span>已跨平台</span></div>
        <div><strong>{merges.filter(item => !item.reverted_at).length}</strong><span>可撤销合并</span></div>
      </div>
    </section>
    {error && <div class="error-banner" role="alert">{error}</div>}
    {notice && <div class="notice success">{notice}</div>}
    <div class="person-grid">
      {persons.map(person => <article class="person-card" key={person.id}>
        <header>
          <h3>{person.display_name}</h3>
          <span class="muted-note">{person.message_count} 条私聊</span>
        </header>
        <div class="identity-list">
          {person.identities.map(identity => <span class="identity-chip" key={identity.id}>
            {platformNames[identity.platform]} · {identity.platform_uid}
            <small>{idKindLabels[identity.id_kind]}</small>
          </span>)}
          {person.identities.length === 0 && <span class="muted-note">身份已被合并走，可在下面的历史里撤销恢复</span>}
        </div>
        {person.provisional_only && <div class="notice warning">只有昵称的身份不参与合并</div>}
        <button class="secondary-button" onClick={() => openCandidates(person)}>合并到…</button>
        {sourceId === person.id && <div class="candidate-list">
          {candidates.length === 0 && <p class="muted-note">没有其他人物可以合并。</p>}
          {candidates.map(candidate => <button
            class="candidate-item" key={candidate.id} disabled={!candidate.eligible}
            onClick={() => showPreview(candidate)}>
            {candidate.display_name} · {candidate.platforms.map(item => platformNames[item as keyof typeof platformNames]).join(' / ')}
            <small>{candidate.eligible ? `${candidate.message_count} 条私聊` : candidate.reason}</small>
          </button>)}
        </div>}
      </article>)}
    </div>
    {preview && <section class="merge-preview">
      <h3>合并预览：{preview.source.display_name} → {preview.target.display_name}</h3>
      <p class="muted-note">
        会移动 {preview.identities.length} 个身份、{preview.conversations.length} 个私聊会话、{preview.message_count} 条消息；
        {preview.facts.filter(item => item.action === 'quarantine').length} 条归属不明的事实会被隔离。
      </p>
      <ul>
        {preview.identities.map(identity => <li key={identity.id}>
          {platformNames[identity.platform]} · {identity.platform_uid}（{idKindLabels[identity.id_kind]}）
        </li>)}
      </ul>
      {preview.blocked.length > 0 && <div class="error-banner">{preview.blocked.join('；')}</div>}
      <div class="inline-form">
        <button class="primary-button" disabled={!preview.ok || busy} onClick={confirmMerge}>确认合并</button>
        <button class="secondary-button" onClick={() => setPreview(null)}>再看看</button>
      </div>
    </section>}
    <div class="section-heading"><h3>合并历史</h3><span>撤销按日志恢复，不重新猜归属</span></div>
    <ul class="merge-list">
      {merges.length === 0 && <li class="muted-note">还没有合并记录。</li>}
      {merges.map(record => <li key={record.id} class="merge-row">
        <span>#{record.id} {record.source_name} → {record.target_name}</span>
        <span class="muted-note">{record.reverted_at ? '已撤销' : '生效中'}</span>
        {!record.reverted_at && <button class="small-button" disabled={busy} onClick={() => revertMerge(record.id)}>撤销</button>}
      </li>)}
    </ul>
  </div>;
}

function MemorySection() {
  const [persona, setPersona] = useState<Persona | null>(null);
  const [versions, setVersions] = useState<PersonaVersion[]>([]);
  const [name, setName] = useState('');
  const [content, setContent] = useState('');
  const [persons, setPersons] = useState<Person[]>([]);
  const [personId, setPersonId] = useState<number | null>(null);
  const [facts, setFacts] = useState<Fact[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const loadPersona = useCallback(async () => {
    try {
      const [current, history] = await Promise.all([
        request<Persona>('/api/v1/persona'),
        request<{ versions: PersonaVersion[] }>('/api/v1/persona/versions'),
      ]);
      setPersona(current); setName(current.name); setContent(current.content);
      setVersions(history.versions);
    } catch (err) { setError((err as Error).message); }
  }, []);
  const loadPersons = useCallback(async () => {
    try {
      const result = await request<{ persons: Person[] }>('/api/v1/persons');
      setPersons(result.persons);
      setPersonId(current => current ?? (result.persons[0]?.id ?? null));
    } catch (err) { setError((err as Error).message); }
  }, []);
  useEffect(() => { loadPersona(); loadPersons(); }, [loadPersona, loadPersons]);

  useEffect(() => {
    if (personId === null) { setFacts([]); return; }
    let alive = true;
    request<{ facts: Fact[] }>(`/api/v1/memory-facts?person_id=${personId}&status=`)
      .then(result => { if (alive) setFacts(result.facts); })
      .catch(err => { if (alive) setError((err as Error).message); });
    return () => { alive = false; };
  }, [personId]);

  async function savePersona() {
    setBusy(true);
    try {
      const next = await request<Persona>('/api/v1/persona', 'PUT', { name, content, note: '界面修改' });
      setPersona(next); setName(next.name); setContent(next.content);
      setNotice(`人格已保存为 v${next.version}；只影响之后的新消息`);
      await loadPersona();
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  }
  async function patchFact(id: number, body: Record<string, unknown>) {
    try {
      await request(`/api/v1/memory-facts/${id}`, 'PATCH', body);
      const result = await request<{ facts: Fact[] }>(`/api/v1/memory-facts?person_id=${personId}&status=`);
      setFacts(result.facts);
      setNotice('已更新；下一次 prompt 立即按新状态注入');
    } catch (err) { setError((err as Error).message); }
  }

  const activeCount = facts.filter(fact => fact.status === 'active').length;
  const quarantinedCount = facts.filter(fact => fact.status === 'quarantined').length;
  return <div class="memory-view">
    <section class="overview">
      <div>
        <span class="eyebrow">统一记忆</span>
        <h3>四个平台读同一份人格、同一套事实。</h3>
        <p>改人格只影响之后的新消息，历史回复不改写；事实被隔离或删除后，下一次 prompt 立即不再包含。</p>
      </div>
      <div class="overview-metrics">
        <div><strong>v{persona?.version ?? '—'}</strong><span>人格版本</span></div>
        <div><strong>{activeCount}</strong><span>生效事实</span></div>
        <div><strong>{quarantinedCount}</strong><span>已隔离</span></div>
      </div>
    </section>
    {error && <div class="error-banner" role="alert">{error}</div>}
    {notice && <div class="notice success">{notice}</div>}

    <section class="panel">
      <div class="section-heading"><h3>人格</h3><span>当前 v{persona?.version ?? '—'}，保存后只影响新消息</span></div>
      <div class="inline-form">
        <input aria-label="人格名字" value={name} onInput={event => setName(event.currentTarget.value)} />
        <button class="primary-button" disabled={busy || !name.trim() || !content.trim()} onClick={savePersona}>
          保存为新版本
        </button>
      </div>
      <textarea
        aria-label="人格内容" rows={4} class="persona-text"
        value={content} onInput={event => setContent(event.currentTarget.value)}
      />
      <details class="version-history">
        <summary>版本历史（{versions.length}）</summary>
        <ul>
          {versions.map(version => <li key={version.version}>
            v{version.version} · {version.name} · {new Date(version.created_at * 1000).toLocaleString('zh-CN')}
          </li>)}
        </ul>
      </details>
    </section>

    <section class="panel">
      <div class="section-heading"><h3>长期事实</h3><span>最多 12 条进 prompt；删除/隔离立即生效</span></div>
      <div class="filter-group">
        {persons.map(person => <button
          key={person.id}
          class={`filter-chip${personId === person.id ? ' selected' : ''}`}
          onClick={() => setPersonId(person.id)}>
          {person.display_name} · {person.platforms.join('/') || '无身份'}
        </button>)}
      </div>
      <ul class="fact-list">
        {facts.length === 0 && <li class="muted-note">这个人物还没有事实。M5 会从回复后的对话里自动抽取，现在只能查看和纠错。</li>}
        {facts.map(fact => <li key={fact.id} class={`fact-row ${fact.status}`}>
          <div class="message-meta">
            <span class="badge muted">{factKindLabels[fact.kind]}</span>
            <span class="muted-note">置信度 {fact.confidence.toFixed(1)}</span>
            <span class={`badge ${fact.status === 'active' ? 'good' : 'muted'}`}>{factStatusLabels[fact.status]}</span>
            {fact.frozen && <span class="badge warn">已冻结</span>}
            {fact.source_message_id && <span class="muted-note">来源消息 #{fact.source_message_id}</span>}
          </div>
          <p class="message-body">{fact.content}</p>
          <div class="inline-form">
            {fact.status === 'active'
              ? <button class="small-button" onClick={() => patchFact(fact.id, { status: 'quarantined' })}>隔离</button>
              : <button class="small-button" onClick={() => patchFact(fact.id, { status: 'active' })}>恢复</button>}
            <button class="small-button" onClick={() => patchFact(fact.id, { frozen: !fact.frozen })}>
              {fact.frozen ? '解冻' : '冻结'}
            </button>
            <button class="small-button" onClick={() => patchFact(fact.id, { status: 'deleted' })}>删除</button>
          </div>
        </li>)}
      </ul>
    </section>
  </div>;
}

function StickersSection() {
  const [stickers, setStickers] = useState<Sticker[]>([]);
  const [capabilities, setCapabilities] = useState<Capabilities>({});
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const [showDisabled, setShowDisabled] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const load = useCallback(async () => {
    const params = new URLSearchParams();
    if (search) params.set('q', search);
    if (showDisabled) params.set('enabled', '0');
    try {
      const [list, caps] = await Promise.all([
        request<{ stickers: Sticker[] }>(`/api/v1/stickers?${params.toString()}`),
        request<{ platforms: Capabilities }>('/api/v1/capabilities'),
      ]);
      setStickers(list.stickers);
      setCapabilities(caps.platforms);
      setError('');
    } catch (err) { setError((err as Error).message); }
  }, [search, showDisabled]);
  useEffect(() => { load(); }, [load]);

  async function scanDirectory() {
    setBusy(true);
    try {
      const report = await request<{
        imported: number; skipped: number; failed: { file: string; error: string }[];
      }>('/api/v1/stickers', 'POST', { scan_directory: true });
      setNotice(`目录导入：新增 ${report.imported}，跳过 ${report.skipped}` +
        (report.failed.length ? `，失败 ${report.failed.length}` : ''));
      setError(report.failed.length
        ? report.failed.map(item => `${item.file}：${item.error}`).join('；')
        : '');
      await load();
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  }
  async function patch(sticker: Sticker, body: Record<string, unknown>) {
    try {
      await request(`/api/v1/stickers/${sticker.id}`, 'PATCH', body);
      await load();
    } catch (err) { setError((err as Error).message); }
  }
  async function rename(sticker: Sticker) {
    const name = window.prompt('新的表情名称（模型按 ID 引用，改名不会断）', sticker.name);
    if (name === null) return;
    if (name.trim()) patch(sticker, { name: name.trim() });
  }
  async function editTags(sticker: Sticker) {
    const tags = window.prompt('标签，用逗号分隔', sticker.tags.join(','));
    if (tags === null) return;
    patch(sticker, { tags: tags.split(',').map(item => item.trim()).filter(Boolean) });
  }

  return <div class="memory-view">
    <section class="overview">
      <div>
        <span class="eyebrow">表情与能力</span>
        <h3>发不出去的图，不假装发出去了。</h3>
        <p>能发图的平台按 ID 发图；不能或未确认的平台自动降级成文字（比如「（无语）」），并在发送记录里写明原因。</p>
      </div>
      <div class="overview-metrics">
        <div><strong>{stickers.filter(item => item.enabled).length}</strong><span>可用表情</span></div>
        <div><strong>{Object.keys(capabilities).length}</strong><span>已挂平台</span></div>
      </div>
    </section>
    {error && <div class="error-banner" role="alert">{error}</div>}
    {notice && <div class="notice success">{notice}</div>}

    <section class="panel">
      <div class="section-heading"><h3>发送能力</h3><span>unknown 一律按不能发处理</span></div>
      <table class="capability-table">
        <thead><tr><th>平台</th><th>收文本</th><th>发文本</th><th>发图</th><th>评论</th><th>主动私信</th></tr></thead>
        <tbody>
          {Object.entries(capabilities).map(([platform, caps]) => <tr key={platform}>
            <td>{platformNames[platform as keyof typeof platformNames] || platform}</td>
            <td>{capabilityLabels[String(caps.text_receive)] || '—'}</td>
            <td>{capabilityLabels[String(caps.text_send)] || '—'}</td>
            <td>{capabilityLabels[String(caps.image_send)] || '—'}</td>
            <td>{capabilityLabels[String(caps.comment_send)] || '—'}</td>
            <td>{capabilityLabels[String(caps.proactive_private)] || '—'}</td>
          </tr>)}
        </tbody>
      </table>
    </section>

    <section class="panel">
      <div class="section-heading"><h3>表情库</h3><span>文件改名不影响模型引用；引用的是数字 ID</span></div>
      <div class="inbox-toolbar">
        <form class="inline-form" onSubmit={event => { event.preventDefault(); setSearch(query.trim()); }}>
          <input aria-label="搜索表情" placeholder="搜名称或标签" value={query}
            onInput={event => setQuery(event.currentTarget.value)} />
          <button class="small-button" type="submit">搜索</button>
        </form>
        <label class="muted-note">
          <input type="checkbox" checked={showDisabled}
            onChange={event => setShowDisabled(event.currentTarget.checked)} /> 显示已停用
        </label>
        <button class="secondary-button" disabled={busy} onClick={scanDirectory}>扫描 data/stickers 目录</button>
      </div>
      <div class="sticker-grid">
        {stickers.length === 0 && <p class="muted-note">
          还没有表情。把图片放进 data/stickers 后点「扫描」，或在收件箱里对图片点「存为表情」。
        </p>}
        {stickers.map(sticker => <article class={`sticker-card${sticker.enabled ? '' : ' disabled'}`} key={sticker.id}>
          <div class="sticker-badge">{sticker.id}</div>
          <div class="sticker-body">
            <strong>{sticker.name}</strong>
            <span class="muted-note">
              {sticker.mime} · {sticker.w ?? '?'}×{sticker.h ?? '?'} · {Math.round(sticker.bytes / 1024)} KB
            </span>
            <span class="muted-note">{sticker.tags.length ? sticker.tags.join(' · ') : '无标签'} · {sticker.source}</span>
          </div>
          <div class="inline-form">
            <button class="small-button" onClick={() => rename(sticker)}>改名</button>
            <button class="small-button" onClick={() => editTags(sticker)}>标签</button>
            <button class="small-button" onClick={() => patch(sticker, { enabled: !sticker.enabled })}>
              {sticker.enabled ? '停用' : '启用'}
            </button>
          </div>
        </article>)}
      </div>
    </section>
  </div>;
}

function LogsSection() {
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [diagnostics, setDiagnostics] = useState<Diagnostics | null>(null);
  const [level, setLevel] = useState('');
  const [platform, setPlatform] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [autostart, setAutostart] = useState<AutostartStatus | null>(null);
  const [autoBusy, setAutoBusy] = useState(false);

  const load = useCallback(async () => {
    const params = new URLSearchParams({ lines: '200' });
    if (level) params.set('level', level);
    if (platform) params.set('platform', platform);
    try {
      const [logs, diag] = await Promise.all([
        request<{ entries: LogEntry[] }>(`/api/v1/logs?${params.toString()}`),
        request<Diagnostics>('/api/v1/diagnostics'),
      ]);
      setEntries(logs.entries);
      setDiagnostics(diag);
      setError('');
    } catch (err) { setError((err as Error).message); }
  }, [level, platform]);
  useEffect(() => {
    load();
    const timer = window.setInterval(load, 3000);
    return () => window.clearInterval(timer);
  }, [load]);

  async function exportDiagnostics() {
    try {
      const result = await request<{ path: string; bytes: number; excluded: string[] }>(
        '/api/v1/diagnostics/export', 'POST', {});
      setNotice(`诊断包已导出：${result.path}（${Math.round(result.bytes / 1024)} KB，已排除 ${result.excluded.join('、')}）`);
    } catch (err) { setError((err as Error).message); }
  }

  // 开机自启：读的是注册表真实状态，不是配置里那个「愿不愿意」的值
  const loadAutostart = useCallback(async () => {
    try { setAutostart(await window.ilink.getAutostart()); }
    catch (err) { setAutostart(null); setError((err as Error).message); }
  }, []);
  useEffect(() => { loadAutostart(); }, [loadAutostart]);

  async function toggleAutostart(next: boolean) {
    setAutoBusy(true);
    try {
      const result = await window.ilink.setAutostart(next);
      if (!result.applied) {
        throw new Error(result.reason === 'os_rejected'
          ? '系统没接受这次修改（可能被安全软件拦了），注册表里的状态没变'
          : `未生效：${result.reason}`);
      }
      await loadAutostart();
      setNotice(next ? '已登记开机自启' : '已取消开机自启');
      setError('');
    } catch (err) { setError((err as Error).message); }
    finally { setAutoBusy(false); }
  }

  const counters = diagnostics?.metrics.counters ?? {};
  const latency = diagnostics?.metrics.latency_seconds ?? {};
  const tokens = diagnostics?.metrics.tokens;
  return <div class="memory-view">
    <section class="overview">
      <div>
        <span class="eyebrow">运行与诊断</span>
        <h3>排障不用开终端。</h3>
        <p>日志写盘前就脱敏；诊断包只含版本、计数、耗时与脱敏日志，不含正文、附件和凭据。</p>
      </div>
      <div class="overview-metrics">
        <div><strong>{diagnostics?.app.version ?? '—'}</strong><span>应用版本</span></div>
        <div><strong>v{diagnostics?.database.schema ?? '—'}</strong><span>schema</span></div>
        <div><strong>{diagnostics?.database.queue?.inbound ?? 0}</strong><span>待处理入站</span></div>
      </div>
    </section>
    {error && <div class="error-banner" role="alert">{error}</div>}
    {notice && <div class="notice success">{notice}</div>}

    <section class="panel">
      <div class="section-heading"><h3>运行指标</h3><span>token 为保守估算，不假装精确</span></div>
      <div class="diag-grid">
        <div><span>入站 P95</span><strong>{latency.inbound?.p95 ?? 0}s</strong></div>
        <div><span>发送 P95</span><strong>{latency.send?.p95 ?? 0}s</strong></div>
        <div><span>今日 token</span><strong>{tokens?.total ?? 0}</strong></div>
        <div><span>今日费用</span><strong>{diagnostics?.metrics.cost_usd == null ? '未配置单价' : `$${diagnostics?.metrics.cost_usd}`}</strong></div>
        <div><span>降级</span><strong>{counters.degraded ?? 0}</strong></div>
        <div><span>发送结果不明</span><strong>{counters.unknown ?? 0}</strong></div>
        <div><span>预算拦截</span><strong>{counters.budget_blocked ?? 0}</strong></div>
        <div><span>数据库错误</span><strong>{counters.db_errors ?? 0}</strong></div>
      </div>
      <div class="inline-form">
        <button class="secondary-button" onClick={exportDiagnostics}>导出诊断包</button>
        <button class="secondary-button" onClick={() => window.ilink?.openLogFolder()}>打开日志目录</button>
      </div>
    </section>

    <section class="panel">
      <div class="section-heading"><h3>本机启动</h3><span>只写当前用户的登录启动项，不需要管理员</span></div>
      {autostart === null
        ? <p class="muted-note">读不到启动项状态；请从桌面窗口打开（普通浏览器没有本机操作权限）。</p>
        : <>
          <label class="check-line">
            <input type="checkbox" checked={autostart.enabled} disabled={autoBusy}
              onChange={event => toggleAutostart(event.currentTarget.checked)} />
            开机时自动启动 iLink Studio（当前：{autostart.enabled ? '已开启' : '已关闭'}）
          </label>
          <p class="muted-note">
            {autostart.mode === 'dev'
              ? '源码模式：登录时拉起的是 electron 加项目目录，项目挪了位置要重新登记一次。'
              : '打包模式：登录时拉起的就是这个应用本身。'}
          </p>
          <p class="muted-note">登记的命令：{autostart.path} {autostart.args.join(' ')}</p>
        </>}
    </section>

    <section class="panel">
      <div class="section-heading"><h3>日志</h3><span>每 3 秒尾随一次；敏感字段已脱敏</span></div>
      <div class="filter-group">
        {[['', '全部级别'], ['INFO', 'INFO'], ['WARNING', 'WARNING'], ['ERROR', 'ERROR'], ['DEBUG', 'DEBUG']].map(([value, label]) =>
          <button key={value || 'all'} class={`filter-chip${level === value ? ' selected' : ''}`}
            onClick={() => setLevel(value)}>{label}</button>)}
      </div>
      <div class="filter-group">
        {[['', '全部平台'], ['qq', 'QQ'], ['wechat', '微信'], ['bilibili', 'B站'], ['douyin', '抖音']].map(([value, label]) =>
          <button key={value || 'any'} class={`filter-chip${platform === value ? ' selected' : ''}`}
            onClick={() => setPlatform(value)}>{label}</button>)}
      </div>
      <div class="log-list">
        {entries.length === 0 && <p class="muted-note">没有匹配的日志行。</p>}
        {entries.map((entry, index) => <div class={`log-line ${entry.level.toLowerCase()}`} key={`${entry.ts}-${index}`}>
          <span class="log-ts">{entry.ts}</span>
          <span class="log-level">{entry.level}</span>
          <span class="log-logger">{entry.logger}</span>
          <span class="log-message">{entry.message}</span>
        </div>)}
      </div>
    </section>
  </div>;
}

function RadarItem({ item, onShare }: { item: RadarCandidate; onShare: (c: RadarCandidate) => void }) {
  const stats = item.stats || {};
  const seconds = item.duration_sec ? `${Math.round(item.duration_sec)} 秒` : '时长未知';
  return <article class="candidate-item">
    <div class="section-heading">
      <h3>{item.title || '（没有文案）'}</h3>
      <span>{item.author || '未知作者'} · {seconds}</span>
    </div>
    <p>{item.reason || item.error || '没有给出理由'}</p>
    <div class="filter-group">
      <span class="badge muted">赞 {stats.like ?? 0}</span>
      <span class="badge muted">评 {stats.comment ?? 0}</span>
      <span class="badge muted">藏 {stats.collect ?? 0}</span>
      <span class="badge muted">转 {stats.share ?? 0}</span>
      {item.tags.map(tag => <span key={tag} class="badge warn">{tag}</span>)}
    </div>
    {item.comments.length > 0 && <div class="fact-list">
      {item.comments.map((comment, index) => <div key={index} class="fact-row">
        赞 {comment.likes}　{comment.text}
      </div>)}
    </div>}
    <div class="top-actions">
      <button class="secondary-button" onClick={() => onShare(item)}>分享给…</button>
      <span class="hint">{item.url}</span>
    </div>
  </article>;
}

function RadarSection() {
  // 一个区两个平台：抖音靠观众反应判断，B 站能读字幕原文。默认抖音（历史行为）
  const [platform, setPlatform] = useState<RadarPlatform>('douyin');
  const base = `/api/v1/radar/${platform}`;
  const [status, setStatus] = useState<RadarStatus | null>(null);
  const [candidates, setCandidates] = useState<RadarCandidates | null>(null);
  const [report, setReport] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [sharing, setSharing] = useState<RadarCandidate | null>(null);
  const [shareTo, setShareTo] = useState('');
  const [shareReason, setShareReason] = useState('');
  const [preview, setPreview] = useState<RadarShare | null>(null);
  const [doctor, setDoctor] = useState<RadarDoctor | null>(null);
  const [config, setConfig] = useState<RadarSettings | null>(null);
  const [draftTop, setDraftTop] = useState('8');
  const [draftNotify, setDraftNotify] = useState('file');
  const [draftWebhook, setDraftWebhook] = useState('');
  const [savedHint, setSavedHint] = useState('');
  // B 站（刷B站）专属：四页签设置 + 「它刷过的视频」
  const [settingsView, setSettingsView] = useState<RadarSettingsView | null>(null);
  const [activeTab, setActiveTab] = useState('daily');
  const [draftValues, setDraftValues] = useState<Record<string, unknown>>({});
  const [videos, setVideos] = useState<RadarVideos | null>(null);
  const [videoGrade, setVideoGrade] = useState('');
  const [videoStatus, setVideoStatus] = useState('');
  const [videoPage, setVideoPage] = useState(1);

  const load = useCallback(async () => {
    try {
      const [current, snapshot, doc, check, cfg] = await Promise.all([
        request<RadarStatus>(`${base}/status`),
        request<RadarCandidates>(`${base}/candidates`),
        request<RadarReport>(`${base}/report`),
        request<RadarDoctor>(`${base}/doctor`),
        request<Record<string, unknown>>(`${base}/settings`),
      ]);
      setStatus(current); setCandidates(snapshot); setReport(doc.markdown); setError('');
      setDoctor(check);
      if (platform === 'bilibili') {
        // B 站是四页签那套：values/tabs 一起回来，草稿直接按 values 铺开
        const view = cfg as unknown as RadarSettingsView;
        setSettingsView(view); setConfig(null);
        setDraftValues({ ...(view.values || {}) });
      } else {
        const plain = cfg as unknown as RadarSettings;
        setConfig(plain); setSettingsView(null);
        setDraftTop(String(plain.top)); setDraftNotify(plain.notify);
      }
    } catch (err) { setError((err as Error).message); }
  }, [base, platform]);
  useEffect(() => { load(); }, [load]);

  const loadVideos = useCallback(async () => {
    if (platform !== 'bilibili') return;
    try {
      const query = new URLSearchParams({ page: String(videoPage), size: '20' });
      if (videoGrade) query.set('grade', videoGrade);
      if (videoStatus) query.set('status', videoStatus);
      setVideos(await request<RadarVideos>(`${base}/videos?${query.toString()}`));
    } catch (err) { setError((err as Error).message); }
  }, [base, platform, videoGrade, videoStatus, videoPage]);
  useEffect(() => { loadVideos(); }, [loadVideos]);

  const saveBiliSettings = async () => {
    setError(''); setSavedHint('');
    try {
      const body: Record<string, unknown> = {};
      for (const field of Object.values(settingsView?.tabs || {}).flat()) {
        if (field.key in draftValues) body[field.key] = draftValues[field.key];
      }
      const view = await request<RadarSettingsView>(`${base}/settings`, 'POST', body);
      setSettingsView(view); setDraftValues({ ...(view.values || {}) });
      setSavedHint('已保存');
      await loadVideos();
    } catch (err) { setError((err as Error).message); }
  };

  const setDraft = (key: string, value: unknown) =>
    setDraftValues(current => ({ ...current, [key]: value }));

  const runRound = async () => {
    setBusy(true); setError(''); setNotice('');
    try {
      const out = await request<RadarRound>(`${base}/run`, 'POST', { top: 8 });
      setCandidates(out.candidates);
      setNotice(`看到 ${out.summary.seen} 条，推 ${out.summary.picked} 条，判断不了 ${out.summary.uncertain} 条`);
      await load();
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  };

  const openShare = (item: RadarCandidate) => {
    setSharing(item); setShareTo(''); setShareReason(''); setPreview(null); setError('');
  };

  const askPreview = async () => {
    if (!sharing) return;
    try {
      setPreview(await request<RadarShare>(`${base}/share`, 'POST', {
        item_id: sharing.item_id, to: shareTo.trim(), reason: shareReason.trim(),
      }));
    } catch (err) { setError((err as Error).message); }
  };

  const confirmShare = async () => {
    if (!sharing) return;
    try {
      const out = await request<RadarShare>(`${base}/share`, 'POST', {
        item_id: sharing.item_id, to: shareTo.trim(),
        reason: shareReason.trim(), confirm: true,
      });
      setPreview(out);
      setNotice(out.sent ? `已发给 ${out.to}` : `没发出去：${out.detail || '未知原因'}`);
    } catch (err) { setError((err as Error).message); }
  };

  const saveConfig = async () => {
    setError(''); setSavedHint('');
    try {
      const body: Record<string, unknown> = { top: Number(draftTop) || 8, notify: draftNotify };
      if (draftWebhook.trim()) body.webhook_url = draftWebhook.trim();
      const out = await request<RadarSettings>(`${base}/settings`, 'POST', body);
      setConfig(out); setDraftWebhook(''); setSavedHint('已保存');
    } catch (err) { setError((err as Error).message); }
  };

  const quota = status?.quota;
  const ready = Boolean(status?.ready);
  const platformLabel = platform === 'bilibili' ? 'B站' : '抖音';

  return <>
    <section class="overview">
      <div><span class="eyebrow">内容雷达</span>
        <h3>{platform === 'bilibili'
          ? '刷一轮B站，读字幕挑出值得看的那几条。'
          : '刷一轮抖音，挑出值得点开的那几条。'}</h3>
        <p>{platform === 'bilibili'
          ? 'B站有 CC 字幕：它逐条读字幕原文再判断，没字幕的演示类先抽故事板帧看画面。写「值得看」必须读过字幕或看过画面，否则最多「可以看」。'
          : '判断只依据文案、时长、互动四件套和高赞评论——它看不到画面，也没有字幕可读。证据不够就写「判断不了」，不硬凑。'}</p>
      </div>
      <div class="overview-metrics">
        <div><strong>{quota ? quota.left : '—'}</strong><span>今日额度剩余</span></div>
        <div><strong>{status?.last?.picked ?? '—'}</strong><span>上一轮推荐</span></div>
        <div><strong>{status?.last?.seen ?? '—'}</strong><span>上一轮看到</span></div>
      </div>
    </section>

    <section class="panel">
      <div class="section-heading"><h3>平台</h3><span>两个平台各有一套通道与家目录，互不干扰</span></div>
      <div class="filter-group">
        <button class={platform === 'douyin' ? 'primary-button' : 'secondary-button'}
          onClick={() => setPlatform('douyin')}>抖音 · 读观众反应</button>
        <button class={platform === 'bilibili' ? 'primary-button' : 'secondary-button'}
          onClick={() => setPlatform('bilibili')}>B站 · 读字幕原文</button>
      </div>
    </section>

    <section class="panel">
      <div class="section-heading">
        <h3>{ready ? `${platformLabel}已就绪：${status?.identity.nick || status?.identity.uid || '未知账号'}` : `${platformLabel}未就绪`}</h3>
        <span>{ready ? `日报 ${status?.report_day}.md` : (status?.identity.error || '正在读登录状态…')}</span>
      </div>
      <div class="top-actions">
        <button class="primary-button" onClick={runRound} disabled={busy || !ready}>
          {busy ? '正在刷…' : '刷一轮'}
        </button>
        <button class="secondary-button" onClick={() => load()}>重新读取</button>
        {busy && <span class="hint">一轮可能要一两分钟：拉推荐流、取评论、逐条判断</span>}
      </div>
      {notice && <p class="hint">{notice}</p>}
      {status?.node && !status.node.ok &&
        <div class="error-banner" role="alert">
          {status.node.error}（当前 Node：{status.node.version || '未知'}，路径 {status.node.path}）—— 发布版不自带 Node，装好再试。
        </div>}
      {error && <div class="error-banner" role="alert">{error}</div>}
    </section>

    {platform === 'douyin' && sharing && <section class="panel">
      <div class="section-heading"><h3>分享「{sharing.title.slice(0, 24)}」</h3><span>先预览，再确认——确认前不会发出</span></div>
      <div class="filter-group">
        <label>发给谁（昵称或 sec_uid）
          <input value={shareTo} onInput={event => setShareTo((event.target as HTMLInputElement).value)} />
        </label>
        <label>一句理由
          <input value={shareReason} onInput={event => setShareReason((event.target as HTMLInputElement).value)} />
        </label>
      </div>
      <div class="top-actions">
        <button class="secondary-button" onClick={askPreview} disabled={!shareTo.trim()}>预览</button>
        <button class="primary-button" onClick={confirmShare} disabled={!preview || preview.sent}>确认发送</button>
        <button class="secondary-button" onClick={() => { setSharing(null); setPreview(null); }}>取消</button>
      </div>
      {preview && <div class="block-note">
        <strong>{preview.sent ? `已发出，收件人 ${preview.to}` : '还没发出去'}</strong>
        <p>{preview.preview || preview.detail || '（没有预览内容）'}</p>
      </div>}
    </section>}

    {candidates && <section class="panel">
      <div class="section-heading"><h3>本轮候选</h3>
        <span>{candidates.picked.length} 条推荐 · {candidates.uncertain.length} 条判断不了</span></div>
      {!candidates.picked.length && !candidates.uncertain.length &&
        <div class="empty-state"><h3>还没有候选</h3><p>{candidates.note || '点上面的「刷一轮」开始。'}</p></div>}
      {candidates.picked.map(item => <RadarItem key={item.item_id} item={item} onShare={openShare} />)}
      {candidates.uncertain.length > 0 && <>
        <div class="section-heading"><h3>判断不了</h3><span>证据不足，不硬推</span></div>
        {candidates.uncertain.map(item => <RadarItem key={item.item_id} item={item} onShare={openShare} />)}
      </>}
    </section>}

    <section class="panel">
      <div class="section-heading"><h3>环境体检</h3>
        <span>{doctor?.ok ? '没有硬伤' : (doctor?.error || '读取中…')}</span></div>
      {doctor?.items?.length
        ? <div class="diag-grid">
          {doctor.items.map(row => <div key={row.item} class="check-line">
            <span class={`badge ${row.state === 'ok' ? 'good' : row.state === 'warn' ? 'warn' : 'bad'}`}>{row.item}</span>
            <span>{row.detail || '—'}</span>
            {row.fix && <span class="hint">怎么修：{row.fix}</span>}
          </div>)}
        </div>
        : <div class="empty-state"><h3>还没有体检结果</h3>
          <p>{doctor?.error || '点「重新读取」试一次。缺依赖时按 doctor 给出的命令自己装 —— 这里不替你自动装。'}</p></div>}
    </section>

    {platform === 'douyin' && <section class="panel">
      <div class="section-heading"><h3>刷抖音设置</h3>
        <span>每天名额 {config?.per_day ?? '—'} 条</span></div>
      <div class="filter-group">
        <label>每次最多挑几条
          <input value={draftTop} onInput={event => setDraftTop((event.target as HTMLInputElement).value)} />
        </label>
        <label>通知方式
          <select value={draftNotify} onChange={event => setDraftNotify((event.target as HTMLSelectElement).value)}>
            {(config?.channels || ['file']).map(channel =>
              <option key={channel} value={channel}>{notifyLabels[channel] || channel}</option>)}
          </select>
        </label>
        <label>Webhook 地址（留空不改）
          <input value={draftWebhook}
            placeholder={config?.has_webhook ? '已配置（原值不回显）' : 'https://…'}
            onInput={event => setDraftWebhook((event.target as HTMLInputElement).value)} />
        </label>
      </div>
      <div class="top-actions">
        <button class="primary-button" onClick={saveConfig}>保存设置</button>
        {savedHint && <span class="hint">{savedHint}</span>}
      </div>
    </section>}

    {platform === 'bilibili' && <>
      <section class="panel">
        <div class="section-heading"><h3>它刷过的视频</h3>
          <span>{videos ? `共 ${videos.total} 条 · 第 ${videos.page}/${videos.pages || 1} 页` : '读取中…'}</span></div>
        <div class="filter-group">
          <label>档位
            <select value={videoGrade} onChange={event => { setVideoGrade((event.target as HTMLSelectElement).value); setVideoPage(1); }}>
              <option value="">全部</option>
              {(videos?.grades || ['值得看', '可以看', '不值当']).map(grade =>
                <option key={grade} value={grade}>{grade}</option>)}
            </select>
          </label>
          <label>状态
            <select value={videoStatus} onChange={event => { setVideoStatus((event.target as HTMLSelectElement).value); setVideoPage(1); }}>
              <option value="">全部</option>
              {(videos?.statuses || ['挑了', '跳过', '评论']).map(item =>
                <option key={item} value={item}>{item}</option>)}
            </select>
          </label>
        </div>
        {!videos?.items?.length
          ? <div class="empty-state"><h3>还没有刷出东西</h3>
            <p>{videos?.note || '点上面的「刷一轮」开始。'}</p></div>
          : <div class="candidate-list">
            {videos.items.map(item => <article key={`${item.day}-${item.bvid}`} class="candidate-item">
              <div class="section-heading">
                <h3>{item.title || item.bvid}</h3>
                <span>{item.up || '未知UP'} · {item.day}{item.status ? ` · ${item.status}` : ''}</span>
              </div>
              <p>{item.reason || '（没有给出理由）'}</p>
              {item.one_line && <p class="hint">「{item.one_line}」</p>}
              <div class="filter-group">
                <span class={`badge ${item.grade === '值得看' ? 'good' : item.grade === '不值当' ? 'bad' : 'warn'}`}>{item.grade || '未定档'}</span>
                {item.play && <span class="badge muted">{item.play}</span>}
                {item.url && <a class="badge muted" href={item.url} target="_blank" rel="noreferrer">{item.bvid}</a>}
              </div>
            </article>)}
          </div>}
        <div class="top-actions">
          <button class="secondary-button" disabled={videoPage <= 1}
            onClick={() => setVideoPage(page => Math.max(1, page - 1))}>上一页</button>
          <button class="secondary-button" disabled={!videos || videoPage >= (videos.pages || 1)}
            onClick={() => setVideoPage(page => page + 1)}>下一页</button>
          <button class="secondary-button" onClick={loadVideos}>刷新列表</button>
        </div>
      </section>

      <section class="panel">
        <div class="section-heading"><h3>刷B站设置</h3>
          <span>{settingsView?.override_file ? `改动只写覆盖层：${settingsView.override_file}` : '读取中…'}</span></div>
        <div class="filter-group">
          {Object.keys(settingsView?.tabs || {}).map(name =>
            <button key={name} class={activeTab === name ? 'primary-button' : 'secondary-button'}
              onClick={() => setActiveTab(name)}>{settingsView?.tab_labels?.[name] || name}</button>)}
        </div>
        <div class="filter-group">
          {(settingsView?.tabs?.[activeTab] || []).map(field => <label key={field.key}>{field.label}
            {field.kind === 'bool'
              ? <input type="checkbox" checked={Boolean(draftValues[field.key])}
                onChange={event => setDraft(field.key, (event.target as HTMLInputElement).checked)} />
              : field.options
                ? <select value={String(draftValues[field.key] ?? '')}
                  onChange={event => setDraft(field.key, (event.target as HTMLSelectElement).value)}>
                  {field.options.map(option => <option key={option} value={option}>{option}</option>)}
                </select>
                : <input value={Array.isArray(draftValues[field.key])
                    ? (draftValues[field.key] as string[]).join(', ')
                    : String(draftValues[field.key] ?? '')}
                  onInput={event => setDraft(field.key, (event.target as HTMLInputElement).value)} />}
          </label>)}
        </div>
        <div class="top-actions">
          <button class="primary-button" onClick={saveBiliSettings}>保存设置</button>
          {savedHint && <span class="hint">{savedHint}</span>}
        </div>
      </section>
    </>}

    <section class="panel">
      <div class="section-heading"><h3>今日日报</h3><span>{status?.report || ''}</span></div>
      {report
        ? <pre class="log-list">{report}</pre>
        : <div class="empty-state"><h3>今天还没有日报</h3><p>刷一轮之后会写到 data/reports 里。</p></div>}
    </section>
  </>;
}

function PendingSection({ section }: { section: Section }) {
  const labels: Record<string, [string, string]> = {};
  const [title, description] = labels[section] ?? ['尚未接入', '这个区域会在后续里程碑补齐。'];
  return <div class="empty-state"><div class="empty-icon">{section === 'logs' ? '≡' : '·'}</div><h3>{title}</h3><p>{description}</p>{section === 'logs' && <button class="secondary-button" onClick={() => window.ilink?.openLogFolder()}>打开日志目录</button>}</div>;
}

function OnboardingDialog({ bootstrap, accounts, onDone }: { bootstrap: Bootstrap; accounts: Account[]; onDone: () => void }) {
  const [risks, setRisks] = useState(false);
  const [data, setData] = useState(false);
  const [model, setModel] = useState(false);
  const [autostart, setAutostart] = useState(bootstrap.onboarding.autostart);
  const [command, setCommand] = useState(bootstrap.ai.command || '');
  const [modelName, setModelName] = useState(bootstrap.ai.model && bootstrap.ai.model !== '默认模型' ? bootstrap.ai.model : '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const backendReady = bootstrap.ai.ready || !!command.trim();
  const boundCount = accounts.filter(account => account.auth_state === 'bound').length;

  async function pickBackend() {
    try {
      const picked = await window.ilink?.chooseBackend();
      if (picked) { setCommand(picked); setNotice(''); setError(''); }
    } catch (err) { setError((err as Error).message); }
  }

  async function confirm() {
    setBusy(true); setError(''); setNotice('');
    try {
      if (autostart) {
        const result = await window.ilink?.setAutostart(true);
        if (result && !result.applied && result.reason !== 'dev') {
          setError('系统拒绝了开机自启设置，请取消勾选后继续');
          return;
        }
      }
      await request('/api/v1/onboarding', 'POST', {
        accept_risks: risks, accept_data: data, accept_model: model,
        autostart, command: command.trim(), model: modelName.trim(),
      });
      onDone();
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  }

  const ready = risks && data && model && backendReady;
  return <div class="modal-backdrop">
    <div class="modal-card" role="dialog" aria-modal="true" aria-label="首启确认">
      <h2>开始之前，先确认这几件事</h2>
      <ul class="risk-list">{bootstrap.onboarding.risks.map(item => <li key={item}>{item}</li>)}</ul>
      <dl class="onboard-facts">
        <div><dt>数据位置</dt><dd>{bootstrap.onboarding.data_dir || '本机数据目录'}</dd></div>
        <div><dt>通道</dt><dd>{boundCount} / {accounts.length} 个通道已绑定；未绑定的不会被启用</dd></div>
      </dl>
      <div class="backend-block">
        <span class="label">Reasonix 可执行文件</span>
        <div class="inline-form">
          <input aria-label="Reasonix 可执行文件路径" placeholder="选择本机 Reasonix 的 .exe / .cmd" value={command} onInput={event => setCommand(event.currentTarget.value)} />
          <button class="secondary-button" onClick={pickBackend} type="button">浏览…</button>
        </div>
        <div class="inline-form">
          <input aria-label="模型名（可选）" placeholder="模型名（留空用 Reasonix 默认模型）" value={modelName} onInput={event => setModelName(event.currentTarget.value)} />
        </div>
        <span class="muted-note">
          {bootstrap.ai.ready
            ? <>已配置可执行后端，可只改模型名。</>
            : <>还没有可用的模型后端；不选可执行文件，AI 回复保持关闭。</>}
        </span>
      </div>
      <label class="check-line">
        <input type="checkbox" checked={risks} onChange={event => setRisks(event.currentTarget.checked)} />
        我知道这些平台是非官方路径，接受封号风险
      </label>
      <label class="check-line">
        <input type="checkbox" checked={data} onChange={event => setData(event.currentTarget.checked)} />
        我知道聊天正文以明文 SQLite 保存在本机
      </label>
      <label class="check-line">
        <input type="checkbox" checked={model} onChange={event => setModel(event.currentTarget.checked)} />
        我知道正文会发送给上面这个模型服务
      </label>
      <label class="check-line">
        <input type="checkbox" checked={autostart} onChange={event => setAutostart(event.currentTarget.checked)} />
        开机自启（可选，写入本机登录启动项）
      </label>
      {notice && <div class="notice">{notice}</div>}
      {error && <div class="error-banner" role="alert">{error}</div>}
      <div class="inline-form">
        <button class="primary-button" disabled={!ready || busy} onClick={confirm}>确认并继续</button>
        <span class="muted-note">{backendReady ? '确认后仍需在「账号」页登录并单独启用通道' : '先选好 Reasonix 可执行文件才能继续'}</span>
      </div>
    </div>
  </div>;
}

function AllowlistPanel({ onError }: { onError: (message: string) => void }) {
  const [entries, setEntries] = useState<Record<string, string[]>>({});
  const [candidates, setCandidates] = useState<AllowlistCandidate[]>([]);
  const [platform, setPlatform] = useState<'qq' | 'wechat' | 'bilibili' | 'douyin'>('wechat');
  const [uid, setUid] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');

  const load = useCallback(async () => {
    try {
      const result = await request<AllowlistEntries>('/api/v1/allowlist');
      setEntries(result.entries);
      setCandidates(result.candidates || []);
    } catch (error) { onError((error as Error).message); }
  }, [onError]);
  useEffect(() => { load(); }, [load]);

  async function change(nextPlatform: string, nextUid: string, allow: boolean) {
    setBusy(true); setNotice('');
    try {
      const result = await request<AllowlistEntries>(
        '/api/v1/allowlist', allow ? 'POST' : 'DELETE',
        { platform: nextPlatform, platform_uid: nextUid },
      );
      setEntries(result.entries);
      setUid('');
      setNotice(allow
        ? `已允许 ${nextUid}：下一条消息就会进入生成流程`
        : `已移出 ${nextUid}：以后不会自动回复此人`);
      await load();
    } catch (error) { onError((error as Error).message); }
    finally { setBusy(false); }
  }

  const pending = candidates.filter(item =>
    !(entries[item.platform] || []).some(value => value === item.platform_uid));
  const total = Object.values(entries).reduce((sum, list) => sum + list.length, 0);
  return <section class="allowlist-panel">
    <div class="section-heading">
      <h3>自动回复白名单</h3>
      <span>默认拒绝：只有列在这里的人会被自动回复（当前 {total} 个）</span>
    </div>
    <form class="inline-form" onSubmit={event => { event.preventDefault(); if (uid.trim()) change(platform, uid.trim(), true); }}>
      <select aria-label="平台" value={platform} onChange={event => setPlatform(event.currentTarget.value as typeof platform)}>
        {(['qq', 'wechat', 'bilibili', 'douyin'] as const).map(name =>
          <option key={name} value={name}>{platformNames[name]}</option>)}
      </select>
      <input aria-label="允许的稳定 ID" placeholder="对方稳定 ID（B站数字 UID / 微信 im.wechat 串 / NapCat QQ 号 / 抖音会话标题）" value={uid} onInput={event => setUid(event.currentTarget.value)} />
      <button class="small-button" type="submit" disabled={busy || !uid.trim()}>加入白名单</button>
    </form>
    {notice && <div class="notice success">{notice}</div>}
    {pending.length > 0 && <div class="allow-group candidates">
      <span class="label">最近来过</span>
      {pending.map(item => <button
        class="allow-chip candidate"
        key={`${item.platform}:${item.platform_uid}`}
        disabled={busy}
        title={item.id_kind === 'provisional' ? '只有昵称，不能自动回复' : `最近 ${item.hits} 条`}
        onClick={() => change(item.platform, item.platform_uid, true)}
      >{platformNames[item.platform as keyof typeof platformNames] || item.platform} · {item.name || item.platform_uid}（{item.hits}）</button>)}
    </div>}
    {total === 0
      ? <p class="muted-note">白名单为空时所有消息都只入库不回复。也可以先点上面「最近来过」里的名字，或在「收件箱」里对被拒的消息点「允许此人」。</p>
      : <div class="allow-groups">
        {(['qq', 'wechat', 'bilibili', 'douyin'] as const).map(name => {
          const list = entries[name] || [];
          if (list.length === 0) return null;
          return <div class="allow-group" key={name}>
            <span class="label">{platformNames[name]}</span>
            {list.map(value => <span class="allow-chip" key={value}>
              {value}
              <button aria-label={`移除 ${value}`} disabled={busy} onClick={() => change(name, value, false)}>×</button>
            </span>)}
          </div>;
        })}
      </div>}
  </section>;
}

function App() {
  const [section, setSection] = useState<Section>('accounts');
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [napcat, setNapcat] = useState<NapcatState | null>(null);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      const result = await request<{ accounts: Account[] }>('/api/v1/accounts');
      setAccounts(result.accounts); setError('');
    } catch (err) { setError((err as Error).message); }
    finally { setLoading(false); setRefreshing(false); }
  }, []);
  useEffect(() => { refresh(); }, [refresh]);
  const refreshNapcat = useCallback(async () => {
    try { setNapcat(await request<NapcatState>('/api/v1/napcat')); }
    catch { setNapcat(null); }
  }, []);
  useEffect(() => { refreshNapcat(); }, [refreshNapcat]);
  const refreshBootstrap = useCallback(async () => {
    try { setBootstrap(await request<Bootstrap>('/api/v1/bootstrap')); }
    catch { setBootstrap(null); }
  }, []);
  useEffect(() => { refreshBootstrap(); }, [refreshBootstrap]);
  const activeCount = accounts.filter(a => a.connection_state === 'online').length;
  const boundCount = accounts.filter(a => a.auth_state === 'bound').length;
  return <>
    {bootstrap && !bootstrap.onboarding.confirmed &&
      <OnboardingDialog bootstrap={bootstrap} accounts={accounts} onDone={refreshBootstrap} />}
    <div class="app-shell">
    <aside class="sidebar">
      <div class="brand"><div class="brand-mark">iL</div><div><h1>iLink Studio</h1><span>多平台聊天集成台</span></div></div>
      <div class="workspace-label">控制台</div>
      <nav aria-label="主导航">{sections.map(([key, label, sub]) => <button class={`nav-item ${section === key ? 'selected' : ''}`} onClick={() => setSection(key)}><span class="nav-symbol">{key === 'accounts' ? '◎' : key === 'inbox' ? '▤' : key === 'conversations' ? '◫' : key === 'radar' ? '✦' : key === 'memory' ? '◇' : key === 'stickers' ? '☺' : '≡'}</span><span>{label}<small>{sub}</small></span>{key !== 'accounts' && <span class="nav-stage">{key === 'logs' ? '本机' : key === 'radar' ? '雷达' : key === 'inbox' || key === 'conversations' ? 'M1–2' : key === 'memory' ? 'M2–5' : 'M3'}</span>}</button>)}</nav>
      <div class="sidebar-bottom"><div class="local-note"><span class="lock">⌑</span><div><strong>仅本机访问</strong><span>不开放公网 · 不使用他人身份</span></div></div><div class="version">v0.1.0-rc1 <span>初始候选版</span></div></div>
    </aside>
    <main class="main-content">
      <header class="topbar"><div><h2>{sections.find(([key]) => key === section)?.[1]}</h2><p>{section === 'accounts' ? '一个窗口管理各平台的登录、连接与发送状态' : sections.find(([key]) => key === section)?.[2]}</p></div><div class="top-actions"><span class="runtime-status"><i />网关运行中</span><button class="icon-button" title="刷新账号状态" aria-label="刷新账号状态" onClick={refresh} disabled={refreshing}>↻</button></div></header>
      <div class="content-body">
        {section === 'inbox' ? <InboxSection />
          : section === 'conversations' ? <ConversationsSection />
          : section === 'radar' ? <RadarSection />
          : section === 'memory' ? <MemorySection />
          : section === 'stickers' ? <StickersSection />
          : section === 'logs' ? <LogsSection />
          : section !== 'accounts' ? <PendingSection section={section} /> : loading ? <div class="loading">正在读取本机账号状态…</div> : <>
          <section class="overview"><div><span class="eyebrow">本地工作台</span><h3>让每条连接，都有清楚的状态。</h3><p>凭据、连接和发送能力分开看。不把端口开放当作在线，也不把扫码成功当作可以发送。</p></div><div class="overview-metrics"><div><strong>{accounts.length}</strong><span>账号槽位</span></div><div><strong>{boundCount}</strong><span>已绑定</span></div><div><strong>{activeCount}</strong><span>连接正常</span></div></div></section>
          <div class="security-strip"><span class="shield">✓</span><span>
          {bootstrap?.onboarding.confirmed
            ? <><strong>首启已确认</strong>　自动收发按配置启用；平台通道仍以各自登录状态为准。</>
            : <><strong>自动收发尚未启用</strong>　请先完成首启确认与机器人 UID 核对。</>}
          　本机微信 4.x 不支持 wxauto。</span><span class="security-tag">默认拒绝</span></div>
          {error && <div class="error-banner" role="alert">{error}<button onClick={refresh}>重试</button></div>}
          <div class="section-heading"><h3>平台与通道</h3><span>微信双通道凭据独立保存</span></div>
          <div class="account-grid">{accounts.map(account => <AccountCard key={`${account.platform}:${account.channel}`} account={account} napcat={account.channel === 'onebot' ? napcat : null} onRefresh={() => { refresh(); refreshNapcat(); }} onError={setError} />)}</div>
          <AllowlistPanel onError={setError} />
          <footer class="content-footer"><span>QQ · NapCat　微信 · iLink　B站 · Cookie　抖音 · RPA</span><span>外部通道的稳定性与账号安全不作保证</span></footer>
        </>}
      </div>
    </main>
    </div>
  </>;
}

render(<App />, document.getElementById('app')!);
