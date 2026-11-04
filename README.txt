双板协同可定制肌电仿生手

八通道肌电手环经 ESP32-S3、ESP-NOW 和 ESP32-C3 向 A 板传输采样。
A 板完成滤波与手势识别，B 板控制机械手和 VGA 显示。两板均使用安路 EG4S20。
电脑端用于动作配置、数据采集、参数生成及下发。

代码目录

fpga/A_recognition/   A板RTL、初始化HEX、约束、物理ECO及当前位流
fpga/B_execution/     B板RTL、显示ROM、约束、物理ECO及当前位流
wireless/            ESP32-S3发送端、ESP32-C3接收端共用程序
pc/                  电脑端界面、通信、训练、量化与导出程序
models/current_8class/ 当前八类示例模型与部署参数
extras/              备用界面工具
 tools/               启动、离线测试及文件校验脚本
 docs/                构建说明、版本对应与文件清单

电脑端运行

使用 Python 3.11 或兼容版本，并安装 Tk 图形支持。在仓库根目录执行：

python -m venv .venv
Windows：.venv\Scripts\activate
macOS/Linux：source .venv/bin/activate
python -m pip install -r pc/requirements.txt
python tools/launch_pc.py

在界面中选择串口，加载模型并配置响应任务，按需启用机械手。
模型与任务加载后保持供电，双FPGA可脱离电脑持续运行。

无线固件构建与烧录

cd wireless
pio run -e transmitter_s3
pio run -e receiver_c3

需要烧录时在对应命令后加 -t upload。
发送端 receiverPeerAddress 须改为接收端的实际 MAC。
两端使用信道8、11 Mbps短前导码；接收端UART为921600。

FPGA构建

A板：fpga/A_recognition/原版R4源码与备用/rebuild.tcl
B板：fpga/B_execution/基准RTL源码/fpga_b/gesture_tasks/sources.txt

使用安路TD及对应器件数据库，管脚和时序约束位于各板源码目录。
当前位流包含物理ECO；基准RTL、补丁与检查点的使用顺序见 docs/BUILD_AND_RUN.txt。
位流名称及SHA-256见 docs/firmware_versions.json。

离线检查

python tools/verify_package.py
python tools/offline_tests.py
