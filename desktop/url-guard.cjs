// 外链守卫：只允许在系统浏览器里打开白名单域名上的 http(s) 链接。
//
// 渲染进程送进来的是任意字符串，所以这里两道都要挡：协议（挡住 javascript: / file:）
// 和域名（挡住把 douyin.com 塞进别的站点路径里那种绕过）。
const ALLOWED_HOSTS = Object.freeze([
  'douyin.com',
  'iesdouyin.com',
  'bilibili.com',
  'b23.tv',
]);

function isAllowedExternal(rawUrl) {
  const text = String(rawUrl || '').trim();
  if (!text) return false;
  let parsed;
  try {
    parsed = new URL(text);
  } catch {
    return false;
  }
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') return false;
  const host = parsed.hostname.toLowerCase();
  // 等于白名单域名，或它的子域（www.douyin.com）。不带点前缀，避免 douyin.com.evil.com 混进来。
  return ALLOWED_HOSTS.some(allowed => host === allowed || host.endsWith(`.${allowed}`));
}

module.exports = { ALLOWED_HOSTS, isAllowedExternal };
