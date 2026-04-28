#!/usr/bin/env python3
"""
剪贴板朗读工具 (Low Latency Edition)
快捷键: Alt+C
- 流式播放：首包返回后立即开始
- 性能监控：精确记录首响延迟
"""

import threading
import subprocess
import gc
import sys
import os
import datetime
import socket
import traceback
import asyncio
import time
import re
import argparse
import shutil
import errno
import signal
import ctypes
from ctypes import wintypes

try:
    import edge_tts
except ImportError:
    edge_tts = None

try:
    import pyperclip
    from pyperclip import PyperclipException
except ImportError:
    pyperclip = None
    PyperclipException = RuntimeError

try:
    from aiohttp import client_exceptions
    CLIENT_ERROR_TYPES = (client_exceptions.ClientError,)
except ImportError:
    CLIENT_ERROR_TYPES = ()

try:
    import keyboard
except ImportError:
    keyboard = None

try:
    EDGE_TTS_VERSION = edge_tts.__version__
except AttributeError:
    EDGE_TTS_VERSION = "unknown"


def get_app_base_path():
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def get_toggle_pid_path():
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return os.path.join(runtime_dir, TOGGLE_PID_FILENAME)
    return os.path.join("/tmp", f"{TOGGLE_PID_FILENAME}.{os.getuid()}")


def get_ffplay_path():
    """获取 ffplay 路径"""
    base_path = get_app_base_path()
    binary_name = "ffplay.exe" if sys.platform == "win32" else "ffplay"

    candidates = [
        os.path.join(base_path, binary_name),
        os.path.join(base_path, "bin", binary_name),
        os.path.join(base_path, "tools", "ffmpeg", binary_name),
        os.path.join(base_path, "tools", "ffmpeg", "bin", binary_name),
    ]

    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    tools_ffmpeg_path = os.path.join(base_path, "tools", "ffmpeg")
    if os.path.isdir(tools_ffmpeg_path):
        for root, _, files in os.walk(tools_ffmpeg_path):
            if binary_name in files:
                candidate = os.path.join(root, binary_name)
                if os.access(candidate, os.X_OK):
                    return candidate

    return "ffplay"


# --- 核心配置 ---
VOICE = "zh-CN-XiaoxiaoNeural"
RATE = "+0%"
SPEED = 3.0
VOLUME = 1.6
NETWORK_TIMEOUT_SECONDS = 15
MAX_RETRIES = 6
RETRY_BASE_DELAY = 1.2
HOTKEY_DEBOUNCE_SECONDS = 0.2

lock = threading.Lock()
is_playing = False
ffplay_process = None
start_press_time = None # 记录按下快捷键的时间
playback_session_id = 0
current_audio_file = None
last_hotkey_time = 0.0
hotkey_handler = None
hotkey_registered_at = 0.0
singleton_socket = None
active_stream_loop = None
active_stream_task = None
active_stream_session_id = None
last_memory_log_time = 0.0
toggle_pid_file = None

HOTKEY = "alt+c"
HOTKEY_REBIND_INTERVAL_SECONDS = 60.0
MEMORY_LOG_INTERVAL_SECONDS = 300.0
WINDOWS_HOTKEY_ID = 1
WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_NOREPEAT = 0x4000
VK_C = 0x43
TOGGLE_PID_FILENAME = "clipspeak-toggle.pid"

RE_SPLIT = re.compile(r'([。！？；!?;])')

LINUX_CLIPBOARD_COMMANDS = (
    ("wl-paste", ["wl-paste", "--no-newline"]),
    ("xclip", ["xclip", "-selection", "clipboard", "-out"]),
    ("xsel", ["xsel", "--clipboard", "--output"]),
)


def log(msg, level="INFO"):
    """带时间戳的日志输出"""
    if os.environ.get("CLIPSPEAK_SIMPLE_LOG") == "1":
        print(msg)
        return

    timestamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{timestamp}] [{level}] {msg}")


def get_linux_clipboard_hint():
    if os.environ.get("WAYLAND_DISPLAY"):
        return "没有找到可用的剪贴板命令。Wayland 下通常是 wl-paste，来自 wl-clipboard。"
    if os.environ.get("DISPLAY"):
        return "没有找到可用的剪贴板命令。X11 下通常是 xclip 或 xsel。"
    return "当前没有检测到 DISPLAY/WAYLAND_DISPLAY，可能不是图形桌面会话。"


