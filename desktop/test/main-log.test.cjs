const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { EventEmitter } = require('node:events');

const { createMainLog, isBrokenPipeError, redact } = require('../main-log.cjs');

function tmpLog() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ilink-mainlog-'));
  return path.join(dir, 'desktop-startup.log');
}

function stream(options = {}) {
  const s = new EventEmitter();
  s.writable = options.writable !== false;
  s.destroyed = options.destroyed === true;
  s.chunks = [];
  s.write = options.write || (chunk => { s.chunks.push(String(chunk)); return true; });
  return s;
}

function epipe(message = 'write EPIPE') {
  return Object.assign(new Error(message), { code: 'EPIPE' });
}

test('断管类错误被识别，普通异常不被误判', () => {
  assert.equal(isBrokenPipeError(epipe()), true);
  assert.equal(isBrokenPipeError(new Error('EPIPE: broken pipe, write')), true);
  assert.equal(isBrokenPipeError(Object.assign(new Error('x'), { code: 'ERR_STREAM_DESTROYED' })), true);
  assert.equal(isBrokenPipeError(Object.assign(new Error('x'), { code: 'ERR_STREAM_WRITE_AFTER_END' })), true);
  assert.equal(isBrokenPipeError(new Error('boom')), false);
  assert.equal(isBrokenPipeError(null), false);
});

test('日志落盘会脱敏长串', () => {
  const file = tmpLog();
  const log = createMainLog({ logFile: () => file, proc: new EventEmitter(), stdout: stream(), stderr: stream() });
  log.append('startup', `token=${'A'.repeat(64)} end`);
  const text = fs.readFileSync(file, 'utf8');
  assert.match(text, /\[redacted\]/);
  assert.ok(!text.includes('A'.repeat(40)));
  assert.equal(redact('short'), 'short');
});

// 复现截图里那条崩溃：父进程没了 → stdout 写 EPIPE → console.error → 整窗崩
test('stdout 断管后 console.* 不再抛异常，进程能继续', () => {
  const file = tmpLog();
  const proc = new EventEmitter();
  const stdout = stream({ write: () => { throw epipe(); } });
  const stderr = stream();
  const log = createMainLog({ logFile: () => file, proc, stdout, stderr });
  log.install();
  try {
    assert.doesNotThrow(() => console.log('hello'));
    assert.doesNotThrow(() => console.error('still alive'));
  } finally {
    log.restore();
  }
  const text = fs.readFileSync(file, 'utf8');
  assert.match(text, /stdout write failed: EPIPE/);
  assert.match(text, /hello/);
  assert.match(text, /still alive/);
  assert.equal(log.isBroken(), true);
});

test('stdout 收到 EPIPE error 事件时只记一行，不再往上写', () => {
  const file = tmpLog();
  const proc = new EventEmitter();
  const stdout = stream();
  const stderr = stream();
  const log = createMainLog({ logFile: () => file, proc, stdout, stderr });
  log.install();
  try {
    stdout.emit('error', epipe());
    assert.doesNotThrow(() => console.log('after pipe closed'));
  } finally {
    log.restore();
  }
  const text = fs.readFileSync(file, 'utf8');
  assert.match(text, /stdout closed by parent process \(EPIPE\)/);
  assert.equal(stdout.chunks.length, 0, '断管后不应再往 stdout 写');
  assert.equal(log.isBroken(), true);
});

test('断管不进 onFatal，真异常才上报', () => {
  const file = tmpLog();
  const proc = new EventEmitter();
  const fatal = [];
  const log = createMainLog({
    logFile: () => file, proc, stdout: stream(), stderr: stream(),
    onFatal: error => fatal.push(error),
  });
  log.install();
  try {
    proc.emit('uncaughtException', epipe('EPIPE: broken pipe, write'));
    assert.equal(fatal.length, 0, '断管不该弹框');
    proc.emit('uncaughtException', new Error('boom'));
    assert.equal(fatal.length, 1);
    assert.equal(fatal[0].message, 'boom');
    proc.emit('unhandledRejection', new Error('nobody caught me'));
    assert.equal(fatal.length, 1, '未处理 rejection 只记日志');
  } finally {
    log.restore();
  }
  const text = fs.readFileSync(file, 'utf8');
  assert.match(text, /uncaughtException: Error: boom/);
  assert.match(text, /unhandledRejection: Error: nobody caught me/);
});

test('日志超过上限会滚动，旧内容进 .1', () => {
  const file = tmpLog();
  const log = createMainLog({
    logFile: () => file, proc: new EventEmitter(), stdout: stream(), stderr: stream(), maxBytes: 120,
  });
  for (let i = 0; i < 6; i += 1) log.append('startup', `line-${i}-${'x'.repeat(20)}`);
  assert.ok(fs.existsSync(`${file}.1`), '应生成滚动文件');
  assert.ok(fs.statSync(file).size < 200);
});

test('日志目录写不进去也不会带走进程', () => {
  const log = createMainLog({
    logFile: () => path.join(os.tmpdir(), 'ilink-missing-dir-that-cannot-be-made', 'x', '\u0000bad', 'log.txt'),
    proc: new EventEmitter(), stdout: stream(), stderr: stream(),
  });
  assert.doesNotThrow(() => log.append('startup', 'whatever'));
});
