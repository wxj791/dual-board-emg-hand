#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include "driver/adc.h"

#include "emg_filter.h"

// ESP-NOW 固定工作信道。避开极度拥挤的 1/6/11 路由默认信道，选择信道 8。
#ifndef ESPNOW_WIFI_CHANNEL
#define ESPNOW_WIFI_CHANNEL 8
#endif

// ============================================================================
// ESP-NOW packet format used by BOTH transmitter and receiver in this file.
// IMPORTANT: flash this SAME source to both ends.
// ESP-NOW v1 payload limit is 250 bytes, so 10 samples/packet is a safe choice.
// 10 * 8 * uint16_t = 160 bytes EMG + metadata, comfortably below 250 bytes.
// ============================================================================
static constexpr uint8_t EMG_SAMPLES_PER_PACKET = 10;

struct EmgBatchPacket {
    uint32_t packet_id;
    uint32_t timestamp_ms;
    uint16_t emg_data[EMG_SAMPLES_PER_PACKET][8];

    // In diagnostic firmware these carry cumulative TX counters:
    // roll  = adcFramesBuilt
    // pitch = espNowOk
    // yaw   = txPacketsDropped
    float roll;
    float pitch;
    float yaw;

    uint8_t sensor_online;
};

static_assert(sizeof(EmgBatchPacket) <= 250,
              "EmgBatchPacket exceeds ESP-NOW v1 250-byte payload limit.");

// ==============================================================================
//  角色配置开关：优先使用 platformio.ini 注入的角色宏
// ==============================================================================
#ifndef ROLE_TRANSMITTER
#define ROLE_TRANSMITTER 1   // 1: 手环发射端 (TX)
#endif

#ifndef ROLE_RECEIVER
#define ROLE_RECEIVER    2   // 2: 目标接收端 (RX)
#endif

#ifndef CURRENT_ROLE
#define CURRENT_ROLE ROLE_TRANSMITTER
#endif
// ==============================================================================

// 广播 MAC 地址 (无需事先配对，同信道开机即通)
static uint8_t receiverPeerAddress[] = {0x30, 0xED, 0xA0, 0x89, 0x7D, 0xD4};

#if (CURRENT_ROLE == ROLE_TRANSMITTER)
// ==============================================================================
//                  【发送端：手环 (Transmitter) - ADC DMA 1kHz】
// ==============================================================================

static EmgBatchPacket txBatchPacket;
static uint32_t packetCounter = 0;
static uint8_t batchSampleIdx = 0;

// 目标：8 通道，每通道 1kHz => ADC 总 conversion rate = 8kHz
static constexpr uint32_t PER_CHANNEL_HZ = 1000;
static constexpr uint32_t NUM_EMG_CH = 8;
static constexpr uint32_t ADC_TOTAL_HZ = PER_CHANNEL_HZ * NUM_EMG_CH;

// ESP32-S3: GPIO1..GPIO10 对应 ADC1_CH0..ADC1_CH9
// 原硬件映射：SIGNAL1 -> IO8 ... SIGNAL8 -> IO1
static const uint8_t ADC_GPIO_PINS[NUM_EMG_CH] = {8, 7, 6, 5, 4, 3, 2, 1};
static const adc_channel_t ADC_CHANNELS[NUM_EMG_CH] = {
    ADC_CHANNEL_7, ADC_CHANNEL_6, ADC_CHANNEL_5, ADC_CHANNEL_4,
    ADC_CHANNEL_3, ADC_CHANNEL_2, ADC_CHANNEL_1, ADC_CHANNEL_0
};

// 维特智能 JY901B 协议状态机
static uint8_t  u0Buffer[11];
static uint8_t  u0Index = 0;
static unsigned long lastGyroValidTime = 0;
static bool gyroOnline = false;

// ADC DMA Buffer
static constexpr size_t ADC_DMA_FRAME_BYTES = 1024;
static uint8_t adcDmaBuf[ADC_DMA_FRAME_BYTES];

