// 渲染进程能打到本机网关的路径白名单。
//
// 只放行 /api/v1/ 下的接口。查询串（`?limit=30&platform=douyin`）是正常业务：
// 收件箱、日志尾随、雷达分页、正文检索全靠它，拦掉这些等于把界面拍瘸。
// 但路径部分仍然不许出现 `..`、控制字符、`#` 片段，也不许换成别的 API 版本。
//
// 注意：这里不 trim。前端送进来的空格、换行、制表符一律原样拒绝，
// 免得 `"/api/v1/messages?limit=30\x20HTTP/1.1\r\nX: y"` 这类注入混进 http.request。
const PATH_PREFIX = '/api/v1/';
const MAX_PATH_LENGTH = 2048;
// 路径段：字母数字、下划线、点、斜杠、连字符。
// 查询串额外允许 URLSearchParams 会产出的 ? & = % + ~ 以及常见的 , : @ ! * ' ( ) 保留字。
const PATH_PATTERN = /^\/api\/v1\/[A-Za-z0-9_./-]+(?:\?[A-Za-z0-9_.~%+=&:@!*'(),/-]*)?$/;

function isAllowedApiPath(pathname) {
  if (typeof pathname !== 'string') return false;
  if (pathname.length === 0 || pathname.length > MAX_PATH_LENGTH) return false;
  if (!pathname.startsWith(PATH_PREFIX)) return false;
  // `..` 在路径段和查询串里都不接受：前者是穿越，后者是 %2e%2e 这类编码绕过的落点。
  if (pathname.includes('..')) return false;
  if (!PATH_PATTERN.test(pathname)) return false;
  return true;
}

module.exports = { PATH_PREFIX, MAX_PATH_LENGTH, PATH_PATTERN, isAllowedApiPath };
