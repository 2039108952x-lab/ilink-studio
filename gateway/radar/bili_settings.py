"""刷B站 的设置：config.toml 给默认值，界面改的是**覆盖层**。

为什么不让界面直接写 config.toml
    项目铁律里 ``[platforms.*]`` 是只读段 —— 界面改它会被拒，而且打包后的
    ``config.toml`` 在资源目录里（更新会被覆盖）。所以这里照 ``ui-settings.json``
    的做法：界面写 ``<家目录>/radar-settings.json``，读的时候与 config 的默认值合并。

四页签怎么分（跟上游的设置页对齐，少一项都算丢功能）
    日常   峰谷自动刷（总开关 + 两个时段）· 每天最多挑几条
    偏好   人设提示词 · 关注关键词 · 默认收藏夹 · 日报写多细
    互动   自动评论并 @ · 每轮几条 · 回复评论 · 投递方式 · 私信发给谁
    高级   Python 路径 · 单次超时 · 本地转写
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 谷时窗口（北京时间）。官方口径：高峰 = 工作日 9:00–12:00 与 14:00–18:00，
# 其余全是谷时 ⇒ 中午 12–14、18:00 之后一整晚、以及**整个周末**都便宜。
DEFAULT_PEAKS = ["12:00-14:00", "18:00-09:00"]

DM_MODES = ("off", "only", "both")
REPORT_STYLES = ("full", "thin", "none")

# 界面可改的键 → (类型, 合法值/范围, 中文名)。白名单之外的键一律忽略。
FIELDS: dict[str, tuple[str, Any, str]] = {
    "peak_auto": ("bool", None, "峰谷自动刷"),
    "peaks": ("list", None, "谷时窗口"),
    "per_day": ("int", (0, 50), "每天最多挑几条"),
    "persona": ("str", None, "人设提示词"),
    "keywords": ("list", None, "关注关键词"),
    "favorite_to": ("str", None, "默认收藏夹"),
    "report_style": ("str", REPORT_STYLES, "日报详略"),
    "comment_auto": ("bool", None, "自动评论并 @"),
    "comment_per_round": ("int", (1, 20), "每轮最多几条评论"),
    "reply_auto": ("bool", None, "回复评论"),
    "dm_mode": ("str", DM_MODES, "投递方式"),
    "dm_uid": ("str", None, "私信发给谁"),
    "transcribe": ("bool", None, "本地转写"),
    "python": ("str", None, "Python 路径"),
    "mcp_timeout": ("int", (10, 900), "单次调用超时"),
}

# 四个页签分别放哪些键（界面直接照着渲染）
TABS: dict[str, tuple[str, ...]] = {
    "daily": ("peak_auto", "peaks", "per_day"),
    "preference": ("persona", "keywords", "favorite_to", "report_style"),
    "interact": ("comment_auto", "comment_per_round", "reply_auto", "dm_mode", "dm_uid"),
    "advanced": ("python", "mcp_timeout", "transcribe"),
}

TAB_LABELS = {"daily": "日常", "preference": "偏好", "interact": "互动", "advanced": "高级"}


def defaults_from_config(options: dict[str, Any] | None) -> dict[str, Any]:
    """config.toml 的 ``[platforms.bilibili]`` 段 → 一份完整默认值。"""
    opts = dict(options or {})
    defaults: dict[str, Any] = {
        "peak_auto": bool(opts.get("peak_auto", False)),
        "peaks": list(opts.get("peaks") or DEFAULT_PEAKS),
        "per_day": 5,
        "persona": str(opts.get("persona") or ""),
        "keywords": [str(k) for k in (opts.get("keywords") or [])],
        "favorite_to": str(opts.get("favorite_to") or ""),
        "report_style": str(opts.get("report_style") or "full"),
        "comment_auto": bool(opts.get("comment_auto", False)),
        "comment_per_round": 1,
        "reply_auto": bool(opts.get("reply_auto", False)),
        "dm_mode": str(opts.get("dm_mode") or "only"),
        "dm_uid": str(opts.get("dm_uid") or ""),
        "transcribe": bool(opts.get("transcribe", False)),
        "python": str(opts.get("python") or ""),
    }
    for key, cast in (("per_day", int), ("comment_per_round", int)):
        try:
            defaults[key] = cast(opts.get(key, defaults[key]) or defaults[key])
        except (TypeError, ValueError):
            pass
    try:
        defaults["mcp_timeout"] = int(opts.get("mcp_timeout") or 120)
    except (TypeError, ValueError):
        defaults["mcp_timeout"] = 120
    # 老配置里可能只填了关键词、没写 dm_mode：按上游 2.0 的默认「只发私信」走
    if defaults["dm_mode"] not in DM_MODES:
        defaults["dm_mode"] = "only"
    if defaults["report_style"] not in REPORT_STYLES:
        defaults["report_style"] = "full"
    return defaults


def _path(home: Path | str) -> Path:
    return Path(home) / "radar-settings.json"


def read_override(home: Path | str) -> dict[str, Any]:
    """读覆盖层。读不出来就当没有 —— 但**不会**把默认值一起吞掉。"""
    try:
        raw = json.loads(_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k in FIELDS or k in ("top", "notify", "webhook_url")}


def effective(home: Path | str, options: dict[str, Any] | None) -> dict[str, Any]:
    """默认值 + 覆盖层。业务侧读设置一律走这个。"""
    merged = defaults_from_config(options)
    merged.update(read_override(home))
    return merged


def _coerce(key: str, value: Any) -> Any:
    """按 schema 校验并归一。不合法就抛 ``ValueError``（调用方转成接口错误）。"""
    kind, rule, label = FIELDS[key]
    if kind == "bool":
        return bool(value)
    if kind == "int":
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"「{label}」要是数字") from None
        low, high = rule
        return max(low, min(high, number))
    if kind == "list":
        if isinstance(value, str):
            # 界面里关键词是一行一个 / 逗号分隔，两种都收
            items = [part.strip() for part in value.replace(",", "\n").splitlines()]
        elif isinstance(value, (list, tuple)):
            items = [str(part).strip() for part in value]
        else:
            raise ValueError(f"「{label}」要是列表")
        return [item for item in items if item]
    text = str(value or "").strip()
    if rule and text not in rule:
        raise ValueError(f"「{label}」只能是 {' / '.join(rule)}")
    return text


def write_override(home: Path | str, payload: dict[str, Any]) -> dict[str, Any]:
    """把界面改动落进覆盖层。只认白名单里的键，其余静默忽略。"""
    data = read_override(home)
    for key, value in (payload or {}).items():
        if key not in FIELDS:
            continue
        data[key] = _coerce(key, value)
    path = _path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise ValueError(f"设置写不进去：{exc}") from exc
    return data


def view(home: Path | str, options: dict[str, Any] | None) -> dict[str, Any]:
    """给界面的一份完整视图：值 + 页签分组 + 可选值说明。"""
    merged = effective(home, options)
    return {
        "values": merged,
        "tabs": {name: [{"key": key, "label": FIELDS[key][2], "kind": FIELDS[key][0],
                         "options": (list(FIELDS[key][1]) if isinstance(FIELDS[key][1], tuple)
                                     and FIELDS[key][0] == "str" else None)}
                        for key in keys]
                 for name, keys in TABS.items()},
        "tab_labels": dict(TAB_LABELS),
        "dm_modes": list(DM_MODES),
        "report_styles": list(REPORT_STYLES),
        "override_file": str(_path(home)),
    }