// 每个“完整 8 通道时刻”的暂存。
// Continuous ADC 是逐 conversion 返回；当 8 个通道各拿到一个新值后才组成一帧。
static uint16_t currentFrame[NUM_EMG_CH] = {0};
static uint8_t currentFrameMask = 0;

// ESP-NOW 发送握手：同一时刻只允许一个异步 send 在飞
static volatile bool espNowBusy = false;
static volatile uint32_t espNowOk = 0;
static volatile uint32_t espNowFail = 0;
static volatile uint32_t adcFramesBuilt = 0;
static volatile uint32_t txPacketsDropped = 0;

// 增大 TX 包队列 (32 -> 128)。避免在无线短时重传或突发抖动时队列满导致无情丢包。
static constexpr uint8_t TX_QUEUE_LEN = 128;
static EmgBatchPacket txQueue[TX_QUEUE_LEN];
static volatile uint8_t txQHead = 0;
static volatile uint8_t txQTail = 0;
static portMUX_TYPE txQMux = portMUX_INITIALIZER_UNLOCKED;

static inline bool txQueuePush(const EmgBatchPacket &pkt) {
    bool ok = false;
    portENTER_CRITICAL(&txQMux);
    uint8_t next = (uint8_t)((txQHead + 1) % TX_QUEUE_LEN);
    if (next != txQTail) {
        txQueue[txQHead] = pkt;
        txQHead = next;
        ok = true;
    }
    portEXIT_CRITICAL(&txQMux);
    if (!ok) txPacketsDropped++;
    return ok;
}

static inline bool txQueuePop(EmgBatchPacket &pkt) {
    bool ok = false;
    portENTER_CRITICAL(&txQMux);
    if (txQTail != txQHead) {
        pkt = txQueue[txQTail];
        txQTail = (uint8_t)((txQTail + 1) % TX_QUEUE_LEN);
        ok = true;
    }
    portEXIT_CRITICAL(&txQMux);
    return ok;
}

void parseWitMotionByte(uint8_t ch) {
    if (u0Index == 0) {
        if (ch == 0x55) {
            u0Buffer[0] = ch;
            u0Index = 1;
        }
    } else {
        u0Buffer[u0Index++] = ch;
        if (u0Index == 11) {
            uint8_t sum = 0;
            for (int i = 0; i < 10; i++) sum += u0Buffer[i];
            if (sum == u0Buffer[10] && u0Buffer[1] == 0x53) {
                int16_t rawRoll  = (int16_t)((u0Buffer[3] << 8) | u0Buffer[2]);
                int16_t rawPitch = (int16_t)((u0Buffer[5] << 8) | u0Buffer[4]);
                int16_t rawYaw   = (int16_t)((u0Buffer[7] << 8) | u0Buffer[6]);
                txBatchPacket.roll  = (float)rawRoll  / 32768.0f * 180.0f;
                txBatchPacket.pitch = (float)rawPitch / 32768.0f * 180.0f;
                txBatchPacket.yaw   = (float)rawYaw   / 32768.0f * 180.0f;
                lastGyroValidTime = millis();
                gyroOnline = true;
            }
            u0Index = 0;
        }
    }
}

void onDataSent(const uint8_t *mac_addr, esp_now_send_status_t status) {
    if (status == ESP_NOW_SEND_SUCCESS) espNowOk++;
    else espNowFail++;
    espNowBusy = false;
}

