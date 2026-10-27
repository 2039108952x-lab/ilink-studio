const { app, BrowserWindow, Tray, Menu, nativeImage, ipcMain, dialog, shell } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const crypto = require('node:crypto');
const { isAllowedExternal } = require('./url-guard.cjs');
const { isAllowedApiPath } = require('./api-path.cjs');
const { createMainLog } = require('./main-log.cjs');

app.setName('iLink Studio');
const ROOT = app.isPackaged
  ? path.join(process.resourcesPath, 'app.asar')
  : path.resolve(__dirname, '..');
const DEV = !app.isPackaged;
const HOST = '127.0.0.1';
const PORT = Number(process.env.ILINK_PORT || 8787);
if (!Number.isInteger(PORT) || PORT < 1 || PORT > 65535) throw new Error('invalid loopback port');
const EXPECTED_GATEWAY = '0.1.0-rc1';
const GATEWAY_EXE = process.env.ILINK_GATEWAY_EXE || (DEV
  ? null
  : path.join(process.resourcesPath, 'gateway', 'gateway.exe'));
const PLAYWRIGHT_DIR = DEV ? null : path.join(process.resourcesPath, 'playwright');
// 主进程日志与断管兜底：父进程消失后 stdout 会变断管，console.* 抛 EPIPE 就会
// 弹「A JavaScript error occurred in the main process」。装完这层以后只记日志不崩。
const mainLog = createMainLog({
  logFile: () => path.join(dataDir(), '..', 'logs', 'desktop-startup.log'),
  onFatal: error => {
    // 真错误仍然弹框，但由我们控制文案，不再走 Electron 默认的整窗崩溃
    try { dialog.showErrorBox('iLink Studio 主进程错误', String((error && error.message) || error)); } catch {}
  },
});
mainLog.install();
function logStartup(message) { mainLog.append('startup', message); }
let mainWindow = null;
let tray = null;
let gateway = null;
let gatewayOwned = false;
let quitting = false;
let gatewayGeneration = 0;
let gatewayPort = PORT;
let sessionToken = '';
let paused = false;

