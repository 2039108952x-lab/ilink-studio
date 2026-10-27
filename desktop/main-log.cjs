'use strict';
// 主进程的 stdout/stderr 是被父进程（终端、dev 脚本、后台任务）接管的管道。
// 父进程一消失就变成断管：此后任何 console.* 都会抛 EPIPE，未处理就是
// 「A JavaScript error occurred in the main process」整窗崩掉。
// 这里做三件事：兜住断管、把 console 落盘、真错误才上报给调用方。

const fs = require('node:fs');
const path = require('node:path');
const util = require('node:util');

const PIPE_CODES = new Set(['EPIPE', 'ECONNRESET', 'ERR_STREAM_DESTROYED', 'ERR_STREAM_WRITE_AFTER_END']);
const PIPE_TEXT = /broken pipe|write after end|stream (?:is )?destroyed|socket hang up|read end/i;
const DEFAULT_MAX_BYTES = 2 * 1024 * 1024;
const MAX_LINE = 4000;

function isBrokenPipeError(error) {
  if (!error) return false;
  if (PIPE_CODES.has(error.code)) return true;
  return PIPE_TEXT.test(String(error.message || error));
}

// 与旧 logStartup 同一条脱敏规则：长串一律当凭据
function redact(text) {
  return String(text).replace(/[A-Za-z0-9_-]{40,}/g, '[redacted]');
}

function createMainLog(options = {}) {
  const {
    logFile,                    // () => string，惰性求值：路径依赖 app 就绪后的数据目录
    maxBytes = DEFAULT_MAX_BYTES,
    onFatal = null,             // 真错误交给调用方（弹框），断管不交
    proc = process,
    stdout = process.stdout,
    stderr = process.stderr,
    stamp = () => new Date().toISOString(),
    format = (...args) => util.format(...args),
  } = options;

  const outputs = [
    { stream: stdout, label: 'stdout', broken: false },
    { stream: stderr, label: 'stderr', broken: false },
  ];
  let installed = false;
  let restored = false;
  let originals = null;

  function rotate(file) {
    if (!(maxBytes > 0)) return;
    let size = 0;
    try { size = fs.statSync(file).size; } catch { return; }
    if (size < maxBytes) return;
    try { fs.renameSync(file, `${file}.1`); }
    catch { try { fs.truncateSync(file, 0); } catch {} }
  }

  function append(level, message) {
    if (typeof logFile !== 'function') return;
    try {
      const file = logFile();
      if (!file) return;
      fs.mkdirSync(path.dirname(file), { recursive: true });
      rotate(file);
      fs.appendFileSync(file, `${stamp()} ${level} ${redact(message).slice(0, MAX_LINE)}\n`);
    } catch {
      // 日志写不进去也不能带走进程
    }
  }

  function writeThrough(output, text) {
    if (output.broken) return;
    const stream = output.stream;
    try {
      if (!stream || typeof stream.write !== 'function' || stream.destroyed || stream.writable === false) {
        output.broken = true;
        return;
      }
      stream.write(text);
    } catch (error) {
      if (isBrokenPipeError(error)) output.broken = true;
      append('warn', `${output.label} write failed: ${error.code || ''} ${error.message || error}`);
    }
  }

  function install() {
    if (installed) return handle;
    installed = true;

    for (const output of outputs) {
      const stream = output.stream;
      if (!stream || typeof stream.on !== 'function') continue;
      stream.on('error', error => {
        // 断管以后不再往这个流写东西，只留一行记录，进程继续跑
        if (isBrokenPipeError(error)) {
          output.broken = true;
          append('warn', `${output.label} closed by parent process (${error.code || error.message || error})`);
          return;
        }
        append('warn', `${output.label} error: ${error.code || ''} ${error.message || error}`);
      });
    }

    if (proc && typeof proc.on === 'function') {
      proc.on('uncaughtException', error => {
        if (isBrokenPipeError(error)) {
          append('warn', `swallowed broken pipe: ${error.message || error}`);
          return;
        }
        append('error', `uncaughtException: ${error.stack || error.message || error}`);
        if (typeof onFatal === 'function') onFatal(error);
      });
      proc.on('unhandledRejection', reason => {
        if (isBrokenPipeError(reason)) return;
        const error = reason instanceof Error ? (reason.stack || reason.message) : reason;
        append('error', `unhandledRejection: ${error}`);
      });
    }

    const levels = [
      ['log', 'info', outputs[0]],
      ['info', 'info', outputs[0]],
      ['debug', 'debug', outputs[0]],
      ['warn', 'warn', outputs[1]],
      ['error', 'error', outputs[1]],
    ];
    originals = new Map();
    for (const [method, level, output] of levels) {
      const original = console[method];
      originals.set(method, original);
      console[method] = (...args) => {
        const text = format(...args);
        append(level, text);
        writeThrough(output, `${text}\n`);
      };
    }
    return handle;
  }

  function restore() {
    if (!installed || restored || !originals) return;
    restored = true;
    for (const [method, original] of originals) {
      if (typeof original === 'function') console[method] = original;
    }
  }

  const handle = { append, install, restore, isBroken: () => outputs.some(output => output.broken) };
  return handle;
}

module.exports = { createMainLog, isBrokenPipeError, redact };