static bool initAdcDma() {
    uint32_t adc1_mask = 0;
    for (int i = 0; i < NUM_EMG_CH; i++) {
        adc1_mask |= (1U << ADC_CHANNELS[i]);
    }

    adc_digi_init_config_t adc_dma_config = {};
    adc_dma_config.max_store_buf_size = 4096;
    adc_dma_config.conv_num_each_intr = 512;
    adc_dma_config.adc1_chan_mask = adc1_mask;
    adc_dma_config.adc2_chan_mask = 0;

    esp_err_t rc = adc_digi_initialize(&adc_dma_config);
    if (rc != ESP_OK) {
        return false;
    }

    static adc_digi_pattern_config_t pattern[NUM_EMG_CH] = {};
    for (int i = 0; i < NUM_EMG_CH; i++) {
        pattern[i].atten = ADC_ATTEN_DB_12;
        pattern[i].channel = ADC_CHANNELS[i];
        pattern[i].unit = 0;  // IDF4.4 digi pattern: 0 = ADC1, 1 = ADC2 (ADC_UNIT_1 枚举值=1 会被当成 ADC2)
        pattern[i].bit_width = SOC_ADC_DIGI_MAX_BITWIDTH;
    }

    adc_digi_configuration_t dig_cfg = {};
    dig_cfg.sample_freq_hz = ADC_TOTAL_HZ;
    dig_cfg.conv_mode = ADC_CONV_SINGLE_UNIT_1;

    // ESP32-S3 DMA output uses TYPE2 (32-bit: data[11:0], channel[15:13], unit[16])
    dig_cfg.format = ADC_DIGI_OUTPUT_FORMAT_TYPE2;

    dig_cfg.pattern_num = NUM_EMG_CH;
    dig_cfg.adc_pattern = pattern;

    rc = adc_digi_controller_configure(&dig_cfg);
    if (rc != ESP_OK) {
        adc_digi_deinitialize();
        return false;
    }

    rc = adc_digi_start();
    if (rc != ESP_OK) {
        adc_digi_deinitialize();
        return false;
    }

    return true;
}

// 12-bit raw -> 近似 mV。
// 注意：这是为了保持现有 PC/训练数据“类似 mV”的尺度，不是精密电压标定。
// 后续如需要绝对电压，可加 ADC calibration，不影响 DMA 架构。
static inline uint16_t rawToMv(uint16_t raw) {
    uint32_t mv = ((uint32_t)raw * 3300U + 2047U) / 4095U;
    if (mv > 3300U) mv = 3300U;
    return (uint16_t)mv;
}

static inline int logicalIndexFromAdcChannel(uint8_t adcCh) {
    // ADC1_CH7..0 -> SIGNAL1..8 -> 数组索引 0..7
    if (adcCh <= 7) return 7 - adcCh;
    return -1;
}

static bool adcDmaReady = false;

static void consumeAdcDma() {
    if (!adcDmaReady) return;
    uint32_t bytesRead = 0;
    esp_err_t ret = adc_digi_read_bytes(adcDmaBuf, sizeof(adcDmaBuf), &bytesRead, 0);
    if (ret != ESP_OK || bytesRead == 0) return;

    for (uint32_t i = 0; i + sizeof(adc_digi_output_data_t) <= bytesRead; i += sizeof(adc_digi_output_data_t)) {
        const adc_digi_output_data_t *p =
            (const adc_digi_output_data_t *)&adcDmaBuf[i];
        // S3 硬件 DMA 单元为 32-bit，对应 type2 (channel: p->type2.channel, data: p->type2.data)
        // 且不需要判断 unit，直接由 channel 映射通道
        uint8_t ch = p->type2.channel;
        uint16_t raw = p->type2.data;

        int idx = logicalIndexFromAdcChannel(ch);
        if (idx < 0 || idx >= (int)NUM_EMG_CH) continue;

        currentFrame[idx] = rawToMv(raw);
        currentFrameMask |= (uint8_t)(1U << idx);

        if (currentFrameMask == 0xFF) {
            for (int c = 0; c < NUM_EMG_CH; c++) {
                txBatchPacket.emg_data[batchSampleIdx][c] = currentFrame[c];
            }
            currentFrameMask = 0;
            batchSampleIdx++;
            adcFramesBuilt++;

            if (batchSampleIdx >= EMG_SAMPLES_PER_PACKET) {
                batchSampleIdx = 0;
                txBatchPacket.packet_id = ++packetCounter;
                txBatchPacket.timestamp_ms = millis();

                // Diagnostic build: reuse roll/pitch/yaw as cumulative TX counters.
                // This does NOT change EmgBatchPacket layout, so packet_def.h stays unchanged.
                // roll  = total complete 8-channel ADC frames built
                // pitch = total ESP-NOW packets confirmed successful
                // yaw   = total packets dropped because TX queue was full
                txBatchPacket.roll  = (float)adcFramesBuilt;
                txBatchPacket.pitch = (float)espNowOk;
                txBatchPacket.yaw   = (float)txPacketsDropped;
                txBatchPacket.sensor_online = 0;

                txQueuePush(txBatchPacket);
            }
        }
    }
}

