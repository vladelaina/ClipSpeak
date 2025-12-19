#!/usr/bin/env python3
"""
剪贴板朗读工具
快捷键: Ctrl+Alt+R
- 按下时朗读剪贴板内容
- 再次按下取消朗读
"""

import threading
import subprocess
import gc
import sys
import os

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


def stop_playback():
    """停止播放"""
    global is_playing, ffplay_process
    with lock:
        is_playing = False
        proc = ffplay_process
        ffplay_process = None
    
    if proc:
        try:
            proc.stdin.close()
        except:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=1)
        except:
            try:
                proc.kill()
            except:
                pass


def play_clipboard():
    """流式朗读剪贴板内容"""
    global is_playing, ffplay_process
    
    text = pyperclip.paste()
    if not text or not text.strip():
        print("剪贴板为空")
        with lock:
            is_playing = False
        return
    
    # 去掉不需要朗读的字符
    text = text.replace("#", "").replace("*", "")
    if not text.strip():
        print("剪贴板内容过滤后为空")
        with lock:
            is_playing = False
        return
    
    print(f"正在朗读: {text[:50]}..." if len(text) > 50 else f"正在朗读: {text}")
    
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
        
        # 启动 ffplay
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc = subprocess.Popen([
            get_ffplay_path(), "-nodisp", "-autoexit", "-i", "pipe:0",
            "-af", filter_str,
            "-loglevel", "error"
        ], stdin=subprocess.PIPE, creationflags=creationflags)
        
        with lock:
            ffplay_process = proc
        
        # 流式生成并发送给 ffplay
        communicate = edge_tts.Communicate(text, VOICE, rate=RATE)
        for chunk in communicate.stream_sync():
            with lock:
                if not is_playing:
                    break
                current_proc = ffplay_process
            
            if chunk["type"] == "audio" and current_proc:
                try:
                    current_proc.stdin.write(chunk["data"])
                except:
                    break
        
        # 关闭输入流，等待播放完成
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
                    current_proc.wait()
                except:
                    pass
            
    except Exception as e:
        print(f"错误: {e}")
    finally:
        with lock:
            is_playing = False
            ffplay_process = None
        gc.collect()


def on_hotkey():
    """快捷键回调"""
    global is_playing
    
    with lock:
        playing = is_playing
    
    if playing:
        print("停止朗读")
        stop_playback()
    else:
        print("开始朗读")
        with lock:
            is_playing = True
        thread = threading.Thread(target=play_clipboard, daemon=True)
        thread.start()


def main():
    print("剪贴板朗读工具已启动")
    print("快捷键: Ctrl+Alt+R")
    print("按 Ctrl+C 退出")
    print("-" * 30)
    
    keyboard.add_hotkey('ctrl+alt+r', on_hotkey)
    
    try:
        keyboard.wait()
    except KeyboardInterrupt:
        print("\n已退出")
        stop_playback()


if __name__ == "__main__":
    main()
