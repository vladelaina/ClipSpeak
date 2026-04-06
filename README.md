# 剪贴板朗读工具

使用微软 Edge TTS 朗读剪贴板内容的小工具。

## 功能

- 快捷键 `Alt+C` 朗读剪贴板内容
- 再次按下停止朗读
- 默认 3 倍速播放（可在代码中调整）
- 流式播放：首批音频返回后立即开始播放
- 自动重试，尽量减少微软 TTS 链路波动带来的失败

## 使用方法

### 直接下载

从 [GitHub Actions](../../actions) 下载最新的 `ClipboardReader.exe`，双击运行即可。

### 从源码运行

```bash
pip install edge-tts keyboard pyperclip
python clipboard_reader.py
```

## 配置

在 `clipboard_reader.py` 中可以修改：

- `VOICE` - 语音，默认 `zh-CN-XiaoxiaoNeural`
- `RATE` - TTS 请求速度，默认 `+0%`
- `SPEED` - ffmpeg 播放端额外加速，默认 `3.0`（默认最终听感约 3 倍速）
- `MAX_RETRIES` - 网络失败时的最大重试次数

## 本地打包

需要安装 ffmpeg，然后运行：

```bash
pip install pyinstaller
python build_exe.py
```

生成的 exe 在 `dist/ClipboardReader.exe`。
