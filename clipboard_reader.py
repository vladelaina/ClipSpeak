#!/usr/bin/env python3
"""
剪贴板朗读工具 (Low Latency Edition)
快捷键: Alt+C
- 极速启动：零缓冲播放参数
- 性能监控：精确记录首响延迟
"""

import threading
import subprocess
import gc
import sys
import os
import datetime
import socket
import queue
import traceback
import asyncio
import time
import re
import platform

import edge_tts
import keyboard
import pyperclip


def get_ffplay_path():
    """获取 ffplay 路径"""
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
        return os.path.join(base_path, "ffplay.exe")
    else:
        return "ffplay"


# --- 核心配置 ---
VOICE = "zh-CN-XiaoxiaoNeural"
RATE = "+100%"  
SPEED = 1.5     
CHUNK_MIN_SIZE = 300
CHUNK_MAX_SIZE = 800
HARD_LIMIT_SIZE = 1000

lock = threading.Lock()
is_playing = False
ffplay_process = None
start_press_time = None # 记录按下快捷键的时间

RE_SPLIT = re.compile(r'([。！？；!?;])')


def log(msg, level="INFO"):
    """带时间戳的日志输出"""
    timestamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{timestamp}] [{level}] {msg}")


def log_memory_stats():
    """内存健康度检查"""
    gc.collect()
    obj_count = len(gc.get_objects())
    log(f"内存快照: 对象数 {obj_count} | GC: {gc.get_stats()}", "MEM")


def check_singleton():
    """单例检查"""
    try:
        lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lock_socket.bind(('127.0.0.1', 45678)) 
        return lock_socket
    except socket.error:
        return None


def stop_playback(clear_flags=True):
    """停止播放并执行深度清理"""
    global is_playing, ffplay_process
    
    with lock:
        if clear_flags:
            if is_playing:
                log("状态机变更: Running -> Stopped", "STATE")
            is_playing = False
        proc = ffplay_process
        ffplay_process = None
    
    if proc:
        log(f"终止播放器进程 (PID: {proc.pid})", "CLEAN")
        try:
            proc.stdin.close()
        except: pass
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


def split_text_smart_v3(text):
    """分块算法"""
    text = text.replace("#", "").replace("*", "").replace("\r", "")
    if not text.strip(): return []
    
    raw_lines = text.split('\n')
    chunks = []
    buffer = ""
    
    for line in raw_lines:
        line = line.strip()
        if not line: continue
            
        if len(buffer) + len(line) < CHUNK_MIN_SIZE:
            buffer += line + "，" 
            continue
        
        if buffer:
            chunks.append(buffer)
            buffer = ""
            
        if len(line) < CHUNK_MAX_SIZE:
            if len(line) > CHUNK_MIN_SIZE:
                chunks.append(line)
            else:
                buffer = line + "，"
        else:
            log(f"处理长难句 ({len(line)}字符)，执行硬切分", "TEXT")
            sub_parts = RE_SPLIT.split(line)
            sub_buffer = ""
            for part in sub_parts:
                if len(part) > HARD_LIMIT_SIZE:
                    if sub_buffer:
                        chunks.append(sub_buffer)
                        sub_buffer = ""
                    for k in range(0, len(part), HARD_LIMIT_SIZE):
                        chunks.append(part[k:k+HARD_LIMIT_SIZE])
                elif len(sub_buffer) + len(part) > CHUNK_MAX_SIZE:
                    if sub_buffer: chunks.append(sub_buffer)
                    sub_buffer = part
                else:
                    sub_buffer += part
            if sub_buffer: chunks.append(sub_buffer)
    
    if buffer: chunks.append(buffer)
    
    return chunks


def audio_producer(text_chunks, data_queue):
    """生产者 (完整日志版)"""
    global is_playing
    total_chunks = len(text_chunks)
    log(f"下载线程启动 (任务队列: {total_chunks})", "NET")
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        for i, chunk_text in enumerate(text_chunks):
            with lock:
                if not is_playing: break
            
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    start_time = time.time()
                    log(f"-> 开始下载第 {i+1}/{total_chunks} 段 ({len(chunk_text)} 字符)...", "NET")
                    
                    conn_start_time = time.time() # 记录连接开始时间
                    
                    communicate = edge_tts.Communicate(chunk_text, VOICE, rate=RATE)
                    async_gen = communicate.stream()
                    iterator = async_gen.__aiter__()
                    
                    chunk_size_total = 0
                    is_first_chunk = True # 标记是否为首包
                    
                    while True:
                        with lock:
                            if not is_playing: break
                        
                        try:
                            # 10s 超时
                            chunk = loop.run_until_complete(
                                asyncio.wait_for(iterator.__anext__(), timeout=10)
                            )
                            
                            # 记录首包到达时间 (TTFB)
                            if is_first_chunk:
                                ttfb = time.time() - conn_start_time
                                log(f"微软服务器已响应 (首包耗时/TTFB: {ttfb:.2f}s)", "NET")
                                is_first_chunk = False
                            
                            if chunk["type"] == "audio":
                                while is_playing:
                                    try:
                                        data_queue.put(chunk["data"], timeout=1)
                                        chunk_size_total += len(chunk["data"])
                                        break
                                    except queue.Full:
                                        continue
                            elif chunk["type"] == "error":
                                raise Exception(f"TTS Error: {chunk['message']}")
                                
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            raise Exception("Network Timeout (10s)")
                    
                    duration = time.time() - start_time
                    log(f"<- 第 {i+1} 段下载完成 ({chunk_size_total/1024:.1f} KB, 耗时 {duration:.2f}s)", "NET")
                    break 
                    
                except Exception as e:
                    with lock:
                        if not is_playing: break
                    
                    if attempt < max_retries - 1:
                        log(f"!! 网络连接失败: {e}. 1s后重试 ({attempt+1}/{max_retries})", "WARN")
                        time.sleep(1.0)
                    else:
                        log(f"!! 第 {i+1} 段下载最终失败: {e}", "ERR")
            
    except Exception as e:
        log(f"生产者线程崩溃: {e}", "FATAL")
        traceback.print_exc()
    finally:
        try:
            if loop.is_running(): loop.stop()
            if not loop.is_closed():
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(asyncio.sleep(0.250)) 
                loop.close()
        except: pass
        try:
            data_queue.put(None, timeout=2)
        except: pass
        log("生产者线程退出", "NET")


