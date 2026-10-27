const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const guard = require('../url-guard.cjs');

const main = fs.readFileSync(path.join(__dirname, '..', 'main.cjs'), 'utf8');
const preload = fs.readFileSync(path.join(__dirname, '..', 'preload.cjs'), 'utf8');

test('main window enables all required Electron isolation defaults', () => {
  for (const setting of ['contextIsolation: true', 'sandbox: true', 'nodeIntegration: false', 'webSecurity: true']) {
    assert.ok(main.includes(setting), setting);
  }
  assert.ok(main.includes("setWindowOpenHandler(() => ({ action: 'deny' }))"));
  assert.ok(main.includes("will-navigate"));
  assert.ok(main.includes("windowsHide: true"));
});

test('preload never exposes Node, shell, filesystem or a bearer token', () => {
  assert.ok(preload.includes('contextBridge.exposeInMainWorld'));
  assert.ok(!preload.includes('Authorization'));
  assert.ok(!preload.includes('ipcRenderer.send('));
  assert.ok(!preload.includes('exec('));
  assert.ok(!preload.includes('readFile'));
});

test('UI quit is explicit and window close is separate', () => {
  assert.ok(main.includes('if (!quitting)'));
  assert.ok(main.includes('stopOwnedGateway'));
});

test('every preload bridge has a matching main-process handler', () => {
  const channels = [...preload.matchAll(/ipcRenderer\.invoke\('([^']+)'/g)].map(match => match[1]);
  assert.ok(channels.length >= 6, `preload channels: ${channels.join(', ')}`);
  for (const channel of channels) {
    assert.ok(main.includes(`secureIpc('${channel}'`),
      `main.cjs 缺少受控 handler：${channel}`);
  }
});

test('backend picker and autostart stay behind the trusted-origin IPC guard', () => {
  assert.ok(main.includes("secureIpc('dialog:choose-backend'"));
  assert.ok(main.includes("secureIpc('app:autostart'"));
  // 开机自启必须真的改登录启动项，而不是只写配置文件
  assert.ok(main.includes('setLoginItemSettings'));
  assert.ok(main.includes('getLoginItemSettings'));
});

// 真机问题：以前源码模式直接返回 reason:'dev'，界面上那个开关点了没反应，
// 首启向导关掉之后也没有第二个入口能关。现在两件事都要成立。
test('autostart can be read and turned off, source mode included', () => {
  assert.ok(main.includes("secureIpc('app:autostart-status'"), '界面要读得到真实状态才谈得上关');
  assert.ok(main.includes('function currentAutostart'));
  assert.ok(preload.includes('getAutostart'));
  assert.ok(preload.includes("app:autostart-status"));
  assert.ok(!main.includes("reason: 'dev'"), '源码模式不再当作「不支持」');
  // 源码模式的 execPath 是 electron.exe，不带上项目根的话开机只会拉起一个空壳
  assert.ok(main.includes('args: app.isPackaged ? [] : [app.getAppPath()]'));
});

test('external links only open http(s) on an allowlisted host', () => {
  for (const allowed of [
    'https://www.douyin.com/video/7699857912785187043',
    'https://www.iesdouyin.com/share/video/1/?share_sign=x',
    'https://b23.tv/abc',
    'http://douyin.com/video/1',
  ]) {
    assert.equal(guard.isAllowedExternal(allowed), true, allowed);
  }
  for (const blocked of [
    'https://evil.com/douyin.com',
    'https://douyin.com.evil.com/x',
    'https://notdouyin.com/x',
    'javascript:alert(1)',
    'file:///C:/Windows/System32/drivers/etc/hosts',
    'data:text/html,<script>alert(1)</script>',
    '',
    null,
  ]) {
    assert.equal(guard.isAllowedExternal(blocked), false, String(blocked));
  }
});

test('external links go through the guard, never straight to shell', () => {
  assert.ok(main.includes("secureIpc('app:open-external'"));
  assert.ok(main.includes('isAllowedExternal'));
  assert.ok(preload.includes('app:open-external'));
  assert.ok(!main.includes('shell.openExternal(rawUrl)'));
});

// 父进程消失时 stdout 变断管，未处理的 EPIPE 会让主进程整窗崩（已实测复现）。
// 这层兜底必须一直在，不能被顺手删掉。
test('main process installs the broken-pipe guard before doing anything else', () => {
  assert.ok(main.includes("require('./main-log.cjs')"));
  assert.ok(main.includes('mainLog.install()'));
  assert.ok(main.includes("mainLog.append('startup'"));
  assert.ok(!main.includes('fs.appendFileSync(startupLog'), '不许绕过 mainLog 直接写启动日志');
  const installAt = main.indexOf('mainLog.install()');
  const windowAt = main.indexOf('function createWindow');
  assert.ok(installAt > 0 && installAt < windowAt, '兜底要在创建窗口之前装好');
});

// 真机事故：旧网关没人认领仍占着 8787，新网关也「启动成功」，请求落到旧那个，
// 界面报「桌面会话鉴权失败」。只验 /healthz 不足以说明这个网关认我们的凭据。
test('gateway readiness requires the session token to be accepted', () => {
  assert.ok(main.includes('function sessionStatus'), '需要带凭据探测的助手');
  assert.ok(main.includes("Authorization: `Bearer ${token}`"));
  assert.ok(main.includes("=== 200 ? 'ready' : 'unauthorized'"), '启动判定要验 200');
  assert.ok(main.includes('sessionStatus(info.port, token)'), '重接管也要验一次');
  assert.ok(main.includes('端口上还有别的网关在应答'));
});

// 真机问题：收件箱/日志尾随/雷达分页/正文检索的请求全都带查询串，旧的内联正则
// 只认 `/api/v1/[A-Za-z0-9_./-]`，于是一律被拒成 invalid api path —— 界面上一片红字，
// 收件箱永远是空表，而库里其实已经有消息。查询串必须放行，别的口子不许跟着开。
const apiPath = require('../api-path.cjs');

test('gateway api paths may carry a query string', () => {
  for (const allowed of [
    '/api/v1/messages?limit=30',
    '/api/v1/messages?limit=30&platform=douyin&status=needs_review',
    '/api/v1/conversations?limit=20',
    '/api/v1/logs?lines=200&level=warning',
    '/api/v1/messages?limit=30&q=%E4%BD%A0%E5%A5%BD',
    '/api/v1/radar/videos?page=1&size=20',
    '/api/v1/messages/12/confirm',
    '/api/v1/stats',
  ]) {
    assert.equal(apiPath.isAllowedApiPath(allowed), true, allowed);
  }
});

test('api path guard still blocks traversal, version switch and injection', () => {
  for (const blocked of [
    '/api/v1/../secret',
    '/api/v1/..%2fsecret',
    '/api/v2/messages',
    '/api/v1/messages?limit=30\nX-Injected: 1',
    '/api/v1/messages?limit=30\r\nHost: evil',
    '/api/v1/messages?limit=30#fragment',
    '/api/v1/messages?limit=30 HTTP/1.1',
    `/api/v1/${'a'.repeat(3000)}`,
    'http://127.0.0.1:8787/api/v1/messages',
    ' /api/v1/messages',
    '',
    null,
    42,
  ]) {
    assert.equal(apiPath.isAllowedApiPath(blocked), false, String(blocked).slice(0, 60));
  }
});

test('main process validates api paths through the shared guard', () => {
  assert.ok(main.includes("require('./api-path.cjs')"));
  assert.ok(main.includes('isAllowedApiPath(pathname)'));
  assert.ok(!main.includes('A-Za-z0-9_./-'), '旧的内联白名单正则不许回到 main.cjs');
});
