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

import edge_tts
import keyboard
import pyperclip
from aiohttp import client_exceptions

try:
    EDGE_TTS_VERSION = edge_tts.__version__
except AttributeError:
    EDGE_TTS_VERSION = "unknown"


def get_ffplay_path():
    """获取 ffplay 路径"""
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
        return os.path.join(base_path, "ffplay.exe")
    else:
        return "ffplay"


# --- 核心配置 ---
VOICE = "zh-CN-XiaoxiaoNeural"
RATE = "+0%"
SPEED = 3.0
NETWORK_TIMEOUT_SECONDS = 15
MAX_RETRIES = 6
RETRY_BASE_DELAY = 1.2

lock = threading.Lock()
is_playing = False
ffplay_process = None
start_press_time = None # 记录按下快捷键的时间
playback_session_id = 0
current_audio_file = None

RE_SPLIT = re.compile(r'([。！？；!?;])')


def log(msg, level="INFO"):
    """带时间戳的日志输出"""
    timestamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{timestamp}] [{level}] {msg}")


def build_atempo_filter(speed):
    """ffmpeg atempo 单次仅支持 0.5-2.0，超出时需要串联。"""
    filters = []
    remaining = speed

    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0

    filters.append(f"atempo={remaining:.3f}".rstrip("0").rstrip("."))
    return ",".join(filters)


def get_session_state():
    with lock:
        return playback_session_id, is_playing, ffplay_process, current_audio_file


def is_session_active(session_id):
    with lock:
        return is_playing and playback_session_id == session_id


def log_memory_stats():
    """内存健康度检查"""
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
        (
            client_exceptions.ClientError,
            asyncio.TimeoutError,
            TimeoutError,
            ConnectionError,
            OSError,
        ),
    )


def check_singleton():
    """单例检查"""
    try:
        lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lock_socket.bind(('127.0.0.1', 29921)) 
        return lock_socket
    except socket.error as e:
        print(f"Socket bind error: {e}")
        return None


def stop_playback(clear_flags=True, session_id=None):
    """停止播放并执行深度清理"""
    global is_playing, ffplay_process, current_audio_file
    
    with lock:
        if session_id is not None and playback_session_id != session_id:
            return
        if clear_flags:
            if is_playing:
                log("状态机变更: Running -> Stopped", "STATE")
            is_playing = False
        proc = ffplay_process
        audio_file = current_audio_file
        ffplay_process = None
        current_audio_file = None
    
    if proc:
        proc_returncode = proc.poll()
        if proc_returncode is None:
            log(f"终止播放器进程 (PID: {proc.pid})", "CLEAN")
        else:
            log(f"播放器进程已退出 (PID: {proc.pid}, Exit Code: {proc_returncode})", "CLEAN")
        try:
            proc.stdin.close()
        except: pass
        if proc_returncode is None:
            try:
                proc.terminate()
                proc.wait(timeout=1)
            except:
                try:
                    proc.kill()
                except: pass
    
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/F", "/IM", "ffplay.exe"], 
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except: pass

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
    if abs(SPEED - 1.0) > 0.001:
        ffplay_args.extend(["-af", build_atempo_filter(SPEED)])

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
    global ffplay_process
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
                    if playback_session_id != session_id or not is_playing:
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                        return False
                    ffplay_process = proc

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

                loop.run_until_complete(
                    asyncio.wait_for(pump_stream(), timeout=NETWORK_TIMEOUT_SECONDS * 8)
                )

                if proc.stdin:
                    proc.stdin.close()
                duration = time.time() - start_time
                log(f"<- 音频流写入完成 ({bytes_written / 1024:.1f} KB, 耗时 {duration:.2f}s)", "NET")
                return first_chunk_time is not None
            except Exception as e:
                hint = explain_network_error(e)
                if hint:
                    log(hint, "HINT")

                if proc:
                    try:
                        if proc.stdin:
                            proc.stdin.close()
                    except Exception:
                        pass
                    try:
                        proc.terminate()
                        proc.wait(timeout=1)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass

                if first_chunk_time is not None:
                    log(f"!! 流式播放中断: {e}", "ERR")
                    raise

                if attempt < MAX_RETRIES - 1:
                    retry_delay = RETRY_BASE_DELAY * (attempt + 1)
                    log(
                        f"!! 音频首包前失败: {e}. {retry_delay:.1f}s后重试 ({attempt+1}/{MAX_RETRIES})",
                        "WARN",
                    )
                    time.sleep(retry_delay)
                else:
                    log(f"!! 音频流最终失败: {e}", "ERR")
                    raise
    finally:
        try:
            if loop.is_running():
                loop.stop()
            if not loop.is_closed():
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(asyncio.sleep(0.1))
                loop.close()
        except Exception:
            pass


def play_clipboard(session_id):
    """消费者 (极速响应版)"""
    global is_playing, ffplay_process, start_press_time, current_audio_file
    
    text = None
    
    try:
        text = pyperclip.paste()
        if not text or not text.strip():
            log("剪贴板内容为空", "WARN")
            with lock: is_playing = False
            return
            
        preview = text[:30].replace('\n', ' ')
        log(f"捕获任务: [{preview}...]", "DATA")

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
        gc.collect()
        
        log_memory_stats()
        log("会话结束", "INFO")


def on_hotkey():
    global is_playing, start_press_time, playback_session_id
    
    with lock:
        playing = is_playing
    
    if playing:
        log(">> 用户触发停止 <<", "USER")
        threading.Thread(target=stop_playback, daemon=True).start()
    else:
        # 记录按下时间
        start_press_time = time.time()
        with lock:
            playback_session_id += 1
            session_id = playback_session_id
            is_playing = True
        log(f">> 用户触发朗读 << (会话 {session_id})", "USER")
        threading.Thread(target=play_clipboard, args=(session_id,), daemon=True).start()


def main():
    if not check_singleton():
        print("!! 程序已在运行中 !!")
        sys.exit(0)

    keyboard.add_hotkey('alt+c', on_hotkey)
    
    log(f"=== ClipSpeak Pro (Low Latency) ===", "INIT")
    log(f"PID={os.getpid()} | Python {sys.version.split()[0]}", "INIT")
    log(f"edge-tts={EDGE_TTS_VERSION}", "INIT")
    
    try:
        keyboard.wait()
    except KeyboardInterrupt:
        stop_playback()

if __name__ == "__main__":
    main()
