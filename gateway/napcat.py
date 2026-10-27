"""本机 NapCat 探测：告诉界面真实的 WebUI / OneBot 端口，而不是让用户去猜。

NapCat 的端口写在它自己的 config 里（``webui.json`` / ``onebot11_<qq>.json``），
默认值 6099/3001 只是「通常如此」。用户机器上装没装、跑没跑，都要实测。
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_WEBUI_PORT = 6099
DEFAULT_ONEBOT_PORT = 3001


def _looks_like_napcat(path: Path) -> bool:
    return (path / "NapCatWinBootMain.exe").is_file() or (path / "config").is_dir()


def find_napcat_dir() -> Path | None:
    """按常见安装位置找 NapCat；找不到就返回 None，不做任何安装动作。"""
    candidates = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "NapCat")
    override = os.environ.get("ILINK_NAPCAT_DIR", "").strip()
    if override:
        candidates.insert(0, Path(override))
    for candidate in candidates:
        if _looks_like_napcat(candidate):
            return candidate
    return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_qq_exe() -> str:
    """从注册表找 QQ.exe 的绝对路径；NapCat 的启动脚本也是这么找的。"""
    try:
        import winreg
    except ImportError:  # 非 Windows
        return ""
    keys = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
    )
    for root, path in keys:
        try:
            with winreg.OpenKey(root, path) as key:
                uninstall, _ = winreg.QueryValueEx(key, "UninstallString")
        except OSError:
            continue
        # 注册表里常带引号：`"D:\qq\Uninstall.exe"`，不去掉会算错目录
        raw = str(uninstall).strip().strip('"').strip()
        candidate = Path(raw).parent / "QQ.exe"
        if candidate.is_file():
            return str(candidate)
    return ""


def _napcat_env(directory: Path) -> dict[str, str]:
    """复刻 launcher-user.bat 设的环境变量；NapCat 内部要读它们。"""
    env = dict(os.environ)
    env.update({
        "NAPCAT_PATCH_PACKAGE": str(directory / "qqnt.json"),
        "NAPCAT_LOAD_PATH": str(directory / "loadNapCat.js"),
        "NAPCAT_INJECT_PATH": str(directory / "NapCatWinBootHook.dll"),
        "NAPCAT_LAUNCHER_PATH": str(directory / "NapCatWinBootMain.exe"),
        "NAPCAT_MAIN_PATH": str(directory / "napcat.mjs"),
    })
    return env


def launch(wait_seconds: float = 0.0) -> dict[str, Any]:
    """直接拉起 NapCat（等价于双击 launcher-user.bat，但不留 pause 等待按键）。

    只使用探测到的固定路径与注册表里的 QQ 路径，不接受任何外部参数。
    启动会重启 QQ 并注入 NapCat —— 这一点调用方必须先向用户说明。
    """
    import subprocess

    directory = find_napcat_dir()
    if directory is None:
        return {"started": False, "code": "not_installed",
                "message": "本机没有找到 NapCat，无法自动启动"}

    launcher = directory / "NapCatWinBootMain.exe"
    hook = directory / "NapCatWinBootHook.dll"
    if not launcher.is_file() or not hook.is_file():
        return {"started": False, "code": "napcat_incomplete",
                "message": "NapCat 目录里缺少启动程序或注入 DLL，请重新安装"}

    state = describe(directory)
    if state["onebot_running"] or state["webui_running"]:
        # WebUI 已在监听就说明 NapCat 进程还活着；再 spawn 会多注入一个 QQ
        return {"started": False, "code": "already_running",
                "message": f"NapCat 已经在运行（{state['onebot_ws']}）；"
                           f"扫码登录后 OneBot 才会开始监听"}

    qq_exe = find_qq_exe()
    if not qq_exe:
        return {"started": False, "code": "qq_missing",
                "message": "注册表里没找到已安装的 QQ；NapCat 需要 QQ NT 客户端才能注入"}

    try:
        # 不留黑窗，但把 NapCat 自己的输出落到文件里：出问题时能直接看为什么没起来
        log_dir = directory / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "ilink-launch.log"
        with open(log_file, "ab") as sink:
            sink.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} ilink 启动 ===\n".encode())
            sink.flush()
            process = subprocess.Popen(  # noqa: S603 - 参数全部来自固定探测结果
                [str(launcher), qq_exe, str(hook)],
                cwd=str(directory),
                env=_napcat_env(directory),
                close_fds=True,
                stdout=sink,
                stderr=subprocess.STDOUT,
                creationflags=(
                    getattr(subprocess, "DETACHED_PROCESS", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                ),
            )
    except OSError as exc:
        return {"started": False, "code": "spawn_failed",
                "message": f"启动 NapCat 失败：{type(exc).__name__}"}

    log.info("已拉起 NapCat（pid=%s），QQ 会被重启并注入", process.pid)
    if wait_seconds:
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if _port_open("127.0.0.1", DEFAULT_ONEBOT_PORT):
                break
            time.sleep(0.5)

    return {
        "started": True,
        "code": "started",
        "pid": process.pid,
        "qq_exe": qq_exe,
        "onebot_ws": state["onebot_ws"],
        "message": "NapCat 正在启动：QQ 会被重启并注入，随后在弹出的窗口里扫码登录 QQ",
    }


def qrcode_state(napcat_dir: Path | None = None, *, max_age: float = 180.0) -> dict[str, Any]:
    """NapCat 登录二维码。它只打在控制台和 cache/qrcode.png 里，界面上要能直接看到。

    NapCat 每次刷新都会覆盖同一个 PNG；这里只读文件，过期（默认 3 分钟）不返回，
    免得把早就失效的码显示给用户。
    """
    import base64
    import time

    directory = napcat_dir if napcat_dir is not None else find_napcat_dir()
    if directory is None or not _looks_like_napcat(directory):
        return {"available": False, "reason": "no_napcat"}

    image = directory / "cache" / "qrcode.png"
    if not image.is_file():
        return {"available": False, "reason": "no_qrcode"}

    age = time.time() - image.stat().st_mtime
    if age > max_age:
        return {"available": False, "reason": "expired", "age_seconds": round(age, 1)}

    try:
        raw = image.read_bytes()
    except OSError as exc:
        return {"available": False, "reason": f"read_failed:{type(exc).__name__}"}
    if not raw:
        return {"available": False, "reason": "empty"}

    url = ""
    log_file = directory / "logs" / "ilink-launch.log"
    if log_file.is_file():
        try:
            text = log_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for line in reversed(text.splitlines()):
            if "二维码解码URL" in line:
                url = line.split("二维码解码URL:", 1)[-1].strip()
                break

    return {
        "available": True,
        "data_url": "data:image/png;base64," + base64.b64encode(raw).decode("ascii"),
        "age_seconds": round(age, 1),
        "decoded_url": url,
    }


def describe(napcat_dir: Path | None = None) -> dict[str, Any]:
    """返回可直接展示给用户的事实：装在哪、端口多少、现在跑没跑。绝不返回 token。"""
    directory = napcat_dir if napcat_dir is not None else find_napcat_dir()
    if directory is not None and not _looks_like_napcat(directory):
        directory = None
    if directory is None:
        return {
            "installed": False,
            "dir": "",
            "webui_url": "",
            "webui_running": False,
            "onebot_ws": f"ws://127.0.0.1:{DEFAULT_ONEBOT_PORT}",
            "onebot_running": False,
            "launcher": "",
            "accounts": [],
            "message": "本机没有找到 NapCat；QQ 通道需要先安装 NapCat（OneBot 11 实现）",
        }

    config_dir = directory / "config"
    webui = _read_json(config_dir / "webui.json")
    webui_host = str(webui.get("host") or "127.0.0.1")
    webui_port = int(webui.get("port") or DEFAULT_WEBUI_PORT)

    onebot_ws = f"ws://127.0.0.1:{DEFAULT_ONEBOT_PORT}"
    onebot_host, onebot_port = "127.0.0.1", DEFAULT_ONEBOT_PORT
    accounts: list[str] = []
    for path in sorted(config_dir.glob("onebot11_*.json")):
        accounts.append(path.stem.split("_", 1)[-1])
        network = _read_json(path).get("network") or {}
        servers = network.get("websocketServers") or []
        for server in servers:
            if isinstance(server, dict) and server.get("enable", True):
                onebot_host = str(server.get("host") or onebot_host)
                onebot_port = int(server.get("port") or onebot_port)
                onebot_ws = f"ws://{onebot_host}:{onebot_port}"
                break
        break

    launcher = ""
    for name in ("launcher-user.bat", "launcher-win10-user.bat", "launcher.bat"):
        candidate = directory / name
        if candidate.is_file():
            launcher = str(candidate)
            break

    onebot_running = _port_open(onebot_host, onebot_port)
    webui_running = _port_open(webui_host, webui_port)
    if onebot_running:
        message = f"NapCat 正在运行：OneBot 在 {onebot_ws}，点「只读探活」核对机器人 UID"
    elif webui_running:
        # NapCat 起来了，但 OneBot 端口还没监听 —— 通常是 QQ 还没登录
        message = (
            f"NapCat 已启动，等 QQ 登录：在弹出的 QQ 窗口里扫码，"
            f"登录后 OneBot 会自动监听 {onebot_ws}（WebUI：http://{webui_host}:{webui_port}）"
        )
    else:
        message = (
            f"NapCat 已安装但没在运行：点「启动 NapCat」由本应用拉起，"
            f"之后在 QQ 窗口扫码，OneBot 会监听 {onebot_ws}"
        )
    return {
        "installed": True,
        "dir": str(directory),
        "webui_url": f"http://{webui_host}:{webui_port}",
        "webui_running": webui_running,
        "onebot_ws": onebot_ws,
        "onebot_running": onebot_running,
        "launcher": launcher,
        "accounts": accounts,
        "message": message,
    }
