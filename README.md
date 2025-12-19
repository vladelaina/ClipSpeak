# 剪贴板朗读工具

使用微软 Edge TTS 朗读剪贴板内容的小工具。

## 功能

- 快捷键 `Ctrl+Alt+R` 朗读剪贴板内容
- 再次按下停止朗读
- 3倍速朗读（可在代码中调整）
- 流式处理，长文本也能快速开始播放

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
- `RATE` - TTS 速度，默认 `+100%`（2倍速）
- `SPEED` - ffmpeg 额外加速，默认 `1.5`（最终3倍速）

## 本地打包

需要安装 ffmpeg，然后运行：

```bash
pip install pyinstaller
python build_exe.py
```

生成的 exe 在 `dist/ClipboardReader.exe`。