static void serviceEspNowTx() {
    if (espNowBusy) return;

    static EmgBatchPacket inFlight;
    if (!txQueuePop(inFlight)) return;

    espNowBusy = true;
    esp_err_t rc = esp_now_send(receiverPeerAddress, (uint8_t *)&inFlight, sizeof(inFlight));
    if (rc != ESP_OK) {
        espNowFail++;
        espNowBusy = false;
    }
}

void setup() {
    // JY901B
    Serial.begin(9600);

    // ADC GPIO 输入
    for (int i = 0; i < NUM_EMG_CH; i++) {
        pinMode(ADC_GPIO_PINS[i], INPUT);
    }

    // WiFi / ESP-NOW
    WiFi.mode(WIFI_STA);
    WiFi.disconnect();

    // Real-time sensor link: disable modem sleep on both ends.
    esp_wifi_set_ps(WIFI_PS_NONE);

    // 开启 802.11b/g/n 协议栈，并配置 11Mbps 短前导码 (或 6Mbps OFDM)，
    // 空中传输耗时比 1Mbps 降低数倍，彻底释放信道占用时间，根除信道堵塞雪崩。
    esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);

    esp_wifi_set_promiscuous(true);
    esp_wifi_set_channel(ESPNOW_WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE);
    esp_wifi_set_promiscuous(false);

    if (esp_now_init() != ESP_OK) {
        while (true) delay(1000);
    }

    // 设置为 11Mbps 短前导码 (WIFI_PHY_RATE_11M_S)，既兼顾 CCK 穿透力又具有极低空中占空比
    esp_wifi_config_espnow_rate(WIFI_IF_STA, WIFI_PHY_RATE_11M_S);

    esp_now_register_send_cb(onDataSent);

    esp_now_peer_info_t peerInfo = {};
    memcpy(peerInfo.peer_addr, receiverPeerAddress, 6);
    peerInfo.channel = ESPNOW_WIFI_CHANNEL;
    peerInfo.encrypt = false;
    esp_now_add_peer(&peerInfo);

    memset(&txBatchPacket, 0, sizeof(txBatchPacket));

    // Do not ESP_ERROR_CHECK() here. If ADC DMA setup fails, keep the MCU alive
    // instead of entering an abort/reboot loop.
    const bool adcDmaOk = initAdcDma();
    adcDmaReady = adcDmaOk;
    if (!adcDmaOk) {
        // Keep running so the board does not reboot endlessly.
        // No EMG packets will be generated until the ADC configuration is fixed.
        return;
    }
}

void loop() {
    // 陀螺仪仍按原方式解析，但不能阻塞 DMA；DMA 在硬件后台持续采样。
    while (Serial.available() > 0) {
        parseWitMotionByte(Serial.read());
    }

    if (millis() - lastGyroValidTime > 200) {
        gyroOnline = false;
        txBatchPacket.roll = 0.0f;
        txBatchPacket.pitch = 0.0f;
        txBatchPacket.yaw = 0.0f;
        txBatchPacket.sensor_online = 0;
    } else {
        txBatchPacket.sensor_online = 1;
    }

    consumeAdcDma();
    serviceEspNowTx();


    // 不加 delay；让 CPU 尽快搬运 DMA 数据并服务无线队列。
}

