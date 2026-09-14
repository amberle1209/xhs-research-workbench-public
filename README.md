# XHS Research Workbench

一个本地、只读的小红书研究采集工具。你主动选择要采集的帖子后，它会在你的电脑上生成可查看的 HTML 和 JSON 报告。

## 下载 macOS 用户安装包

[**下载 macOS 安装包 v0.1.13**](https://github.com/amberle1209/xhs-research-workbench-public/releases/download/v0.1.13/XHS-Research-Workbench-macOS-0.1.13.zip)

不要点击 GitHub 的 `Code → Download ZIP`，也不要下载 Release 中的 `Source code (zip)` 或 `Source code (tar.gz)`；它们是开发源码，不能直接安装。

## 能做什么

- 在可见的小红书帖子详情页，手动启动采集当前帖子。
- 将帖子信息和页面可用的媒体整理为本地报告。
- 视频保存后，可在后台尝试转录最长 15 分钟的普通话音频，并把文字稿加进报告；页面提供独立字幕时也会另存。
- 报告中的发布日期按北京时间显示为 `YYYY-MM-DD`，例如 `2026-09-14`。
- 不会发布、编辑、点赞、关注、评论或删除小红书内容。

单个视频最多 100 MiB。页面未提供可用来源、视频过大或音频不可读时，报告会说明结果；已保存的视频和基础报告仍可使用。自动识别的文字稿可能有错字，引用前请校对。

## 安装（macOS）

你需要 Google Chrome 和 [`uv`](https://docs.astral.sh/uv/getting-started/installation/)。下载上方的用户安装包后，双击解压。

解压后的文件夹中有三个项目：

- `Start Here.txt`：离线安装说明。
- `xhs_research_workbench-0.1.13-py3-none-any.whl`：由下方终端命令使用，不需要双击。
- `Chrome Extension`：兼容旧安装方式的副本。新版请从下方的固定目录加载扩展。

按下面步骤操作：

1. 打开「终端」。输入 `cd` 后保留一个空格，把刚才解压的整个文件夹拖进终端窗口，再按回车。
2. 先决定报告保存位置。你只需要编辑下方第一行 `REPORTS_FOLDER=...` 双引号内的路径。示例路径可以直接使用；若想保存到其他位置，直接替换双引号内的整段路径即可。

   路径必须是完整路径：使用 `$HOME/` 开头，或 `/Users/你的用户名/` 开头；不能只填写文件夹名称。路径中有空格时，不要在空格前加入反斜杠 `\`，也不要使用中文弯引号 `“ ”`；请保留代码中的英文半角双引号 `"`。例如：`REPORTS_FOLDER="$HOME/Documents/小红书 研究报告"`。`$REPORTS_FOLDER` 是固定变量名，后面的命令必须原样复制；不要写成 `$REPORTS\_FOLDER`。

3. 修改第一行后，选中并一次性复制下面的**完整命令块**到终端运行。不要跳过第一行，也不要单独运行 `mkdir` 或后面的命令：

   ```bash
   REPORTS_FOLDER="$HOME/Desktop/小红书研究报告"
   uv tool install --force './xhs_research_workbench-0.1.13-py3-none-any.whl[video]'
   export PATH="$HOME/.local/bin:$PATH"
   mkdir -p "$REPORTS_FOLDER"
   xhs-workbench extension-install --output "$REPORTS_FOLDER"
   xhs-workbench extension-status
   ```

4. 最后一行应显示 `"status":"ready"`，表示本地安装文件已就绪。
5. 在 Chrome 地址栏打开 `chrome://extensions`，开启右上角的「开发者模式」，点击「加载已解压的扩展程序」。
6. 文件选择窗口打开后，按 **Command + Shift + G**，粘贴 `~/.local/share/xhs-workbench/chrome-extension`，按回车，再选择这个文件夹。请勿选择解压包中的 `Chrome Extension` 副本。
7. 确认扩展卡片显示版本 **0.1.13**，再从工具栏拼图图标打开或固定 **XHS Research Workbench**。

报告会保存到你在 `REPORTS_FOLDER` 中设置的文件夹。请在首次安装前选好这个位置；安装完成后不需要再运行终端命令。

### 每一行命令的作用

- `REPORTS_FOLDER="..."`：指定报告保存位置。只改这一行双引号内的路径；其余命令不要改。
- `uv tool install ...[video]`：安装本地工具及视频转录所需组件。
- `export PATH=...`：让当前终端能找到刚安装的工具。
- `mkdir -p "$REPORTS_FOLDER"`：创建你指定的报告文件夹；已经存在也不会删除其中内容。
- `xhs-workbench extension-install --output "$REPORTS_FOLDER"`：把报告文件夹设为扩展保存报告的位置。
- `xhs-workbench extension-status`：只检查安装是否就绪，不会登录、采集或写入报告。

如果终端显示 `>`，说明引号没有正确结束。按 `Ctrl + C` 退出，再从第 2 步重新复制完整命令块；在 `>` 状态下不要继续输入命令。

### 以后想更换报告保存位置

不要只改 `REPORTS_FOLDER` 后单独重跑安装命令。先在终端运行：

```bash
xhs-workbench extension-uninstall
```

这不会删除已有报告。然后回到第 2 步，修改 `REPORTS_FOLDER` 后重新运行完整命令块。

### 已安装旧版本的用户

仍用原来的报告文件夹填写 `REPORTS_FOLDER`，在新解压包中重新运行上面的完整命令块。随后到 `chrome://extensions` 点击扩展卡片上的「重新加载」，确认版本是 **0.1.13**。如果以前从下载文件夹中的 `Chrome Extension` 加载扩展，请改为加载第 6 步的固定目录，避免 Chrome 继续运行旧文件。原有报告文件夹无需移动或删除。

## 第一次采集

1. 在同一个 Chrome 个人资料中，先自行登录小红书。若页面要求验证码或其他账号验证，请在页面上自行完成后再开始；本工具不会代替你登录或绕过验证。
2. 打开要研究的那一篇小红书帖子详情页。
3. 打开 **XHS Research Workbench**，点击「提取当前帖子」。首次采集如出现 Chrome 权限提示，请允许本次所需的小红书媒体访问权限；若拒绝，采集不会开始。
4. 等待采集完成后，在扩展中点击「打开本地报告」。视频笔记的基础报告会先打开，音频转录在后台继续；可关闭笔记页和弹窗，但请保持 Chrome 运行、电脑不休眠，稍后刷新或重新打开报告查看文字稿。

首次转录需要联网下载约 465 MB 的模型，此后在本机 CPU 上运行，不需要 API Key 或付费转录账户。最长 15 分钟的视频可尝试转录，但每次常规处理最多等待 10 分钟；处理超时不影响已保存的视频和基础报告。

## 安装或使用异常

- `uv` 不可用：先按其[官方说明](https://docs.astral.sh/uv/getting-started/installation/)安装，再重新执行上面的终端命令。
- 最后一行没有显示 `ready`：重新执行上面的终端命令；仍未解决时，请在 [GitHub Issues](https://github.com/amberle1209/xhs-research-workbench-public/issues) 提交完整终端输出和你的 macOS、Chrome 版本。不要提交账号信息、Cookie 或报告内容。
- Chrome 找不到扩展或仍显示旧版：按安装第 6 步从固定目录加载，再确认版本 **0.1.13**；不要继续使用下载文件夹里的旧副本。
- 页面提示未登录或无法采集：确认当前标签页已在小红书完成登录，并停留在单篇帖子详情页。

## 数据与权限

- 只有你点击按钮后才会开始采集。
- 扩展只读取完成当前采集所需的可见页面内容，并把结果保存到你的本地电脑。
- Chrome 扩展的正常采集路径不会读取或导出系统浏览器的 Cookie，也不会把 Cookie 写入报告。
- 安装包还保留供开发者使用的命令行登录功能，它会在本机保存独立登录会话。普通扩展用户不需要使用；存储边界见[开发说明](DEVELOPMENT.md#命令行登录与本机会话)。
- 报告和下载的媒体留在你的电脑上，除非你自行分享。

## 开发

普通使用不需要阅读开发命令。参与开发、测试或通过命令行采集时，请参阅 [开发说明](DEVELOPMENT.md)。