function dataDir() {
  const override = process.env.ILINK_DATA_DIR;
  if (override) return path.resolve(override);
  // 发布模式按 PRD §15.1：%LOCALAPPDATA%\iLink Studio\data，与 gateway/paths.py 一致
  if (!DEV) {
    const local = process.env.LOCALAPPDATA || app.getPath('userData');
    return path.join(local, 'iLink Studio', 'data');
  }
  return path.join(ROOT, 'data');
}
function runtimeFile() { return path.join(dataDir(), 'runtime.json'); }
function gatewayOrigin(port = gatewayPort) { return `http://${HOST}:${port}`; }
function request(port, pathname) {
  return new Promise((resolve, reject) => {
    const req = http.request({ hostname: HOST, port, path: pathname, method: 'GET', headers: { Host: `${HOST}:${port}` }, timeout: 1500 }, res => {
      const chunks = [];
      res.on('data', chunk => chunks.push(chunk));
      res.on('end', () => {
        try { resolve({ status: res.statusCode, body: JSON.parse(Buffer.concat(chunks).toString('utf8')) }); }
        catch { reject(new Error('invalid gateway response')); }
      });
    });
    req.on('timeout', () => req.destroy(new Error('gateway timeout')));
    req.on('error', reject);
    req.end();
  });
}
function waitForGateway(port, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = async () => {
      try {
        const r = await request(port, '/healthz');
        if (r.status === 200 && r.body.version === EXPECTED_GATEWAY) return resolve(r.body);
      } catch {}
      if (Date.now() >= deadline) return reject(new Error('网关启动超时，请查看本机日志'));
      setTimeout(attempt, 200);
    };
    attempt();
  });
}
function runHelper(args) {
  return new Promise((resolve, reject) => {
    const executable = GATEWAY_EXE || 'python';
    const child = spawn(executable, args, {
      cwd: path.dirname(executable), windowsHide: true,
      env: gatewayEnv(),
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    const stdout = [], stderr = [];
    child.stdout.on('data', chunk => stdout.push(chunk));
    child.stderr.on('data', chunk => stderr.push(chunk));
    child.once('error', reject);
    child.once('exit', code => {
      if (code !== 0) return reject(new Error('网关重接管失败'));
      const token = Buffer.concat(stdout).toString('utf8').trim();
      if (token.length < 43 || token.includes('\n')) return reject(new Error('网关会话格式无效'));
      resolve(token);
    });
  });
}
async function adoptGateway() {
  let info;
  try { info = JSON.parse(fs.readFileSync(runtimeFile(), 'utf8')); } catch { return null; }
  if (!Number.isInteger(info.port) || info.port !== PORT) throw new Error('已有网关端口配置不匹配，请先退出旧网关');
  let health;
  try { health = await request(info.port, '/healthz'); } catch { return null; }
  if (health.body.version !== EXPECTED_GATEWAY) {
    throw new Error(`127.0.0.1:${info.port} 是旧版或其他网关，未接管、未停止。请先按你的安排处理它，再启动 iLink Studio。`);
  }
  const args = GATEWAY_EXE
    ? ['--reauth', '--port', String(info.port)]
    : ['-u', '-m', 'gateway.session', '--port', String(info.port)];
  const token = await runHelper(args);
  if (await sessionStatus(info.port, token) !== 200) {
    throw new Error('重接管拿到的会话凭据不被网关接受，请退出那个网关后重新启动');
  }
  return { port: info.port, token };
}
// 用这次生成的会话凭据敲一次控制面：只通了 /healthz 不算启动成功。
// 端口上残留着别的网关时，healthz 会照常 200，但 /api/v1/runtime 是 401。
function sessionStatus(port, token) {
  return new Promise(resolve => {
    const req = http.request({
      hostname: HOST, port, path: '/api/v1/runtime', method: 'GET',
      headers: { Host: `${HOST}:${port}`, Origin: `http://${HOST}:${port}`, Authorization: `Bearer ${token}` },
      timeout: 4000,
    }, res => { res.resume(); res.on('end', () => resolve(res.statusCode || 0)); });
    req.on('timeout', () => req.destroy(new Error('session check timeout')));
    req.on('error', () => resolve(0));
    req.end();
  });
}
function gatewayEnv() {
  return {
    ...process.env,
    PYTHONUTF8: '1',
    ILINK_DATA_DIR: dataDir(),
    ILINK_RESOURCE_DIR: DEV ? ROOT : path.join(process.resourcesPath, 'gateway', '_internal'),
    ...(PLAYWRIGHT_DIR ? { PLAYWRIGHT_BROWSERS_PATH: PLAYWRIGHT_DIR } : {}),
    ILINK_PRODUCTION: DEV ? (process.env.ILINK_PRODUCTION || '0') : '1',
  };
}
async function launchGateway(token) {
  if (gateway || gatewayOwned) throw new Error('gateway already launched');
  if (GATEWAY_EXE && !fs.existsSync(GATEWAY_EXE)) throw new Error('发布目录缺少随包 gateway.exe');
  const executable = GATEWAY_EXE || 'python';
  const args = GATEWAY_EXE
    ? ['--ui-token-stdin', '--port', String(PORT)]
    : ['-u', '-m', 'gateway.desktop', '--ui-token-stdin', '--port', String(PORT)];
  const child = spawn(executable, args, {
    cwd: path.dirname(executable), windowsHide: true,
    // detached + unref：网关是后台服务，UI 崩溃/被强杀后仍要继续跑，
    // 下次启动读 runtime.json 重接管，而不是再起第二个网关。
    detached: true,
    env: gatewayEnv(),
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  child.unref();
  gateway = child;
  gatewayOwned = true;
  gatewayPort = PORT;
  const generation = ++gatewayGeneration;
  let errorBuffer = '';
  const exit = new Promise(resolve => child.once('exit', (code, signal) => resolve({ code, signal })));
  child.stderr.on('data', chunk => {
    const text = chunk.toString();
    errorBuffer = (errorBuffer + text).slice(-6000);
    logStartup(`gateway stderr: ${text}`);
  });
  child.once('error', error => {
    logStartup(`gateway spawn error: ${error.message}`);
    if (generation === gatewayGeneration) gateway = null;
  });
  child.once('exit', (code, signal) => {
    logStartup(`gateway exit: code=${code} signal=${signal}`);
    if (generation === gatewayGeneration) gateway = null;
  });
  child.stdin.end(token + '\n');
  const outcome = await Promise.race([
    waitForGateway(PORT).then(async () => ((await sessionStatus(PORT, token)) === 200 ? 'ready' : 'unauthorized')),
    exit.then(() => 'exit'),
  ]);
  if (outcome === 'unauthorized') {
    if (generation === gatewayGeneration) { gateway = null; gatewayOwned = false; }
    throw new Error(`127.0.0.1:${PORT} 上的网关不接受这次会话凭据：端口上还有别的网关在应答。请先退出那个网关（托盘「退出」或结束那个进程）再启动。`);
  }
  if (outcome !== 'ready') {
    const diagnostic = errorBuffer.trim().replace(/\n/g, ' | ').slice(-800);
    if (generation === gatewayGeneration) { gateway = null; gatewayOwned = false; }
    throw new Error(`网关未能启动${diagnostic ? `：${diagnostic}` : '，请查看本机启动日志'}`);
  }
  return PORT;
}
async function stopOwnedGateway() {
  const child = gateway;
  if (!child || !gatewayOwned) return;
  await new Promise(resolve => {
    let settled = false;
    const done = () => { if (settled) return; settled = true; clearTimeout(timer); resolve(); };
    const timer = setTimeout(done, 10000);
    child.once('exit', done);
    if (child.exitCode !== null) return done();
    const req = http.request({
      hostname: HOST, port: gatewayPort, path: '/api/v1/runtime/shutdown', method: 'POST',
      headers: { Host: `${HOST}:${gatewayPort}`, Origin: gatewayOrigin(), Authorization: `Bearer ${sessionToken}`, 'Content-Type': 'application/json', 'Content-Length': 2 },
      timeout: 5000,
    }, res => { res.resume(); res.on('end', () => {}); });
    req.on('error', () => {});
    req.on('timeout', () => req.destroy());
    req.end('{}');
  });
  gateway = null;
  gatewayOwned = false;
}

const windowOptions = {
  width: 1180, height: 790, minWidth: 960, minHeight: 640,
  autoHideMenuBar: true, backgroundColor: '#f4f6f3', show: false,
  title: 'iLink Studio',
  webPreferences: {
    preload: path.join(__dirname, 'preload.cjs'),
    contextIsolation: true, sandbox: true, nodeIntegration: false,
    webSecurity: true, devTools: DEV, allowRunningInsecureContent: false,
    runAsNode: false,
  },
};
function createWindow() {
  if (mainWindow && !mainWindow.isDestroyed()) { mainWindow.show(); mainWindow.focus(); return mainWindow; }
  const window = new BrowserWindow(windowOptions);
  mainWindow = window;
  window.once('ready-to-show', () => window.show());
  window.on('close', event => { if (!quitting) { event.preventDefault(); window.hide(); } });
  window.on('closed', () => { if (mainWindow === window) mainWindow = null; });
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', event => event.preventDefault());
  window.webContents.on('will-attach-webview', event => { event.preventDefault(); });
  return window;
}
function trayImage() {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><rect x="2" y="3" width="28" height="24" rx="7" fill="#294c3b"/><path d="M9 11h14M9 17h9M9 23h6" stroke="#d7ead6" stroke-width="2" stroke-linecap="round"/></svg>`;
  return nativeImage.createFromDataURL('data:image/svg+xml;base64,' + Buffer.from(svg).toString('base64'));
}
function createTray() {
  if (tray) return;
  tray = new Tray(trayImage());
  tray.setToolTip('iLink Studio · 本机网关');
  tray.on('click', () => createWindow());
  updateTray();
}
function updateTray() {
  if (!tray) return;
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'iLink Studio', enabled: false },
    { label: gatewayOwned ? '网关运行中 · 平台未自动启用' : '已重接管原网关 · 未启动第二实例', enabled: false },
    { label: paused ? '恢复回复' : '暂停回复', click: async () => {
      const next = !paused;
      try { await apiRequest('/api/v1/runtime/pause', 'POST', { paused: next }); paused = next; updateTray(); }
      catch (error) { dialog.showErrorBox('暂停状态未修改', String(error.message || error)); }
    } },
    { label: '打开控制台', click: () => createWindow() },
    { label: '打开日志目录', click: () => shell.openPath(path.join(dataDir(), '..', 'logs')) },
    { type: 'separator' },
    { label: gatewayOwned ? '退出（停止本应用网关）' : '关闭控制台（原网关保留）', click: async () => { quitting = true; await stopOwnedGateway(); app.quit(); } },
  ]));
}
// 登录启动项：打包态的 exe 自己就是应用；源码模式必须把项目根作为参数带上，
// 否则开机只会拉起一个没指定 app 目录的 electron 空壳。
function loginItemConfig(enabled) {
  return {
    openAtLogin: enabled,
    path: process.execPath,
    args: app.isPackaged ? [] : [app.getAppPath()],
  };
}
function currentAutostart() {
  const { path: execPath, args } = loginItemConfig(false);
  const settings = app.getLoginItemSettings({ path: execPath, args });
  return {
    enabled: settings.openAtLogin === true,
    mode: app.isPackaged ? 'packaged' : 'dev',
    path: execPath,
    args,
  };
}
function secureIpc(channel, handler) {
  ipcMain.handle(channel, async (event, ...args) => {
    const origin = new URL(event.senderFrame.url).origin;
    if (origin !== gatewayOrigin()) throw new Error('untrusted renderer');
    if (mainWindow && event.sender.id !== mainWindow.webContents.id) throw new Error('untrusted window');
    return handler(...args);
  });
}
function apiRequest(pathname, method = 'GET', body = undefined) {
  if (!isAllowedApiPath(pathname)) {
    return Promise.reject(new Error('invalid api path'));
  }
  if (!['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) return Promise.reject(new Error('invalid method'));
  return new Promise((resolve, reject) => {
    const payload = body === undefined ? null : Buffer.from(JSON.stringify(body));
    const req = http.request({
      hostname: HOST, port: gatewayPort, path: pathname, method,
      headers: { Host: `${HOST}:${gatewayPort}`, Origin: gatewayOrigin(), Authorization: `Bearer ${sessionToken}`, ...(payload ? { 'Content-Type': 'application/json', 'Content-Length': payload.length } : {}) },
      timeout: 60000,
    }, res => {
      const chunks = [];
      res.on('data', chunk => chunks.push(chunk));
      res.on('end', () => {
        try { resolve({ status: res.statusCode, body: JSON.parse(Buffer.concat(chunks).toString('utf8')) }); }
        catch { reject(new Error('invalid response')); }
      });
    });
    req.on('timeout', () => req.destroy(new Error('request timeout')));
    req.on('error', reject);
    if (payload) req.write(payload);
    req.end();
  });
}
const singleInstance = app.requestSingleInstanceLock();
if (!singleInstance) app.quit();
else {
  app.on('second-instance', () => createWindow());
  // 关窗口只隐藏到托盘；但主动退出（quitting=true）时必须让它真正结束进程。
  app.on('window-all-closed', event => { if (!quitting) event.preventDefault(); });
  app.on('before-quit', () => { quitting = true; });
  app.whenReady().then(async () => {
    sessionToken = crypto.randomBytes(32).toString('base64url');
    const adopted = await adoptGateway();
    if (adopted) sessionToken = adopted.token;
    else await launchGateway(sessionToken);
    secureIpc('gateway:request', request => apiRequest(request?.path, request?.method, request?.body));
    secureIpc('dialog:open-sticker', async () => {
      const result = await dialog.showOpenDialog(mainWindow, { properties: ['openFile'], filters: [{ name: 'Images', extensions: ['png', 'jpg', 'jpeg', 'gif', 'webp'] }] });
      return result.canceled ? null : result.filePaths[0];
    });
    secureIpc('dialog:choose-backend', async () => {
      const result = await dialog.showOpenDialog(mainWindow, {
        title: '选择 Reasonix 可执行文件',
        properties: ['openFile'],
        filters: [{ name: 'Reasonix 可执行文件', extensions: ['exe', 'cmd', 'bat'] }],
      });
      return result.canceled || !result.filePaths[0] ? null : result.filePaths[0];
    });
    secureIpc('app:autostart', async enabled => {
      if (typeof enabled !== 'boolean') throw new Error('invalid autostart flag');
      const config = loginItemConfig(enabled);
      app.setLoginItemSettings(config);
      const current = app.getLoginItemSettings({ path: config.path, args: config.args }).openAtLogin;
      logStartup(`autostart request=${enabled} applied=${current} mode=${app.isPackaged ? 'packaged' : 'dev'}`);
      return { enabled: current, applied: current === enabled, reason: current === enabled ? '' : 'os_rejected' };
    });
    // 界面要有「关掉」的入口，就得能读到注册表里的真实状态，而不是配置里那个愿不愿意的值
    secureIpc('app:autostart-status', async () => currentAutostart());
    secureIpc('app:quit', async () => { quitting = true; await stopOwnedGateway(); app.quit(); });
    secureIpc('app:open-log-folder', async () => { await shell.openPath(path.join(dataDir(), '..', 'logs')); });
    // 外链只走系统浏览器，且必须先过白名单；渲染进程给的字符串一律不信
    secureIpc('app:open-external', async rawUrl => {
      if (!isAllowedExternal(rawUrl)) throw new Error('external url not allowed');
      const target = new URL(String(rawUrl).trim()).toString();
      await shell.openExternal(target);
      return { opened: target };
    });
    const runtime = await apiRequest('/api/v1/runtime');
    if (runtime.status !== 200) throw new Error('桌面会话鉴权失败');
    paused = runtime.body.paused === true;
    createTray();
    createWindow().loadURL(gatewayOrigin());
    logStartup(`desktop ready on ${gatewayOrigin()} ownedGateway=${gatewayOwned}`);
  }).catch(error => {
    logStartup(`desktop startup failed: ${error.stack || error.message || error}`);
    dialog.showErrorBox('iLink Studio 启动失败', String(error.message || error));
    app.quit();
  });
}
module.exports = { windowOptions, gatewayOrigin, dataDir };