#elif (CURRENT_ROLE == ROLE_RECEIVER)
// ==============================================================================
//                         【接收端：底座/目标板 (Receiver)】
// ==============================================================================
//
// 设计目标：
// 1) ESP-NOW 回调只负责把“整包”快速压入环形队列，避免新包覆盖旧包；
// 2) 主循环从队列取包并完整写入串口；
// 3) 不再因为 Serial.availableForWrite() 暂时不足而 break 丢掉半包；
// 4) 保持 PC 端协议完全不变：每采样点仍是 8×float32 + JustFloat 4B 尾标记。
//
// 注意：921600 baud, 8N1 的有效字节吞吐上限约 92.16 kB/s；
//       2 kHz × 36 B/sample = 72 kB/s，理论上可持续承载。

// ------------------------------
// ESP-NOW 接收环形队列
// ------------------------------
// 64 包 × 10 samples/包 = 最多缓存约 320 ms 的 2 kHz 数据突发。
// 对 ESP32-S3 的 RAM 占用很小，却能显著吸收 Wi-Fi/USB/UART 调度抖动。
static constexpr uint16_t RX_QUEUE_SIZE = 64;
static EmgBatchPacket rxQueue[RX_QUEUE_SIZE];
static volatile uint16_t rxHead = 0;  // 下一次写入位置
static volatile uint16_t rxTail = 0;  // 下一次读出位置
static portMUX_TYPE rxQueueMux = portMUX_INITIALIZER_UNLOCKED;

// 链路诊断计数（只在 MCU 内部保存，不能打印到当前 Serial，
// 因为当前 Serial 正在输出 JustFloat 二进制流，插入文本会破坏 PC 解析）。
static volatile uint32_t rxPacketsOk = 0;
static volatile uint32_t rxPacketsBadLen = 0;
static volatile uint32_t rxQueueOverflow = 0;
static volatile uint32_t rxWirelessGapPackets = 0;
static volatile uint32_t lastRxPacketId = 0;
static volatile bool haveLastRxPacketId = false;

// Latest cumulative transmitter diagnostics carried in roll/pitch/yaw.
// These are for this temporary diagnostic firmware; gyro values are not forwarded.
static volatile uint32_t remoteAdcFramesBuilt = 0;
static volatile uint32_t remoteEspNowOk = 0;
static volatile uint32_t remoteTxQueueDrops = 0;

// Special JustFloat diagnostic frame marker.
// The PC GUI detects this BEFORE channel remapping and does not record it as EMG.
static constexpr float DIAG_MAGIC = -123456.0f;

// 在 Wi-Fi/ESP-NOW 回调中只做：校验长度 + 统计 packet_id + 入队。
// 不在回调里做 Serial.write，避免长时间阻塞 Wi-Fi 任务。
void onDataRecv(const uint8_t *mac_addr, const uint8_t *incomingData, int len) {
    if (len != sizeof(EmgBatchPacket)) {
        rxPacketsBadLen++;
        return;
    }

    const EmgBatchPacket *pkt = reinterpret_cast<const EmgBatchPacket *>(incomingData);

    // Snapshot transmitter cumulative diagnostics.
    remoteAdcFramesBuilt = (uint32_t)pkt->roll;
    remoteEspNowOk = (uint32_t)pkt->pitch;
    remoteTxQueueDrops = (uint32_t)pkt->yaw;

    // 统计无线侧 packet_id 缺口。正常情况下 packet_id 每包 +1。
    if (haveLastRxPacketId) {
        const uint32_t expected = lastRxPacketId + 1;
        if (pkt->packet_id != expected) {
            // uint32_t 自然回绕时，下面的无符号减法仍可正常工作。
            const uint32_t gap = pkt->packet_id - expected;
            if (gap < 0x80000000UL) {
                rxWirelessGapPackets += gap;
            }
        }
    }
    lastRxPacketId = pkt->packet_id;
    haveLastRxPacketId = true;

    portENTER_CRITICAL(&rxQueueMux);
    const uint16_t nextHead = (uint16_t)((rxHead + 1U) % RX_QUEUE_SIZE);

    if (nextHead == rxTail) {
        // 队列满：宁可明确计数整包溢出，也不要覆盖尚未处理的旧包。
        rxQueueOverflow++;
        portEXIT_CRITICAL(&rxQueueMux);
        return;
    }

    memcpy(&rxQueue[rxHead], incomingData, sizeof(EmgBatchPacket));
    rxHead = nextHead;
    rxPacketsOk++;
    portEXIT_CRITICAL(&rxQueueMux);
}

