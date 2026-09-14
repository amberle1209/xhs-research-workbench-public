# v0.1.13 · 视频笔记与本地转录

[下载 macOS 安装包](https://github.com/amberle1209/xhs-research-workbench-public/releases/download/v0.1.13/XHS-Research-Workbench-macOS-0.1.13.zip)。请下载这个 ZIP，解压后阅读 `Start Here.txt`；GitHub 自动生成的 Source code ZIP 不是安装包。

首次安装需要 Google Chrome 和 [`uv`](https://docs.astral.sh/uv/getting-started/installation/)。在终端输入 `cd `，把解压后的整个文件夹拖进终端，按回车。确认报告保存位置后运行：

```bash
REPORTS_FOLDER="$HOME/Desktop/小红书研究报告"
uv tool install --force './xhs_research_workbench-0.1.13-py3-none-any.whl[video]'
export PATH="$HOME/.local/bin:$PATH"
mkdir -p "$REPORTS_FOLDER"
xhs-workbench extension-install --output "$REPORTS_FOLDER"
xhs-workbench extension-status
```

看到 `"status":"ready"` 后，打开 Chrome 的 `chrome://extensions`，开启开发者模式，点击「加载已解压的扩展程序」。在文件选择窗口按 **Command + Shift + G**，输入 `~/.local/share/xhs-workbench/chrome-extension` 并选择该文件夹。确认版本 **0.1.13**，然后在小红书单篇帖子页点击扩展中的「提取当前帖子」，完成后点击「打开本地报告」。

这版更新：

- 页面提供视频来源时，会保存到本地；单个视频上限 **100 MiB**。
- 可在后台尝试转录最长 **15 分钟**的普通话音频。基础报告先生成，完成后刷新报告查看文字稿；若页面提供独立字幕，也会另存。
- 报告中的发布日期按北京时间显示为 `YYYY-MM-DD`，例如 `2026-09-14`。
- 首次转录需联网下载约 **465 MB** 模型，之后用本机 CPU 处理；不需要 API Key 或付费转录账户。
- 每次常规处理最多等待 **10 分钟**。等待时保持 Chrome 运行、电脑不休眠；视频来源缺失或转录失败时，基础报告仍可使用。自动识别的文字稿请先校对。

旧版用户请继续使用原来的报告文件夹运行上述命令，再在 `chrome://extensions` 重新加载并核对版本。若以前从下载目录加载扩展，请改从上面的固定目录加载；已有报告不需要移动或删除。
