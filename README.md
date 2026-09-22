# V2T · 视频转文字

输入视频链接或本地音视频，导出中英文 TXT。优先读取字幕，没有字幕时使用本地 Whisper 或自定义转写 API。

**仓库不含模型权重、音视频、文字稿或个人配置。模型由使用者按需下载到本机。**

## 功能

- 本地模型多档可选，默认 `small`；支持 CPU 和 NVIDIA GPU，选择后评估本机资源。
- 链接自动补全；支持队列添加、上下排序、移除和停止。
- 自定义模型与 TXT 保存目录，转写完成自动保存，可编辑另存。

## 安装与启动

下载源码并完整解压，然后运行：

| 系统 | 首次安装 | 之后启动 |
| --- | --- | --- |
| Windows 10/11 x64 | 双击 `install.bat` | 双击 `start.bat` |
| macOS 14+ Apple Silicon | `bash install.sh` | `bash start.sh` |
| Linux glibc 2.28+，x86_64 / aarch64 | `bash install.sh` | `bash start.sh` |

无需预装 Python。首次安装需联网，只安装运行环境和依赖，不下载 Whisper 权重。安装完成自动打开 [本地网页](http://127.0.0.1:7860)。保持服务终端打开，按 `Ctrl+C` 关闭服务。
再次打开点击 `start.bat` 即可。

## 使用

1. **选择模式**：本地模型默认 `small`；API 模式填写服务商地址、模型和密钥，无需下载 Whisper。
2. **设置并保存**：选择模型和文字稿目录（默认 `models/`、`output/`），保存配置。本地模式首次点击“下载所选模型并自检”。
3. **开始转写**：输入链接或选择文件，选择中文、英文或自动识别，点击“开始转写”。多个视频可依次“加入队列”，排序后开始。
4. **查看结果**：TXT 自动保存；可在网页复制文字或编辑另存。

已下载模型会复用。设备默认 `auto`，GPU 可用时优先使用，否则回退 CPU；缺少 GPU 库时可在网页按需安装。

停止当前任务会暂停队列。刷新网页保留队列，重启服务清空队列，已保存的 TXT 不受影响。

API 模式会将音频发送给所配置的服务商，费用由服务商决定。密钥及个人设置保存在本机，请勿上传 `settings.local.json`。

## 模型怎么选

不确定时先用 **small**。下载大小与运行显存不同；下表按本项目 GPU 模式（INT8_FLOAT16）估算，GPU 示例按显存匹配，并非全部实测。

| 模型 | 权重下载约 | 建议空闲显存 | 常见 GPU 示例 |
| --- | ---: | ---: | --- |
| tiny | 80 MB | 1 GiB | RTX 3050 Laptop 4 GB |
| base | 150 MB | 1.5 GiB | RTX 3050 Laptop 4 GB |
| **small（默认）** | **0.5 GB** | **2 GiB** | **RTX 3050 Laptop 4 / 6 GB** |
| medium | 1.6 GB | 4 GiB | RTX 3060 Laptop 6 GB |
| turbo | 1.7 GB | 4.5 GiB | RTX 4060 8 GB |
| large-v3 | 3.1 GB | 6 GiB | RTX 4060 8 GB / RTX 3060 桌面版 12 GB |

以网页显示的**剩余显存和模型自检**为准；同名显卡的笔记本版、桌面版可能不同。没有 NVIDIA GPU 也可使用 CPU。

估算见 [模型配置](model_manager.py)；显卡规格：[RTX 30 笔记本](https://www.nvidia.com/en-us/geforce/laptops/30-series/)、[RTX 4060](https://www.nvidia.com/en-us/geforce/graphics-cards/40-series/rtx-4060-4060ti/)、[RTX 3060 12 GB](https://nvidianews.nvidia.com/news/nvidia-introduces-geforce-rtx-3060-next-generation-of-the-worlds-most-popular-gpu)。

## 更多说明

[安装与排错](INSTALL.md) · [技术栈与数据流](TECH_STACK.md) · [MIT 许可证](LICENSE)
