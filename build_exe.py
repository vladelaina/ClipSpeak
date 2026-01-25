"""打包 clipboard_reader 为 exe"""
import PyInstaller.__main__
import os
import shutil
import sys

# 尝试自动查找 ffplay
ffplay_path = shutil.which("ffplay")
if not ffplay_path:
    # 回退到硬编码路径 (根据你的环境)
    ffplay_path = os.path.expanduser(r"~\scoop\apps\ffmpeg\current\bin\ffplay.exe")

if not os.path.exists(ffplay_path):
    print(f"错误: 找不到 ffplay，请检查路径: {ffplay_path}")
    sys.exit(1)

print(f"使用 ffplay: {ffplay_path}")

PyInstaller.__main__.run([
    'clipboard_reader.py',
    '--onefile',
    '--noconsole',
    '--name=ClipboardReader',
    f'--add-binary={ffplay_path};.',
    # 显式导入异步相关库，防止打包后运行报错
    '--hidden-import=edge_tts',
    '--hidden-import=aiohttp',
    '--hidden-import=asyncio',
    '--hidden-import=queue',
    '--clean',
])