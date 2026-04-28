# 剪贴板朗读工具

使用微软 Edge TTS 朗读剪贴板内容的小工具。

## 功能

- 快捷键 `Alt+C` 朗读剪贴板内容
- 再次按下停止朗读
- 默认 3 倍速播放（可在代码中调整）
- 流式播放：首批音频返回后立即开始播放
- 自动重试，尽量减少微软 TTS 链路波动带来的失败

## 使用方法

### 终端运行

```bash
pip install -r requirements.txt
python clipboard_reader.py
```

项目不需要构建，直接用 Python 运行即可。程序会优先使用项目目录里的 `ffplay`，找不到时再使用系统 PATH 中的 `ffplay`。

如果不想让用户全局安装 `ffmpeg`，可以把 `ffplay` 放到下面任一位置：

```text
tools/ffmpeg/ffplay
tools/ffmpeg/bin/ffplay
bin/ffplay
```

如果使用系统安装版：

```bash
sudo apt install ffmpeg
```

程序会优先调用系统里已有的剪贴板命令：

- Wayland: `wl-paste`
- X11: `xclip` 或 `xsel`

只要用户环境里已经有其中一个，就不需要额外安装剪贴板工具。没有时再按桌面环境安装 `wl-clipboard`、`xclip` 或 `xsel`。

Linux 默认使用前台终端控制模式，不依赖 Python 抢全局键：

```bash
python clipboard_reader.py
```

- 回车：朗读 / 停止
- `q` 后回车：退出

Windows 直接运行时会注册 `Alt+C`。如果在 Windows 上也想使用终端控制，可以加：

```bash
python clipboard_reader.py --stdin-control
```

只朗读一次当前剪贴板内容：

```bash
python clipboard_reader.py --once
```

直接朗读传入的文本：

```bash
python clipboard_reader.py "你好，这是一段测试文本"
```

也可以从标准输入读取：

```bash
echo "你好，这是一段测试文本" | python clipboard_reader.py --text-stdin
```

niri 里可以这样绑定固定文本朗读：

```kdl
binds {
    Mod+C {
        spawn "/home/vladelaina/code/clipspeak/.venv/bin/python" "/home/vladelaina/code/clipspeak/clipboard_reader.py" "你好，这是一段测试文本";
    }
}
```

niri 里推荐直接绑定项目内脚本，逻辑集中在项目中，方便备份：

```kdl
binds {
    Alt+C {
        spawn "/home/vladelaina/code/clipspeak/clipspeak-toggle.sh";
    }
}
```

`clipspeak-toggle.sh` 会打开一个右下角浮动 kitty 显示日志；朗读成功结束后自动关闭，报错时保留窗口方便查看。播放中再次触发会停止朗读并关闭窗口。

## 配置

在 `clipboard_reader.py` 中可以修改：

- `VOICE` - 语音，默认 `zh-CN-XiaoxiaoNeural`
- `RATE` - TTS 请求速度，默认 `+0%`
- `SPEED` - ffmpeg 播放端额外加速，默认 `3.0`（默认最终听感约 3 倍速）
- `VOLUME` - 播放端音量倍率，默认 `1.6`（只影响 ClipSpeak，不改系统音量）
- `MAX_RETRIES` - 网络失败时的最大重试次数

ClipSpeak 会在播放器侧做独立音量放大；系统当前输出设备音量仍然照常生效。
