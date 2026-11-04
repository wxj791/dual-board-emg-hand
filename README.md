# 双板协同可定制肌电仿生手

八通道肌电手环通过 ESP32-S3 → ESP-NOW → ESP32-C3 → UART 向 A 板送入采样；A 板完成信号处理与手势识别，B 板完成机械手任务和 VGA 显示。两块 FPGA 均为安路 EG4S20。

本仓库整理当前系统的源码、约束、模型参数、烧录文件和物理 ECO 补丁。**先从本页选择入口，再看各目录的原始说明。**

## 目录与入口

| 目录 | 内容 | 主要入口 |
|---|---|---|
| `fpga/A_recognition/` | A 板模块化 RTL、冻结构建输入、初始化 HEX、管脚/时序约束、ECO 与位流 | `原版R4源码与备用/source/`、`原版R4源码与备用/rebuild.tcl` |
| `fpga/B_execution/` | B 板 RTL、显示 ROM、约束、执行/示教代码、物理 ECO 与位流 | `基准RTL源码/fpga_b/gesture_tasks/b_tasks_top.v`、同目录 `sources.txt` |
| `wireless/` | S3 发送端与 C3 接收端共用源码，PlatformIO 双环境 | `src/main_10samples_unicast_c3.cpp`、`platformio.ini` |
| `pc/` | 电脑端任务配置、采集、训练、量化、导出及协议工具 | 在仓库根目录运行 `python tools/launch_pc.py` |
| `models/current_8class/` | 当前八类示例的训练材料、评估导出及 A 板部署参数 | `export/manifest.json`、`a_deployment/a_model.json` |
| `extras/EMG_Studio_UI_20261031/` | 单独保留的较早界面定制版，方便查看和继续开发 | `先读说明.md`；当前系统默认使用 `pc/` |
| `docs/` | 目录导航、版本说明、来源记录及验证记录 | `BUILD_AND_RUN.md`、`SOURCE_ORIGINS.json` |
| `tools/` | 便携启动、离线检查和完整性校验 | `verify_package.py`、`offline_tests.py` |

## 快速开始

需要 Python 3.11 或兼容版本，图形界面需要 Tk。建议先创建虚拟环境：

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS / Linux: source .venv/bin/activate
python -m pip install -r pc/requirements.txt
python tools/verify_package.py
python tools/offline_tests.py
python tools/launch_pc.py
```

启动脚本从当前目录定位八类示例模型，正常入口使用原 A/B 协议。串口连接、参数下发和机械手使能由界面显式操作。参数准备在电脑端完成，运行期间的识别和控制由 FPGA 承担。

无线程序构建：

```sh
cd wireless
pio run -e transmitter_s3
pio run -e receiver_c3
```

烧录时在相应命令后加 `-t upload`。发送端使用源码中的 `receiverPeerAddress` 配置 C3 的实际 MAC；两端保持信道 8 和 `WIFI_PHY_RATE_11M_S`（11 Mbps 短前导码）一致。

## FPGA 版本关系

当前 A/B 位流均含物理 ECO，完整保留基准 RTL、物理网表、检查点和补丁。直接重综合基准 RTL 得到的是基准实现；重建当前位流还需按各板记录应用 ECO。各板入口和执行顺序见 [构建与运行说明](docs/BUILD_AND_RUN.md)。

两个当前位流的原始文件名和 SHA-256 记录在 `docs/firmware_versions.json`。现有示例模型可更换，手势及响应也可重新配置。

## 代码包范围与校验

压缩包与 GitHub 使用同一份整理目录。保留完整项目源文件、模型必要材料、初始化文件、ROM、约束和当前实现；原始采集数据汇总、实时日志、第三方 Python 便携环境和构建缓存单独存放。依赖通过 `pc/requirements.txt` 安装，原环境版本清单保留在 `pc/requirements.original-lock.txt` 供追溯。

`MANIFEST.json` 与 `SHA256SUMS.txt` 记录逐文件校验值。`python tools/verify_package.py` 可离线检查文件、源代码语法、A/B 输入依赖及模型参数哈希。验证结果见 `docs/PACKAGE_VALIDATION.json`；该结果说明打包与离线检查范围，硬件运行沿用项目已有记录。
