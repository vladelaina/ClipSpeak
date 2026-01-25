#!/usr/bin/env python3
"""
剪贴板朗读工具 (ClipSpeak Pro Ultimate Debug)
快捷键: Alt+C
- 全链路详细日志追踪
- 针对长时运行优化
- 针对极端文本优化
- 针对网络波动优化
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

import edge_tts
import keyboard
import pyperclip


def get_ffplay_path():
    """获取 ffplay 路径，优先使用系统路径，打包后使用内置路径"""
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
        return os.path.join(base_path, "ffplay.exe")
    else:
        return "ffplay"


# --- 核心配置 ---
VOICE = "zh-CN-XiaoxiaoNeural"
RATE = "+100%"  
SPEED = 1.5     
CHUNK_MIN_SIZE = 300  # 最小聚合
CHUNK_MAX_SIZE = 800  # 最大切分
HARD_LIMIT_SIZE = 1000 # 强制切分阈值

# 全局状态
lock = threading.Lock()
is_playing = False
ffplay_process = None

# 预编译正则
RE_SPLIT = re.compile(r'([。！？；!?;])')


def log(msg):
    """带时间戳的日志输出"""
    timestamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3] # 精确到毫秒
    print(f"[{timestamp}] {msg}")


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
    
    # 1. 标志位管理
    with lock:
        if clear_flags:
            if is_playing:
                log("状态变更: is_playing -> False")
            is_playing = False
        proc = ffplay_process
        ffplay_process = None
    
    # 2. 进程清理
    if proc:
        log(f"正在停止播放器进程 (PID: {proc.pid})...")
        try:
            proc.stdin.close()
        except:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=1)
            log("播放器进程已正常终止")
        except:
            try:
                proc.kill()
                log("播放器进程被强制 Kill")
            except Exception as e:
                log(f"无法终止进程: {e}")
    
    # 3. 兜底清理
    if sys.platform == "win32":
        try:
            # 仅在真的有残留时才可能起作用，平时静默
            subprocess.run(["taskkill", "/F", "/IM", "ffplay.exe"], 
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def split_text_smart_v3(text):
    """V3 分块算法 (带详细日志)"""
    log("正在进行文本分块分析...")
    text = text.replace("#", "").replace("*", "").replace("\r", "")
    if not text.strip():
        return []
    
    raw_lines = text.split('\n')
    chunks = []
    buffer = ""
    
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
            
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
            # 长难句处理
            log(f"DEBUG: 发现长难句 ({len(line)} 字符)，执行标点切分...")
            sub_parts = RE_SPLIT.split(line)
            sub_buffer = ""
            for part in sub_parts:
                if len(part) > HARD_LIMIT_SIZE:
                    log(f"DEBUG: 触发强制硬切分 (片段长度 {len(part)} > {HARD_LIMIT_SIZE})")
                    if sub_buffer:
                        chunks.append(sub_buffer)
                        sub_buffer = ""
                    for k in range(0, len(part), HARD_LIMIT_SIZE):
                        chunks.append(part[k:k+HARD_LIMIT_SIZE])
                elif len(sub_buffer) + len(part) > CHUNK_MAX_SIZE:
                    if sub_buffer:
                        chunks.append(sub_buffer)
                    sub_buffer = part
                else:
                    sub_buffer += part
            
            if sub_buffer:
                chunks.append(sub_buffer)
    
    if buffer:
        chunks.append(buffer)
        
    log(f"分块完成: 共 {len(chunks)} 个片段")
    return chunks


def audio_producer(text_chunks, data_queue):
    """生产者：下载音频 (详细日志版)"""
    global is_playing
    total_chunks = len(text_chunks)
    log(f"生产者线程启动 (任务数: {total_chunks})")
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        for i, chunk_text in enumerate(text_chunks):
            with lock:
                if not is_playing: 
                    log("生产者检测到停止信号，退出循环")
                    break
            
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    log(f"-> [下载] 第 {i+1}/{total_chunks} 段启动 ({len(chunk_text)} 字符)...")
                    start_time = time.time()
                    
                    communicate = edge_tts.Communicate(chunk_text, VOICE, rate=RATE)
                    async_gen = communicate.stream()
                    iterator = async_gen.__aiter__()
                    
                    chunk_count = 0
                    bytes_total = 0
                    
                    while True:
                        with lock:
                            if not is_playing: break
                        
                        try:
                            # 10s 网络超时熔断
                            chunk = loop.run_until_complete(
                                asyncio.wait_for(iterator.__anext__(), timeout=10)
                            )
                            if chunk["type"] == "audio":
                                # put 使用超时
                                while is_playing:
                                    try:
                                        data_queue.put(chunk["data"], timeout=1)
                                        chunk_count += 1
                                        bytes_total += len(chunk["data"])
                                        break
                                    except queue.Full:
                                        # log("DEBUG: 队列已满，生产者等待中...")
                                        continue
                            elif chunk["type"] == "error":
                                raise Exception(f"TTS Error: {chunk['message']}")
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            raise Exception("Network Timeout (10s)")
                    
                    duration = time.time() - start_time
                    log(f"<- [下载] 第 {i+1} 段完成: {bytes_total} bytes, 耗时 {duration:.2f}s")
                    break # 成功
                    
                except Exception as e:
                    with lock:
                        if not is_playing: break
                    
                    if attempt < max_retries - 1:
                        log(f"!! [警告] 网络异常: {e}. 1秒后重试 ({attempt+1}/{max_retries})...")
                        time.sleep(1.0)
                    else:
                        log(f"!! [错误] 第 {i+1} 段下载最终失败: {e}")
            
    except Exception as e:
        log(f"!! 生产者线程发生未捕获异常: {e}")
        traceback.print_exc()
    finally:
        try:
            loop.close()
        except:
            pass
        try:
            data_queue.put(None, timeout=2)
        except:
            pass
        log("生产者线程结束")


def play_clipboard():
    """消费者：音频播放 (详细日志版)"""
    global is_playing, ffplay_process
    
    text = None
    text_chunks = None
    data_queue = None
    producer_thread = None
    
    try:
        text = pyperclip.paste()
        if not text or not text.strip():
            log("错误: 剪贴板为空")
            with lock: is_playing = False
            return
        
        # 预览日志
        preview = text[:50].replace('\n', ' ')
        log(f"剪贴板内容预览: [{preview}...] (总长: {len(text)})")
            
        # 启动前清理
        stop_playback(clear_flags=False)
        
        # 1. 文本处理
        text_chunks = split_text_smart_v3(text)
        if not text_chunks:
            log("错误: 文本分块后为空")
            with lock: is_playing = False
            return

        # 2. 启动播放器
        # 增加 probe size 防止奇怪格式导致 probe 失败
        ffplay_cmd = [
            get_ffplay_path(), "-nodisp", "-autoexit", "-i", "pipe:0",
            "-af", f"atempo={SPEED}" if SPEED < 2.0 else "atempo=2.0,atempo={SPEED/2}", # 简单处理filter
            "-probesize", "4096", "-analyzeduration", "0", 
            "-loglevel", "error"
        ]
        
        # 重新构建 filter string (更严谨)
        atempo_filters = []
        speed_temp = SPEED
        while speed_temp > 2.0:
            atempo_filters.append("atempo=2.0")
            speed_temp /= 2.0
        if speed_temp != 1.0:
            atempo_filters.append(f"atempo={speed_temp}")
        filter_str = ",".join(atempo_filters) if atempo_filters else "anull"
        ffplay_cmd[6] = filter_str # 替换上面简单写的 filter
        
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc = subprocess.Popen(ffplay_cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=sys.stderr, creationflags=creationflags)
        
        with lock:
            ffplay_process = proc
        
        log(f"FFplay 播放器已启动 (PID: {proc.pid})")

        # 3. 启动生产者
        data_queue = queue.Queue(maxsize=100)
        producer_thread = threading.Thread(
            target=audio_producer, 
            args=(text_chunks, data_queue), 
            daemon=True
        )
        producer_thread.start()
        
        # 4. 消费者循环
        log("开始进入播放循环...")
        byte_count = 0
        chunk_idx = 0
        wait_log_printed = False
        
        while True:
            with lock:
                if not is_playing: break
                current_proc = ffplay_process
            
            if current_proc and current_proc.poll() is not None:
                log(f"警告: 播放器进程意外退出 (Code: {current_proc.returncode})")
                break
                
            try:
                # 获取数据
                chunk_data = data_queue.get(timeout=0.5)
                
                if chunk_data is None:
                    log("收到数据流结束标志 (None)")
                    break
                
                wait_log_printed = False # 重置等待日志标志
                
                try:
                    current_proc.stdin.write(chunk_data)
                    byte_count += len(chunk_data)
                    chunk_idx += 1
                    
                    # 适度打日志，防止刷屏
                    if chunk_idx % 10 == 0:
                        log(f"播放进度: 已写入 {chunk_idx} 个数据包 (累计 {byte_count/1024:.1f} KB)")
                        
                except Exception as e:
                    log(f"写入播放器失败: {e}")
                    break
                    
            except queue.Empty:
                if not producer_thread.is_alive():
                    log("队列为空且生产者已结束，停止播放")
                    break
                
                if not wait_log_printed:
                    log("缓冲中... (等待网络数据)")
                    wait_log_printed = True
                continue

        # 5. 结尾处理
        log(f"数据传输结束，等待播放完毕 (总流量: {byte_count/1024:.2f} KB)")
        with lock:
            current_proc = ffplay_process
            still_playing = is_playing
        
        if current_proc and still_playing:
            try:
                current_proc.stdin.close()
                current_proc.wait(timeout=3)
            except:
                pass

    except Exception as e:
        log(f"消费者线程致命错误: {e}")
        traceback.print_exc()
    finally:
        log("执行最终资源回收...")
        stop_playback()
        
        if data_queue:
            with data_queue.mutex: data_queue.queue.clear()
        
        del text, text_chunks, data_queue, producer_thread
        gc.collect()
        
        active_count = threading.active_count()
        log(f"任务完全结束 (当前活动线程: {active_count})")


def on_hotkey():
    global is_playing
    with lock: playing = is_playing
    
    if playing:
        log("快捷键触发: 停止")
        stop_playback()
    else:
        log("快捷键触发: 开始")
        with lock: is_playing = True
        threading.Thread(target=play_clipboard, daemon=True).start()


def main():
    if not check_singleton():
        print("错误: 程序已在运行中 (端口 45678 被占用)")
        sys.exit(0)

    keyboard.add_hotkey('alt+c', on_hotkey)
    log("=== ClipSpeak Pro Ultimate Debug Edition ===")
    log("系统就绪。请按 Alt+C 朗读剪贴板内容。")
    log("按 Ctrl+C 退出程序。")
    
    try:
        keyboard.wait()
    except KeyboardInterrupt:
        log("收到退出信号，正在关闭...")
        stop_playback()

if __name__ == "__main__":
    main()