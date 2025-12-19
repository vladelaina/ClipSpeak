"""打包 clipboard_reader 为 exe"""
import PyInstaller.__main__
import os

# ffplay 路径
ffplay_path = os.path.expanduser(r"~\scoop\apps\ffmpeg\current\bin\ffplay.exe")

PyInstaller.__main__.run([
    'clipboard_reader.py',
    '--onefile',
    '--noconsole',
    '--name=ClipboardReader',
    f'--add-binary={ffplay_path};.',
    '--hidden-import=edge_tts',
    '--hidden-import=aiohttp',
    '--clean',
])