// VOFA+ JustFloat 协议尾部帧：0x00,0x00,0x80,0x7F
static const uint8_t JUSTFLOAT_TAIL[4] = {0x00, 0x00, 0x80, 0x7F};

// 每个 ESP-NOW 包在串口侧会展开为：
// EMG_SAMPLES_PER_PACKET × (8×float32 + 4B tail)
static constexpr size_t SERIAL_BYTES_PER_SAMPLE = 8 * sizeof(float) + 4;
static constexpr size_t SERIAL_BATCH_BYTES =
    EMG_SAMPLES_PER_PACKET * SERIAL_BYTES_PER_SAMPLE;

// Emit one synthetic 8-float JustFloat frame per second.
// Layout:
// [0] magic
// [1] ADC complete 8-ch frames/s at TX
// [2] TX generated packets/s (packet_id delta)
// [3] ESP-NOW successful packets/s at TX
// [4] RX received packets/s
// [5] wireless missing packets/s (packet_id gaps)
// [6] TX queue dropped packets/s
// [7] RX queue overflow packets/s
static void serviceDiagnosticFrame() {
    static uint32_t lastMs = 0;
    static uint32_t prevAdcFrames = 0;
    static uint32_t prevPacketId = 0;
    static uint32_t prevEspOk = 0;
    static uint32_t prevRxOk = 0;
    static uint32_t prevGap = 0;
    static uint32_t prevTxDrop = 0;
    static uint32_t prevRxOverflow = 0;

    const uint32_t now = millis();
    if (now - lastMs < 1000) return;

    const float dt = (lastMs == 0) ? 1.0f : (float)(now - lastMs) / 1000.0f;
    lastMs = now;

    const uint32_t adcNow = remoteAdcFramesBuilt;
    const uint32_t packetNow = lastRxPacketId;
    const uint32_t espOkNow = remoteEspNowOk;
    const uint32_t rxOkNow = rxPacketsOk;
    const uint32_t gapNow = rxWirelessGapPackets;
    const uint32_t txDropNow = remoteTxQueueDrops;
    const uint32_t rxOverflowNow = rxQueueOverflow;

    float d[8];
    d[0] = DIAG_MAGIC;
    d[1] = (float)(adcNow - prevAdcFrames) / dt;
    d[2] = (float)(packetNow - prevPacketId) / dt;
    d[3] = (float)(espOkNow - prevEspOk) / dt;
    d[4] = (float)(rxOkNow - prevRxOk) / dt;
    d[5] = (float)(gapNow - prevGap) / dt;
    d[6] = (float)(txDropNow - prevTxDrop) / dt;
    d[7] = (float)(rxOverflowNow - prevRxOverflow) / dt;

    prevAdcFrames = adcNow;
    prevPacketId = packetNow;
    prevEspOk = espOkNow;
    prevRxOk = rxOkNow;
    prevGap = gapNow;
    prevTxDrop = txDropNow;
    prevRxOverflow = rxOverflowNow;

    Serial.write(reinterpret_cast<const uint8_t *>(d), sizeof(d));
    Serial.write(JUSTFLOAT_TAIL, sizeof(JUSTFLOAT_TAIL));
}

