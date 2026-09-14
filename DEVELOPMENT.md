# 开发说明

本文件仅面向参与开发、测试或通过命令行执行采集的贡献者。

## 环境准备

```bash
uv lock
uv sync --all-groups --extra video
npm --prefix chrome_extension ci
```

## 常用验证

```bash
npm --prefix chrome_extension test
npm --prefix chrome_extension run typecheck
uv run pytest
uv run ruff check .
uv run mypy src
uv build
```

## 构建 macOS 用户下载包

先构建 wheel，再生成用户可直接解压的发布包：

```bash
uv build
python3 scripts/build_macos_user_bundle.py \
  --wheel dist/xhs_research_workbench-0.1.13-py3-none-any.whl \
  --output dist/XHS-Research-Workbench-macOS-0.1.13.zip
```

该压缩包内的 `Chrome Extension` 是兼容副本，必须与 wheel 中的正式扩展构建完全一致；自动测试会验证这一点。用户运行 `extension-install` 后，应在 Chrome 中加载固定目录 `~/.local/share/xhs-workbench/chrome-extension`。用户说明中的安装命令必须包含带引号的 wheel `[video]` 附加依赖。

## 命令行登录与本机会话

以下说明适用于开发者 CLI，和 Chrome 扩展的正常采集路径不同。普通扩展用户在 Chrome 内自行登录即可，不需要运行 `xhs-workbench login`。

- `xhs-workbench login` 会打开本项目专用的独立浏览器，让使用者自行完成登录。它不会读取或导入日常 Chrome、Safari 等系统浏览器的既有资料。
- CLI 默认将本项目的登录数据保存到 `~/.local/auth/`：`browser-profile/` 保存独立浏览器的持久化资料（可包含站点 Cookie、网页存储和缓存），`cookies.json` 保存后续请求所需的登录 Cookie。代码内部显式指定认证目录时，使用指定目录；不会因此转去读取系统浏览器资料。
- 认证目录和独立浏览器资料目录限制为当前本机用户访问（目录权限 `0700`），Cookie 文件为 `0600`。这属于本机访问权限保护，并非加密存储。
- 后续 CLI 采集会使用这个会话向小红书发起认证请求。登录数据不写入采集报告，也不应提交到 Git、Issue 或公开压缩包；请勿分享认证目录。
- `extension-uninstall` 用于卸载扩展安装配置，不等于清除开发者 CLI 会话。停止 CLI 和独立浏览器后，可由用户自行移除该专用认证目录以清除本机保存的会话；已有报告是另外存放的。

## 命令行采集

```bash
uv run xhs-workbench safe-status
uv run xhs-workbench login
uv run xhs-workbench collect-search --keyword "AI 工作流" --limit 5
uv run xhs-workbench collect-account \
  --profile-url "https://www.xiaohongshu.com/user/profile/ACCOUNT_ID" --limit 5
```

`--output` 决定本次运行结果的根目录；每次执行都会创建新的运行目录，不会替换已有的 `results.json` 或 `index.html`。

```bash
uv run xhs-workbench collect-search --keyword "AI 工作流" \
  --output "/path/to/研究资料/小红书采集"
```

成功时命令只输出运行 ID、状态以及 JSON/HTML 的绝对路径；失败或输入无效时只输出有限错误代码。不要把浏览器数据、报告、媒体或环境文件提交到版本控制。

## 公开发布的提交身份

公开仓库仅接收可公开的产品快照，不能合并私有开发仓库的历史。公开提交的作者和提交者必须使用 GitHub noreply 邮箱；本仓库已设置项目维护者名称和 noreply 邮箱。提交前仍须检查实际生效的身份，避免环境变量覆盖 Git 配置。

发布前同时检查所有可达提交的 author/committer、带注释标签的 tagger 和附件中的元数据，不能只扫描源码。不要把个人邮箱、登录会话、测试报告或本机路径带入公开版本。