def play_clipboard():
    """消费者 (极速响应版)"""
    global is_playing, ffplay_process, start_press_time
    
    text = None
    text_chunks = None
    data_queue = None
    producer_thread = None
    
    try:
        text = pyperclip.paste()
        if not text or not text.strip():
            log("剪贴板内容为空", "WARN")
            with lock: is_playing = False
            return
            
        preview = text[:30].replace('\n', ' ')
        log(f"捕获任务: [{preview}...]", "DATA")
        
        stop_playback(clear_flags=False)
        
        text_chunks = split_text_smart_v3(text)
        if not text_chunks:
            with lock: is_playing = False
            return
        
        log(f"文本分块完成: 共 {len(text_chunks)} 块", "TEXT")

        # [Final Check] 启动前最后确认
        with lock:
            if not is_playing:
                log("检测到停止信号，取消启动", "INFO")
                return

        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        
        # === 极速启动参数 ===
        # -fflags nobuffer: 禁用输入缓冲
        # -flags low_delay: 启用低延迟模式
        proc = subprocess.Popen([
            get_ffplay_path(), "-nodisp", "-autoexit", 
            "-fflags", "nobuffer", 
            "-flags", "low_delay",
            "-strict", "experimental",
            "-i", "pipe:0",
            "-af", f"atempo={SPEED}" if SPEED < 2.0 else "atempo=2.0,atempo={{SPEED/2}}",
            "-probesize", "4096", "-analyzeduration", "0", 
            "-loglevel", "error"
        ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=sys.stderr, creationflags=creationflags)
        
        with lock:
            ffplay_process = proc
        
        log(f"播放器进程已挂载 (PID: {proc.pid})", "PROC")

        data_queue = queue.Queue(maxsize=100)
        producer_thread = threading.Thread(
            target=audio_producer, 
            args=(text_chunks, data_queue), 
            daemon=True
        )
        producer_thread.start()
        
        log("等待数据流...", "INFO")
        byte_count = 0
        chunk_idx = 0
        first_byte_time = None
        
        while True:
            with lock:
                if not is_playing: break
                current_proc = ffplay_process
            
            if current_proc and current_proc.poll() is not None:
                log(f"播放器意外退出 (Exit Code: {current_proc.returncode})", "ERR")
                break
                
            try:
                chunk_data = data_queue.get(timeout=0.5)
                if chunk_data is None:
                    log("收到 EOF 结束信号", "INFO")
                    break
                
                try:
                    current_proc.stdin.write(chunk_data)
                    byte_count += len(chunk_data)
                    chunk_idx += 1
                    
                    # === 延迟统计 ===
                    if first_byte_time is None:
                        first_byte_time = datetime.datetime.now()
                        latency = 0
                        if start_press_time:
                            latency = time.time() - start_press_time
                        log(f"⚡ 首响延迟: {latency:.2f}秒 (声音开始)", "PERF")
                    
                    if chunk_idx % 10 == 0:
                        log(f"播放中... 已写入 {chunk_idx} 包 ({byte_count/1024:.1f} KB)", "PLAY")
                        
                except Exception as e:
                    log(f"写入管道失败: {e}", "ERR")
                    break
                    
            except queue.Empty:
                if not producer_thread.is_alive():
                    log("数据源已耗尽", "INFO")
                    break
                log("缓冲中... (Waiting for Net)", "WARN")
                continue

        log(f"播放结束 (总流量: {byte_count/1024:.2f} KB)", "INFO")
        
        with lock:
            current_proc = ffplay_process
            still_playing = is_playing
        
        if current_proc and still_playing:
            try:
                current_proc.stdin.close()
                current_proc.wait(timeout=3)
            except: pass

    except Exception as e:
        log(f"主线程异常: {e}", "FATAL")
        traceback.print_exc()
    finally:
        log("开始资源回收...", "CLEAN")
        stop_playback()
        
        if data_queue:
            with data_queue.mutex: data_queue.queue.clear()
        
        del text, text_chunks, data_queue, producer_thread
        gc.collect()
        
        log_memory_stats()
        log("会话结束", "INFO")


def on_hotkey():
    global is_playing, start_press_time
    
    with lock: playing = is_playing
    
    if playing:
        log(">> 用户触发停止 <<", "USER")
        threading.Thread(target=stop_playback, daemon=True).start()
    else:
        # 记录按下时间
        start_press_time = time.time()
        log(">> 用户触发朗读 <<", "USER")
        with lock: is_playing = True
        threading.Thread(target=play_clipboard, daemon=True).start()


def main():
    if not check_singleton():
        print("!! 程序已在运行中 !!")
        sys.exit(0)

    keyboard.add_hotkey('alt+c', on_hotkey)
    
    log(f"=== ClipSpeak Pro (Low Latency) ===", "INIT")
    log(f"PID={os.getpid()} | Python {sys.version.split()[0]}", "INIT")
    
    try:
        keyboard.wait()
    except KeyboardInterrupt:
        stop_playback()

if __name__ == "__main__":
    main()