def get_linux_clipboard_command():
    if not sys.platform.startswith("linux"):
        return None

    preferred_commands = LINUX_CLIPBOARD_COMMANDS
    if os.environ.get("WAYLAND_DISPLAY"):
        preferred_commands = (
            LINUX_CLIPBOARD_COMMANDS[0],
            LINUX_CLIPBOARD_COMMANDS[1],
            LINUX_CLIPBOARD_COMMANDS[2],
        )
    elif os.environ.get("DISPLAY"):
        preferred_commands = (
            LINUX_CLIPBOARD_COMMANDS[1],
            LINUX_CLIPBOARD_COMMANDS[2],
            LINUX_CLIPBOARD_COMMANDS[0],
        )

    for binary, command in preferred_commands:
        if shutil.which(binary):
            return command

    return None


def read_clipboard_text():
    linux_command = get_linux_clipboard_command()
    if linux_command:
        try:
            result = subprocess.run(
                linux_command,
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
            return result.stdout
        except subprocess.SubprocessError as exc:
            raise RuntimeError(f"剪贴板命令执行失败 ({linux_command[0]}): {exc}") from exc

    if pyperclip is None:
        raise RuntimeError("缺少 pyperclip，且没有找到系统剪贴板命令")

    try:
        return pyperclip.paste()
    except PyperclipException as exc:
        raise RuntimeError(str(exc)) from exc


def check_runtime_dependencies():
    missing_python_deps = []
    if edge_tts is None:
        missing_python_deps.append("edge-tts")
    if pyperclip is None and get_linux_clipboard_command() is None:
        missing_python_deps.append("pyperclip")

    if missing_python_deps:
        log(f"缺少 Python 依赖: {', '.join(missing_python_deps)}", "ERR")
        log("请先运行: pip install -r requirements.txt", "HINT")
        raise RuntimeError("missing Python dependencies")

    ffplay_path = get_ffplay_path()
    if os.path.isabs(ffplay_path) and os.path.exists(ffplay_path):
        return
    if shutil.which(ffplay_path):
        return

    log("找不到 ffplay，请先安装 ffmpeg 并确保 ffplay 在 PATH 中。", "ERR")
    if sys.platform.startswith("linux"):
        log("Debian/Ubuntu: sudo apt install ffmpeg", "HINT")
    raise RuntimeError("ffplay not found")


def build_atempo_filter(speed):
    """ffmpeg atempo 单次仅支持 0.5-2.0，超出时需要串联。"""
    filters = []
    remaining = speed

    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0

    filters.append(f"atempo={remaining:.3f}".rstrip("0").rstrip("."))
    return ",".join(filters)


def build_audio_filter():
    filters = []

    if abs(SPEED - 1.0) > 0.001:
        filters.append(build_atempo_filter(SPEED))

    if abs(VOLUME - 1.0) > 0.001:
        filters.append(f"volume={VOLUME:.3f}".rstrip("0").rstrip("."))

    return ",".join(filters)


def get_session_state():
    with lock:
        return playback_session_id, is_playing, ffplay_process, current_audio_file


def is_session_active(session_id):
    with lock:
        return is_playing and playback_session_id == session_id


def log_memory_stats():
    """内存健康度检查"""
    global last_memory_log_time
    now = time.monotonic()
    if now - last_memory_log_time < MEMORY_LOG_INTERVAL_SECONDS:
        return
    last_memory_log_time = now
    gc.collect()
    obj_count = len(gc.get_objects())
    log(f"内存快照: 对象数 {obj_count} | GC: {gc.get_stats()}", "MEM")


def explain_network_error(exc):
    """给 edge-tts 常见网络错误补充更具体的诊断信息。"""
    message = str(exc)

    if "speech.platform.bing.com" in message:
        return (
            "当前请求被 speech.platform.bing.com 主动断开。"
            "这更像是到微软 TTS 服务的链路波动，而不是播放器本身故障。"
        )

    if "api.msedgeservices.com" in message:
        return "已命中新版 Edge TTS 接口，当前更像是本机网络、代理或防火墙导致的连接中断。"

    if "Timeout" in message or "timed out" in message:
        return "请求超时，可能是到微软 TTS 服务的链路不稳定。"

    return None


def is_expected_tts_error(exc):
    return isinstance(
        exc,
        CLIENT_ERROR_TYPES
        + (
            asyncio.TimeoutError,
            TimeoutError,
            ConnectionError,
            OSError,
        ),
    )


def check_singleton():
    """单例检查"""
    global singleton_socket
    try:
        singleton_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        singleton_socket.bind(('127.0.0.1', 29921))
        return True
    except socket.error as e:
        singleton_socket = None
        if getattr(e, "errno", None) == errno.EADDRINUSE:
            print(f"Socket bind error: {e}")
            return None
        log(f"单例检查不可用，继续运行: {e}", "WARN")
        return True


def is_process_running(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def stop_toggle_process_if_running():
    pid_path = get_toggle_pid_path()
    try:
        with open(pid_path, "r", encoding="utf-8") as file:
            pid = int(file.read().strip())
    except (OSError, ValueError):
        return False

    if pid == os.getpid() or not is_process_running(pid):
        try:
            os.unlink(pid_path)
        except OSError:
            pass
        return False

    try:
        os.kill(pid, signal.SIGTERM)
        log(f"已请求停止当前朗读进程 (PID: {pid})", "USER")
    except OSError as exc:
        log(f"停止当前朗读进程失败 (PID: {pid}): {exc}", "WARN")
        return False

    return True


def register_toggle_process():
    global toggle_pid_file
    pid_path = get_toggle_pid_path()
    os.makedirs(os.path.dirname(pid_path), exist_ok=True)
    with open(pid_path, "w", encoding="utf-8") as file:
        file.write(str(os.getpid()))
    toggle_pid_file = pid_path


def unregister_toggle_process():
    global toggle_pid_file
    if not toggle_pid_file:
        return

    try:
        with open(toggle_pid_file, "r", encoding="utf-8") as file:
            pid = int(file.read().strip())
        if pid == os.getpid():
            os.unlink(toggle_pid_file)
    except (OSError, ValueError):
        pass
    finally:
        toggle_pid_file = None


def terminate_process(proc, timeout=1):
    if not proc:
        return

    try:
        if proc.stdin:
            proc.stdin.close()
    except Exception:
        pass

    if proc.poll() is not None:
        return

    try:
        proc.terminate()
        proc.wait(timeout=timeout)
        return
    except Exception:
        pass

    try:
        proc.kill()
        proc.wait(timeout=timeout)
    except Exception:
        pass


def wait_for_retry_or_stop(session_id, delay):
    deadline = time.monotonic() + delay
    while time.monotonic() < deadline:
        if not is_session_active(session_id):
            return False
        time.sleep(min(0.1, deadline - time.monotonic()))
    return is_session_active(session_id)


def stop_playback(clear_flags=True, session_id=None):
    """停止播放并执行深度清理"""
    global is_playing, ffplay_process, current_audio_file
    global active_stream_loop, active_stream_task, active_stream_session_id
    
    with lock:
        if session_id is not None and playback_session_id != session_id:
            return
        if clear_flags:
            if is_playing:
                log("状态机变更: Running -> Stopped", "STATE")
            is_playing = False
        proc = ffplay_process
        audio_file = current_audio_file
        stream_loop = active_stream_loop
        stream_task = active_stream_task
        stream_session_id = active_stream_session_id
        ffplay_process = None
        current_audio_file = None
        active_stream_loop = None
        active_stream_task = None
        active_stream_session_id = None
    
    if proc:
        proc_returncode = proc.poll()
        if proc_returncode is None:
            log(f"终止播放器进程 (PID: {proc.pid})", "CLEAN")
        else:
            log(f"播放器进程已退出 (PID: {proc.pid}, Exit Code: {proc_returncode})", "CLEAN")
        terminate_process(proc)

    if stream_loop and stream_task and (session_id is None or stream_session_id == session_id):
        try:
            stream_loop.call_soon_threadsafe(stream_task.cancel)
            log("已请求取消当前 TTS 流", "CLEAN")
        except Exception:
            pass

    if audio_file:
        try:
            os.unlink(audio_file)
            log(f"清理临时音频文件: {audio_file}", "CLEAN")
        except OSError:
            pass


def normalize_text(text):
    """轻量清洗文本，避免 markdown 噪音影响朗读。"""
    # Markdown 链接只保留可见文本，不朗读括号里的 URL。
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = text.replace("#", "").replace("*", "").replace("\r", "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def open_ffplay_process():
    """启动 ffplay，并准备从 stdin 接收音频流。"""
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    ffplay_args = [
        get_ffplay_path(),
        "-nodisp",
        "-autoexit",
        "-i",
        "pipe:0",
    ]

    # 流式 mp3 再叠加极端低延迟/二次加速时，容易出现中后段听感跳跃。
    # 默认改成只使用 TTS 请求端 2x，播放器侧保持 1x，更稳。
    audio_filter = build_audio_filter()
    if audio_filter:
        ffplay_args.extend(["-af", audio_filter])

    ffplay_args.extend(["-loglevel", "error"])

    return subprocess.Popen(
        ffplay_args,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=sys.stderr,
        creationflags=creationflags,
    )


def stream_audio_to_ffplay(text, session_id):
    """直接把 edge-tts 音频流写给 ffplay，实现首包即播。"""
    global ffplay_process, active_stream_loop, active_stream_task, active_stream_session_id
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        for attempt in range(MAX_RETRIES):
            if not is_session_active(session_id):
                return False

            proc = None
            first_chunk_time = None
            try:
                start_time = time.time()
                bytes_written = 0
                log(f"-> 开始流式生成音频 ({len(text)} 字符)...", "NET")
                proc = open_ffplay_process()
                with lock:
                    should_abort_proc = playback_session_id != session_id or not is_playing
                    if not should_abort_proc:
                        ffplay_process = proc

                if should_abort_proc:
                    terminate_process(proc)
                    return False

                log(f"播放器进程已挂载 (PID: {proc.pid})", "PROC")
                log("开始等待音频首包并直接播放", "INFO")
                communicate = edge_tts.Communicate(text, VOICE, rate=RATE)

                async def pump_stream():
                    nonlocal first_chunk_time, bytes_written
                    async for chunk in communicate.stream():
                        if not is_session_active(session_id):
                            return
                        if chunk["type"] != "audio":
                            continue
                        if proc.poll() is not None or proc.stdin is None:
                            raise BrokenPipeError("ffplay 已提前退出")
                        if first_chunk_time is None:
                            first_chunk_time = time.time()
                            latency = 0
                            if start_press_time:
                                latency = first_chunk_time - start_press_time
                            log(f"⚡ 首响延迟: {latency:.2f}秒 (流式首包播放)", "PERF")
                        proc.stdin.write(chunk["data"])
                        proc.stdin.flush()
                        bytes_written += len(chunk["data"])

                stream_task = loop.create_task(pump_stream())
                with lock:
                    should_abort_stream = playback_session_id != session_id or not is_playing
                    if not should_abort_stream:
                        active_stream_loop = loop
                        active_stream_task = stream_task
                        active_stream_session_id = session_id

                if should_abort_stream:
                    stream_task.cancel()
                    try:
                        loop.run_until_complete(
                            asyncio.gather(stream_task, return_exceptions=True)
                        )
                    except Exception:
                        pass
                    return False

                loop.run_until_complete(
                    asyncio.wait_for(stream_task, timeout=NETWORK_TIMEOUT_SECONDS * 8)
                )

                if proc.stdin:
                    proc.stdin.close()
                duration = time.time() - start_time
                log(f"<- 音频流写入完成 ({bytes_written / 1024:.1f} KB, 耗时 {duration:.2f}s)", "NET")
                return first_chunk_time is not None
            except asyncio.CancelledError:
                log("TTS 流已取消", "INFO")
                return False
            except Exception as e:
                hint = explain_network_error(e)
                if hint:
                    log(hint, "HINT")

                terminate_process(proc)

                if first_chunk_time is not None:
                    log(f"!! 流式播放中断: {e}", "ERR")
                    raise

                if attempt < MAX_RETRIES - 1:
                    retry_delay = RETRY_BASE_DELAY * (attempt + 1)
                    log(
                        f"!! 音频首包前失败: {e}. {retry_delay:.1f}s后重试 ({attempt+1}/{MAX_RETRIES})",
                        "WARN",
                    )
                    if not wait_for_retry_or_stop(session_id, retry_delay):
                        return False
                else:
                    log(f"!! 音频流最终失败: {e}", "ERR")
                    raise
    finally:
        with lock:
            if active_stream_loop is loop:
                active_stream_loop = None
                active_stream_task = None
                active_stream_session_id = None
        try:
            if loop.is_running():
                loop.stop()
            if not loop.is_closed():
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(asyncio.sleep(0.1))
                loop.close()
        except Exception:
            pass


def play_text(text, session_id, source="TEXT"):
    """朗读指定文本。"""
    global is_playing, ffplay_process, start_press_time, current_audio_file

    try:
        if not text or not text.strip():
            log("朗读内容为空", "WARN")
            with lock: is_playing = False
            return
            
        preview = text[:30].replace('\n', ' ')
        log(f"捕获任务({source}): [{preview}...]", "DATA")

        text = normalize_text(text)
        if not text:
            stop_playback(session_id=session_id)
            return
        
        if not is_session_active(session_id):
            log("检测到停止信号，取消启动", "INFO")
            return

        started = stream_audio_to_ffplay(text, session_id)
        if not started:
            stop_playback(session_id=session_id)
            return

        log("音频流已发送完毕，等待播放器自然结束", "INFO")
        
        while True:
            current_session_id, playing, current_proc, _ = get_session_state()
            if not playing or current_session_id != session_id:
                break
            
            if current_proc and current_proc.poll() is not None:
                log(f"播放器进程结束 (Exit Code: {current_proc.returncode})", "INFO")
                break
            time.sleep(0.2)

        log("播放结束", "INFO")
        
        current_session_id, still_playing, current_proc, _ = get_session_state()
        
        if current_proc and still_playing and current_session_id == session_id:
            try:
                current_proc.wait(timeout=3)
            except: pass

    except Exception as e:
        if is_expected_tts_error(e):
            hint = explain_network_error(e)
            if hint:
                log(hint, "HINT")
            log(f"本次朗读失败: {e}", "ERR")
        else:
            log(f"主线程异常: {e}", "FATAL")
            traceback.print_exc()
    finally:
        log("开始资源回收...", "CLEAN")
        stop_playback(session_id=session_id)

        del text
        
        log_memory_stats()
        log("会话结束", "INFO")


def play_clipboard(session_id):
    """消费者 (极速响应版)"""
    try:
        text = read_clipboard_text()
    except RuntimeError as exc:
        log(f"读取剪贴板失败: {exc}", "ERR")
        if sys.platform.startswith("linux"):
            log(get_linux_clipboard_hint(), "HINT")
        with lock:
            global is_playing
            is_playing = False
        return

    play_text(text, session_id, source="CLIPBOARD")


def on_hotkey():
    global is_playing, start_press_time, playback_session_id, last_hotkey_time

    should_stop = False
    session_id = None
    now = time.monotonic()

    with lock:
        if now - last_hotkey_time < HOTKEY_DEBOUNCE_SECONDS:
            log("忽略过近的重复热键", "USER")
            return
        last_hotkey_time = now

        if is_playing:
            should_stop = True
        else:
            start_press_time = time.time()
            playback_session_id += 1
            session_id = playback_session_id
            is_playing = True

    if should_stop:
        log(">> 用户触发停止 <<", "USER")
        threading.Thread(target=stop_playback, daemon=True).start()
        return

    log(f">> 用户触发朗读 << (会话 {session_id})", "USER")
    threading.Thread(target=play_clipboard, args=(session_id,), daemon=True).start()


def bind_hotkey(force=False):
    global hotkey_handler, hotkey_registered_at

    if keyboard is None:
        raise RuntimeError("未安装 keyboard 依赖，无法注册 fallback 热键")

    with lock:
        now = time.monotonic()
        if (
            not force
            and hotkey_handler is not None
            and now - hotkey_registered_at < HOTKEY_REBIND_INTERVAL_SECONDS
        ):
            return

        previous_handler = hotkey_handler
        hotkey_handler = None

    if previous_handler is not None:
        try:
            keyboard.remove_hotkey(previous_handler)
        except Exception:
            pass

    handler = keyboard.add_hotkey(HOTKEY, on_hotkey)

    with lock:
        hotkey_handler = handler
        hotkey_registered_at = time.monotonic()

    action = "重绑" if previous_handler is not None else "注册"
    log(f"热键已{action}: {HOTKEY}", "INIT")


def hotkey_watchdog():
    while True:
        time.sleep(HOTKEY_REBIND_INTERVAL_SECONDS)
        try:
            with lock:
                playing = is_playing

            if playing:
                log("播放中，跳过本次热键重绑", "INIT")
                continue

            bind_hotkey(force=True)
        except Exception as exc:
            log(f"热键保活失败: {exc}", "WARN")


def run_windows_hotkey_loop():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.RegisterHotKey.argtypes = [
        wintypes.HWND,
        ctypes.c_int,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UnregisterHotKey.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.GetMessageW.restype = ctypes.c_int

    modifiers = MOD_ALT | MOD_NOREPEAT
    if not user32.RegisterHotKey(None, WINDOWS_HOTKEY_ID, modifiers, VK_C):
        raise ctypes.WinError(ctypes.get_last_error())

    log(f"Windows 原生热键已注册: {HOTKEY}", "INIT")
    msg = wintypes.MSG()

    try:
        while True:
            result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result == 0:
                break
            if result == -1:
                raise ctypes.WinError(ctypes.get_last_error())
            if msg.message == WM_HOTKEY and msg.wParam == WINDOWS_HOTKEY_ID:
                on_hotkey()
    finally:
        user32.UnregisterHotKey(None, WINDOWS_HOTKEY_ID)
        log(f"Windows 原生热键已注销: {HOTKEY}", "CLEAN")


def run_keyboard_hotkey_loop():
    bind_hotkey(force=True)
    threading.Thread(target=hotkey_watchdog, daemon=True).start()
    keyboard.wait()


def run_stdin_control_loop():
    log("已进入终端控制模式: 回车朗读/停止，输入 q 后回车退出。", "INIT")
    while True:
        try:
            command = input().strip().lower()
        except EOFError:
            break

        if command in ("q", "quit", "exit"):
            stop_playback()
            break

        on_hotkey()


def run_portable_hotkey_loop():
    try:
        run_keyboard_hotkey_loop()
    except Exception as exc:
        log(f"全局热键不可用，切换到终端控制模式: {exc}", "WARN")
        if sys.platform.startswith("linux"):
            log("Linux 全局热键可能需要 root/input 组权限；不提权时可以使用终端回车触发。", "HINT")
        run_stdin_control_loop()


def run_once():
    if stop_toggle_process_if_running():
        return

    register_toggle_process()
    on_hotkey()
    try:
        while True:
            with lock:
                playing = is_playing
            if not playing:
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        stop_playback()
    finally:
        unregister_toggle_process()


def run_text_once(text):
    global is_playing, start_press_time, playback_session_id

    if stop_toggle_process_if_running():
        return

    register_toggle_process()
    start_press_time = time.time()
    try:
        with lock:
            playback_session_id += 1
            session_id = playback_session_id
            is_playing = True

        play_text(text, session_id, source="ARG")
    finally:
        unregister_toggle_process()


def handle_shutdown_signal(signum, _frame):
    log(f"收到退出信号: {signum}", "CLEAN")
    stop_playback()
    unregister_toggle_process()
    sys.exit(0)


def parse_args():
    parser = argparse.ArgumentParser(description="朗读当前剪贴板内容")
    parser.add_argument(
        "text",
        nargs="*",
        help="直接朗读传入的文本；多个参数会用空格拼接",
    )
    parser.add_argument(
        "--text-stdin",
        action="store_true",
        help="从标准输入读取文本并朗读一次",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只朗读一次当前剪贴板内容，不注册热键",
    )
    parser.add_argument(
        "--stdin-control",
        action="store_true",
        help="使用终端回车控制朗读/停止，不注册全局热键；Linux 默认就是这个模式",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    is_one_shot_mode = args.text_stdin or args.text or args.once

    if not is_one_shot_mode and not check_singleton():
        print("!! 程序已在运行中 !!")
        sys.exit(0)

    log(f"=== ClipSpeak Pro (Low Latency) ===", "INIT")
    log(f"PID={os.getpid()} | Python {sys.version.split()[0]}", "INIT")
    log(f"edge-tts={EDGE_TTS_VERSION}", "INIT")
    try:
        check_runtime_dependencies()
    except RuntimeError:
        sys.exit(1)
    
    try:
        if args.text_stdin:
            run_text_once(sys.stdin.read())
        elif args.text:
            run_text_once(" ".join(args.text))
        elif args.once:
            run_once()
        elif sys.platform == "win32" and not args.stdin_control:
            run_windows_hotkey_loop()
        else:
            run_stdin_control_loop()
    except KeyboardInterrupt:
        stop_playback()

if __name__ == "__main__":
    main()
