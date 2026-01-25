#!/usr/bin/env python3
"""
剪贴板朗读工具
快捷键: Alt+C
- 按下时朗读剪贴板内容
- 再次按下取消朗读
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
import time # 新增：用于重试延迟

import edge_tts
import keyboard
import pyperclip


def get_ffplay_path():
    """获取 ffplay 路径，支持打包后的 exe"""
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
        return os.path.join(base_path, "ffplay.exe")
    else:
        return "ffplay"


VOICE = "zh-CN-XiaoxiaoNeural"
RATE = "+100%"  # TTS 2倍速
SPEED = 1.5  # ffmpeg 额外加速，最终速度 = 2 * 1.5 = 3倍

# 全局状态
lock = threading.Lock()
is_playing = False
ffplay_process = None


def log(msg):
    """带时间戳的日志输出"""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")


def check_singleton():
    """确保程序只运行一个实例"""
    try:
        # 尝试绑定一个本地特定端口
        # 如果端口被占用，说明已经有一个实例在运行了
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
            is_playing = False
        proc = ffplay_process
        ffplay_process = None
    
    if proc:
        log("正在停止播放器进程...")
        try:
            proc.stdin.close()
        except:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception as e:
            log(f"进程终止异常，尝试强制关闭: {e}")
            try:
                proc.kill()
            except:
                pass
    
    # 针对 Windows 的核弹级清理：确保没有任何 ffplay 残留
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/F", "/IM", "ffplay.exe"], 
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def split_text_smart(text, limit=500):
    """
    智能拆分长文本
    优先按换行符拆分，聚合短行，防止请求过大导致延迟
    """
    lines = text.split('\n')
    buffer = ""
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # 如果加上这行还不超限，就拼接到缓冲
        if len(buffer) + len(line) < limit:
            buffer += line + "\n"
        else:
            # 缓冲满了，先吐出之前的
            if buffer:
                yield buffer
            # 当前行如果本身就超长（比如一整段没换行），也得处理
            if len(line) > limit:
                # 这里简单处理，直接把超长行当做一段（edge-tts能处理长段，但拆分能优化首屏时间）
                # 以后可以优化为按句号拆分
                yield line + "\n"
                buffer = ""
            else:
                buffer = line + "\n"
    
    # 吐出最后剩余的
    if buffer:
        yield buffer


def audio_producer(text_chunks, data_queue):
    """
    后台搬运工线程：负责连续下载音频数据放入队列
    (异步超时控制 + 智能重试版)
    """
    global is_playing
    total_chunks = len(text_chunks)
    
    log(f"后台下载线程启动，共 {total_chunks} 个任务")
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        for i, chunk_text in enumerate(text_chunks):
            log(f"-> [下载] 开始处理第 {i+1}/{total_chunks} 段 (长度: {len(chunk_text)})")
            
            with lock:
                if not is_playing:
                    log("-> [下载] 检测到停止信号，退出")
                    break
            
            # === 重试机制 ===
            max_retries = 3
            success = False
            
            for attempt in range(max_retries):
                try:
                    communicate = edge_tts.Communicate(chunk_text, VOICE, rate=RATE)
                    if attempt > 0:
                        log(f"-> [下载] 第 {attempt+1} 次尝试连接...")
                    else:
                        log(f"-> [下载] 建立连接中 (每包超时限制: 10s)...")
                    
                    async_gen = communicate.stream()
                    iterator = async_gen.__aiter__()
                    
                    chunk_count = 0
                    while True:
                        with lock:
                            if not is_playing:
                                break
                        
                        try:
                            # 强制超时控制
                            chunk = loop.run_until_complete(
                                asyncio.wait_for(iterator.__anext__(), timeout=10)
                            )
                            
                            if chunk["type"] == "audio":
                                data_queue.put(chunk["data"])
                                chunk_count += 1
                            elif chunk["type"] == "error":
                                log(f"!! [下载] TTS 返回错误: {chunk['message']}")
                                raise Exception(f"TTS Error: {chunk['message']}") # 触发重试
                            elif chunk["type"] == "WordBoundary":
                                pass
                                
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            log(f"!! [下载] 严重超时：第 {i+1} 段在 10s 内未收到数据")
                            raise Exception("Timeout") # 触发重试
                        except Exception as e:
                            log(f"!! [下载] 数据流异常: {e}")
                            raise e # 触发重试

                    log(f"<- [下载] 第 {i+1} 段处理完毕，共 {chunk_count} 包")
                    success = True
                    break # 成功，跳出重试循环
                    
                except Exception as e:
                    # 如果是用户主动停止，就不重试了
                    with lock:
                        if not is_playing:
                            break
                    
                    log(f"!! [下载] 连接或传输失败 (尝试 {attempt+1}/{max_retries}): {e}")
                    if attempt < max_retries - 1:
                        time.sleep(1) # 冷却 1 秒
                    else:
                        log(f"!! [下载] 重试耗尽，跳过该段")
                        # traceback.print_exc() # 可选：打印堆栈
            
            with lock:
                if not is_playing:
                    break
                    
    except Exception as e:
        log(f"!! [下载] 线程致命错误: {e}")
        traceback.print_exc()
    finally:
        try:
            loop.close()
        except:
            pass
        data_queue.put(None)
        log("后台下载线程结束，已发送结束哨兵 (None)")


def play_clipboard():
    """流式朗读剪贴板内容（双线程零等待版）"""
    global is_playing, ffplay_process
    
    # 显式定义变量以便 finally 块清理
    text = None
    text_chunks = None
    data_queue = None
    producer_thread = None
    
    text = pyperclip.paste()
    if not text or not text.strip():
        log("剪贴板为空")
        with lock:
            is_playing = False
        return
    
    # 去掉不需要朗读的字符
    text = text.replace("#", "").replace("*", "")
    if not text.strip():
        log("剪贴板内容过滤后为空")
        with lock:
            is_playing = False
        return
    
    log(f"准备朗读，文本总长度: {len(text)} 字符")
    
    # 启动前清理：只清理进程，不重置标志！
    stop_playback(clear_flags=False)

    proc = None
    try:
        # 构建 atempo 滤镜
        atempo_filters = []
        speed = SPEED
        while speed > 2.0:
            atempo_filters.append("atempo=2.0")
            speed /= 2.0
        if speed != 1.0:
            atempo_filters.append(f"atempo={speed}")
        filter_str = ",".join(atempo_filters) if atempo_filters else "anull"
        
        log(f"启动播放器进程 (Speed: {SPEED})...")

        # 启动 ffplay
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc = subprocess.Popen([
            get_ffplay_path(), "-nodisp", "-autoexit", "-i", "pipe:0",
            "-af", filter_str,
            "-loglevel", "warning"
        ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=sys.stderr, creationflags=creationflags)
        
        with lock:
            ffplay_process = proc
        
        # 文本分段
        text_chunks = list(split_text_smart(text))
        
        # 初始化缓冲队列
        data_queue = queue.Queue(maxsize=200)
        
        # 启动后台下载线程（生产者）
        producer_thread = threading.Thread(
            target=audio_producer, 
            args=(text_chunks, data_queue), 
            daemon=True
        )
        producer_thread.start()
        
        log("开始流式播放 (等待队列数据)...")
        socket.setdefaulttimeout(30)

        # 主线程循环（消费者）
        byte_count = 0
        chunk_idx = 0
        while True:
            with lock:
                if not is_playing:
                    break
                current_proc = ffplay_process
            
            if current_proc and current_proc.poll() is not None:
                log(f"警告: ffplay 意外退出 (Exit code: {current_proc.returncode})")
                break
            
            try:
                # 尝试获取数据
                chunk_data = data_queue.get(timeout=1.0)
                
                if chunk_data is None:
                    log("收到结束哨兵 (None)，数据流传输完毕")
                    break
                
                chunk_idx += 1
                # 写入播放器
                try:
                    current_proc.stdin.write(chunk_data)
                    byte_count += len(chunk_data)
                    if chunk_idx % 20 == 0:
                        log(f"已写入 {chunk_idx} 个音频包 (累计 {byte_count / 1024:.1f} KB)")
                except Exception as e:
                    log(f"写入音频数据失败: {e}")
                    break
                    
            except queue.Empty:
                if not producer_thread.is_alive():
                    log("下载线程已死且队列为空，停止等待")
                    break
                # log("等待数据中...") 
                continue

        log(f"所有音频数据写入完毕 (Total: {byte_count / 1024:.1f} KB)")
        
        with lock:
            current_proc = ffplay_process
            still_playing = is_playing
        
        if current_proc:
            try:
                current_proc.stdin.close()
            except:
                pass 
            
            if still_playing:
                try:
                    current_proc.wait(timeout=5)
                except:
                    pass
            
    except Exception as e:
        log(f"严重错误: {e}")
        traceback.print_exc()
    finally:
        # --- 深度清理环节 ---
        log("执行深度清理...")
        stop_playback() 
        
        if data_queue:
            with data_queue.mutex:
                data_queue.queue.clear()
        
        del text, text_chunks, data_queue, producer_thread
        gc.collect()
        
        log(f"清理完毕 (当前存活线程数: {threading.active_count()})")
        log("播放任务结束")


def on_hotkey():
    """快捷键回调"""
    global is_playing
    
    with lock:
        playing = is_playing
    
    if playing:
        log("停止朗读")
        stop_playback()
    else:
        log("开始朗读")
        with lock:
            is_playing = True
        thread = threading.Thread(target=play_clipboard, daemon=True)
        thread.start()


def main():
    lock_socket = check_singleton()
    if not lock_socket:
        print("程序已在后台运行中 (Program is already running).")
        sys.exit(0)

    keyboard.add_hotkey('alt+c', on_hotkey)
    
    log("Clipboard Reader 启动就绪 (按 Alt+C 朗读)")
    log("按 Ctrl+C 退出")
    
    try:
        # 保持程序运行
        keyboard.wait()
    except KeyboardInterrupt:
        log("程序正在退出...")
        stop_playback()

if __name__ == "__main__":
    main()