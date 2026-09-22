# 安装与使用

无需预装 Python，也无需管理员权限。首次安装需要联网访问 GitHub 和 PyPI。

## 1. 下载并启动

下载源码 ZIP，**完整解压**到自己有写入权限的目录，再按下表操作：

| 系统 | 首次安装 | 下次启动 |
| --- | --- | --- |
| Windows 10 / 11，x64 | 双击 `install.bat` | 双击 `start.bat` |
| macOS 14+，Apple Silicon | 终端执行 `bash install.sh` | `bash start.sh` |
| Linux x86_64 / aarch64，glibc 2.28+ | 终端执行 `bash install.sh` | `bash start.sh` |

终端命令需在解压后的项目目录执行。Linux 需有 `bash`、`curl`、`tar`、`tee` 和 SHA256 工具。
Windows ARM、32 位系统、macOS Intel 和 Alpine / musl 暂不支持一键安装。

完成后自动打开网页；使用期间请保留服务窗口，按 **Ctrl+C** 可退出。
若浏览器没有打开，手动访问 [http://127.0.0.1:7860](http://127.0.0.1:7860)。

安装只准备本项目的 Python 和依赖，**不会下载 Whisper 模型**，也不会更改系统 Python。

## 2. 选择识别方式

**本地模式：**

1. 选择模型档位、模型目录和 TXT 保存目录，再保存设置。首次使用可选 `small`，设备保持“自动选择”。
2. 查看本机资源评估，再点击“下载所选模型并自检”。权重从 Hugging Face 下载，已有模型会复用。
3. 输入链接或选择本地文件，开始转写。

模型大小和显卡参考见 [模型怎么选](README.md#模型怎么选)。若直接开始本地转写时缺少权重，也会按需下载。
模型准备完成后，本地文件可离线识别；视频链接仍需联网。

**API 模式：** 填写服务商的 Base URL、Model ID 和 API Key，无需下载 Whisper。
需要语音识别时，音频会发送给该服务商，费用按其规则计算。密钥保存在本机，**不要上传个人配置文件**。

## 3. 可选：启用 NVIDIA GPU

“自动选择”会优先使用可用的 NVIDIA GPU，否则使用 CPU；指定 GPU 后，条件不满足会提示失败。
Apple Silicon 当前使用 CPU，不支持 Metal。

若网页提示缺少 CUDA / cuDNN 运行库，可点击“按需安装 GPU 运行库”，完成后再次执行模型自检。
Windows 运行库下载约 1.26 GB，建议预留 6 GiB 磁盘空间。运行库只安装在项目内，不安装或替换显卡驱动。
此入口面向 Windows x64 / Linux NVIDIA 环境；CPU 和 API 用户无需安装。

## 常见问题

| 情况 | 处理方式 |
| --- | --- |
| 安装或模型下载失败 | 检查网络后重试；已有组件和模型会复用。 |
| 提示虚拟环境损坏或版本不符 | 将源码重新解压到新目录安装，原文件会保留。 |
| GPU 不可用或显存不足 | 查看网页提示，安装所需运行库、选择更小模型，或改用 CPU。 |
| 视频链接无法读取 | 确认网页可访问；有登录要求时提供 Cookie，或改用本地音视频。 |

安装日志位于 `.cache/install/`。若检查系统时就失败，请查看服务窗口提示。
Windows 安装与 GPU 转写已实机验证；macOS / Linux 仍需在对应设备上验证。

<details>
<summary>只检查环境，或安装后暂不启动</summary>

在项目目录运行；检查命令不会联网、安装或启动服务。

| 操作 | Windows | macOS / Linux |
| --- | --- | --- |
| 只检查 | `install.bat -CheckOnly` | `bash install.sh --check` |
| 安装但不启动 | `install.bat -NoLaunch` | `bash install.sh --no-launch` |

</details>

## 上传 GitHub

将干净的 `upload_folder` **内部内容**上传到仓库根目录，包括 `.gitignore`、`.github/` 和 `settings.example.json`，不要多套一层文件夹。

**不上传模型权重、音视频、文字稿、Cookie、密钥或个人配置。**
默认 `models/`、`output/`，以及 `.venv/`、`.runtime/`、`.cache/`、`.env*`、`settings.local.json` 都属于本地文件。
GitHub 网页上传**不会按本地 `.gitignore` 自动排除文件**，请直接上传尚未运行过的干净源码目录。

需要源码 ZIP 时，在已安装的项目目录运行：Windows 用 `.venv\Scripts\python.exe tools/build_release.py`；macOS / Linux 用 `.venv/bin/python tools/build_release.py`。
生成的 `dist/v2t-source.zip` 仅包含打包清单中的源码，不包含模型或用户数据。
