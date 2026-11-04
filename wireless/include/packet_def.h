#pragma once
#include <Arduino.h>

// ESP-NOW 通信固定信道 (1~13，两端必须保持一致)
#define ESPNOW_WIFI_CHANNEL 1

// 超时判断门限 (ms)
#define CONNECTION_TIMEOUT_MS 1000

// 每包批量打包的采样点数 (4次采样 = 2ms数据，2kHz 采样率下对应 500Hz 发送频率)
#define EMG_SAMPLES_PER_PACKET 4

#pragma pack(push, 1)

// 针对 2kHz 实时肌电波形的高速批量传输帧 (总大小约 180 字节，远在 250 字节上限内)
typedef struct {
    uint32_t packet_id;        // 数据包自增序号
    uint32_t timestamp_ms;     // 手环时间戳
    
    // 陀螺仪当前欧拉角 (5ms 刷新一次)
    float roll;
    float pitch;
    float yaw;

    // 10 组 8 通道已滤波的肌电采样值 (10 * 8 * 2 = 160 字节)
    uint16_t emg_data[EMG_SAMPLES_PER_PACKET][8];

    uint8_t sensor_online;     // 陀螺仪在线状态
} EmgBatchPacket;

#pragma pack(pop)
