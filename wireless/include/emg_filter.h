#pragma once
#include <Arduino.h>

// ==============================================================================
// 1. 49阶 FIR 48Hz 低通滤波器 (移植自原 STM32-EMG/Algorithm/FIR_48.c)
// ==============================================================================
#define EMG_FIR_TAPS 49

class EMG_FIRFilter {
private:
    float state[EMG_FIR_TAPS];
    uint8_t state_idx;

    // 原工程 49 个系数
    static const float B[EMG_FIR_TAPS];

public:
    EMG_FIRFilter();
    void reset();
    float update(float input);
};

// ==============================================================================
// 2. 卡尔曼滤波器 (移植自原 STM32-EMG/USER/main.c)
// ==============================================================================
class EMG_KalmanFilter {
private:
    float K;
    float P;
    float Q;
    float R;
    float prevData;

public:
    EMG_KalmanFilter(float q = 0.0025f, float r = 0.08f);
    void reset(float initVal = 1670.0f);
    float update(float Z);
};