void setup() {
    // 增大 TX 缓冲，进一步吸收 USB-UART/主机调度抖动。
    // 必须在 Serial.begin() 之前调用。
    Serial.setTxBufferSize(8192);
    Serial.begin(921600);
    delay(500);

    // 1. 初始化 WiFi 为 STA 模式并断开普通 Wi-Fi 连接
    WiFi.mode(WIFI_STA);
    WiFi.disconnect();

    // Receiver must also stay awake; otherwise ESP-NOW packets can be missed.
    esp_wifi_set_ps(WIFI_PS_NONE);

    // 接收端同样开启 802.11b/g/n 协议栈
    esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);

    // 2. 锁定 ESP-NOW 信道
    esp_wifi_set_promiscuous(true);
    esp_wifi_set_channel(ESPNOW_WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE);
    esp_wifi_set_promiscuous(false);

    // 3. 初始化 ESP-NOW
    if (esp_now_init() != ESP_OK) {
        return;
    }

    // 与发射端严格匹配：11Mbps 短前导码
    esp_wifi_config_espnow_rate(WIFI_IF_STA, WIFI_PHY_RATE_11M_S);

    esp_now_register_recv_cb(onDataRecv);
}

void loop() {
    // 一次只从共享队列中弹出一个完整包到局部变量；
    // 随后即使 Serial.write 阻塞，ESP-NOW 回调仍可以继续往环形队列压入新包。
    EmgBatchPacket localPacket;
    bool havePacket = false;

    portENTER_CRITICAL(&rxQueueMux);
    if (rxTail != rxHead) {
        memcpy(&localPacket, &rxQueue[rxTail], sizeof(EmgBatchPacket));
        rxTail = (uint16_t)((rxTail + 1U) % RX_QUEUE_SIZE);
        havePacket = true;
    }
    portEXIT_CRITICAL(&rxQueueMux);

    if (!havePacket) {
        serviceDiagnosticFrame();
        // 没有数据时主动让出 CPU，避免空转占满一个核心。
        delayMicroseconds(50);
        return;
    }

    // 将整包一次性打包成 PC 端原来就能解析的 JustFloat 字节流。
    // 这样比每个 sample 调两次 Serial.write 更高效。
    uint8_t serialBatch[SERIAL_BATCH_BYTES];
    size_t offset = 0;

    for (int i = 0; i < EMG_SAMPLES_PER_PACKET; i++) {
        float floatChannels[8];
        for (int ch = 0; ch < 8; ch++) {
            floatChannels[ch] = (float)localPacket.emg_data[i][ch];
        }

        memcpy(serialBatch + offset, floatChannels, sizeof(floatChannels));
        offset += sizeof(floatChannels);
        memcpy(serialBatch + offset, JUSTFLOAT_TAIL, sizeof(JUSTFLOAT_TAIL));
        offset += sizeof(JUSTFLOAT_TAIL);
    }

    // 关键修改：不再检查 availableForWrite() 后 break 丢样。
    // Arduino-ESP32 的 Serial.write(buf, len) 在缓冲暂时不足时会等待可写空间；
    // 配合上面的 ESP-NOW 环形队列，短时串口抖动不会直接造成采样点丢失。
    size_t written = 0;
    while (written < SERIAL_BATCH_BYTES) {
        const size_t n = Serial.write(serialBatch + written,
                                      SERIAL_BATCH_BYTES - written);
        if (n > 0) {
            written += n;
        } else {
            // 极少数情况下底层暂时返回 0，短暂让出 CPU 后继续写完整包。
            delayMicroseconds(50);
        }
    }

    serviceDiagnosticFrame();
}

#endif
