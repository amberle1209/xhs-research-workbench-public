"""Build the macOS download package used by first-time Chrome extension users."""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

RELEASE_ROOT = "XHS Research Workbench"
EXTENSION_DIRECTORY = "xhs_workbench/extension_bundle"
EXTENSION_FILES = (
    "manifest.json",
    "service-worker.js",
    "content-script.js",
    "popup.html",
    "popup.css",
    "popup.js",
)


def _start_here_text(wheel_name: str) -> str:
    return f"""XHS Research Workbench v0.1.13 安装说明

本包不包含需要双击执行的 Install.command；请按下方终端命令安装。

1. 准备 Google Chrome，并按官方说明安装 uv： https://docs.astral.sh/uv/getting-started/installation/
2. 打开「终端」。输入 cd 后保留一个空格，再把本文件夹拖进终端窗口，按回车进入该目录。
3. 先决定报告保存位置。你只需要编辑下面第一行 `REPORTS_FOLDER=...` 双引号内的路径。示例路径可以直接使用；若想保存到其他位置，直接替换双引号内的整段路径即可。

   路径必须是完整路径：使用 `$HOME/` 开头，或 `/Users/你的用户名/` 开头；不能只填写文件夹名称。路径中有空格时，不要在空格前加入反斜杠 `\\`，也不要使用中文弯引号 `“ ”`；请保留代码中的英文半角双引号 `"`。例如：`REPORTS_FOLDER="$HOME/Documents/小红书 研究报告"`。`$REPORTS_FOLDER` 是固定变量名，后面的命令必须原样复制；不要写成 `$REPORTS\\_FOLDER`。

4. 修改第一行后，选中并一次性复制下面的**完整命令块**到终端运行。不要跳过第一行，也不要单独运行 `mkdir` 或后面的命令：

REPORTS_FOLDER="$HOME/Desktop/小红书研究报告"
uv tool install --force './{wheel_name}[video]'
export PATH="$HOME/.local/bin:$PATH"
mkdir -p "$REPORTS_FOLDER"
xhs-workbench extension-install --output "$REPORTS_FOLDER"
xhs-workbench extension-status

最后一行必须显示：{{"status":"ready"}}

每一行命令的作用：
- `REPORTS_FOLDER=...`：指定报告保存位置。只改 REPORTS_FOLDER 这一行双引号内的路径；其余命令不要改。
- `uv tool install ...[video]`：安装本包附带的本地工具和视频转录组件。
- `export PATH=...`：让当前终端能找到刚安装的工具。
- `mkdir -p ...`：创建你指定的报告文件夹；已经存在也不会删除其中内容。
- `xhs-workbench extension-install --output ...`：把报告文件夹设为扩展保存报告的位置。
- `xhs-workbench extension-status`：只检查安装是否就绪，不会登录、采集或写入报告。

5. 在 Chrome 地址栏打开 chrome://extensions，开启右上角的“开发者模式”，点击“加载已解压的扩展程序”（Load unpacked）。
6. 文件选择窗口打开后，按 Command + Shift + G，输入 ~/.local/share/xhs-workbench/chrome-extension，按回车并选择该文件夹。请勿选择本压缩包中的 Chrome Extension 副本。
7. 确认扩展卡片显示 0.1.13，再从工具栏拼图图标打开或固定 XHS Research Workbench。
8. 在同一个 Chrome 个人资料中自行登录小红书，打开一篇帖子详情页，点击“提取当前帖子”。采集完成后点击“打开本地报告”。

视频笔记的基础报告会先生成；最长 15 分钟的普通话音频可在后台尝试转录，完成后刷新报告看文字稿。可关闭笔记页和弹窗，但请保持 Chrome 运行、电脑不休眠。首次使用会联网下载约 465 MB 模型，转录在本机完成，不需要 API Key 或付费转录账户。常规处理最多等待 10 分钟；单个视频最多 100 MiB，页面没有可用视频来源时也可能无法保存。自动识别文字请校对。

报告会保存到你在 REPORTS_FOLDER 中设置的文件夹。请在首次安装前选好这个位置。

旧版用户请在新解压包中使用原来的报告文件夹重新运行完整命令块，再到 chrome://extensions 点击“重新加载”，确认版本 0.1.13。若之前从下载目录加载扩展，请改从上面的固定目录加载；已有报告不需要移动或删除。

如果终端显示 `>`，说明引号没有正确结束。按 `Ctrl + C` 退出，再从第 3 步重新复制完整命令块；在 `>` 状态下不要继续输入命令。

以后如需更换报告保存位置：不要只改 REPORTS_FOLDER 后单独重跑安装命令。先在终端运行：

xhs-workbench extension-uninstall

它不会删除已有报告。然后回到第 3 步，修改 REPORTS_FOLDER 后重新运行完整命令块。
"""


def _write_file(archive: zipfile.ZipFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = zipfile.ZipInfo(name)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = mode << 16
    archive.writestr(info, data)


def build_macos_user_bundle(*, wheel: Path, output: Path) -> Path:
    """Create a self-contained download containing a Chrome-loadable extension directory."""
    wheel_path = Path(wheel).resolve(strict=True)
    if wheel_path.suffix != ".whl":
        raise ValueError("wheel must be a .whl file")
    output_path = Path(output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(wheel_path) as wheel_archive:
        source_names = {f"{EXTENSION_DIRECTORY}/{name}" for name in EXTENSION_FILES}
        if not source_names.issubset(set(wheel_archive.namelist())):
            raise ValueError("wheel does not contain the complete Chrome extension bundle")
        extension_files = {name: wheel_archive.read(f"{EXTENSION_DIRECTORY}/{name}") for name in EXTENSION_FILES}

    temporary = output_path.with_suffix(f"{output_path.suffix}.tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in EXTENSION_FILES:
            _write_file(archive, f"{RELEASE_ROOT}/Chrome Extension/{name}", extension_files[name])
        _write_file(archive, f"{RELEASE_ROOT}/{wheel_path.name}", wheel_path.read_bytes())
        _write_file(
            archive,
            f"{RELEASE_ROOT}/Start Here.txt",
            _start_here_text(wheel_path.name).encode("utf-8"),
        )
    temporary.replace(output_path)
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the macOS first-time-user download package.")
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    print(build_macos_user_bundle(wheel=arguments.wheel, output=arguments.output))


if __name__ == "__main__":
    main()
