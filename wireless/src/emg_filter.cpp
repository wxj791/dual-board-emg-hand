#include "emg_filter.h"
#include <string.h>

// ------------------------------------------------------------------------------
// 49阶 FIR 48Hz 低通滤波器系数 (与原 STM32-EMG/Algorithm/FIR_48.c 完全一致)
// ------------------------------------------------------------------------------
const float EMG_FIRFilter::B[EMG_FIR_TAPS] = {
   -0.0009602747741f, -0.0007406222285f, -0.000344334956f,  0.0003267489665f,  0.00132522604f,
    0.002570599085f,   0.003790664719f,   0.004526928533f,   0.00422275858f,    0.002387276152f,
   -0.001199811231f,  -0.006298475899f,  -0.01209692098f,   -0.01723066717f,   -0.01995631494f,
   -0.01846508868f,   -0.01128097717f,    0.002341947285f,   0.02211954445f,    0.0466186665f,
    0.07336764783f,    0.09919441491f,    0.1207369491f,     0.1350278109f,     0.1400325894f,
    0.1350278109f,     0.1207369491f,     0.09919441491f,    0.07336764783f,    0.0466186665f,
    0.02211954445f,    0.002341947285f,  -0.01128097717f,   -0.01846508868f,   -0.01995631494f,
   -0.01723066717f,   -0.01209692098f,   -0.006298475899f,  -0.001199811231f,   0.002387276152f,
    0.00422275858f,    0.004526928533f,   0.003790664719f,   0.002570599085f,   0.00132522604f,
    0.0003267489665f, -0.000344334956f,  -0.0007406222285f, -0.0009602747741f
};

EMG_FIRFilter::EMG_FIRFilter() {
    reset();
}

void EMG_FIRFilter::reset() {
    memset(state, 0, sizeof(state));
    state_idx = 0;
}

// 环形缓冲卷积运算，无内存搬移，速度极快
float EMG_FIRFilter::update(float input) {
    state[state_idx] = input;
    
    float acc = 0.0f;
    int cur = state_idx;

    for (int i = 0; i < EMG_FIR_TAPS; i++) {
        acc += B[i] * state[cur];
        cur--;
        if (cur < 0) {
            cur = EMG_FIR_TAPS - 1;
        }
    }

    state_idx++;
    if (state_idx >= EMG_FIR_TAPS) {
        state_idx = 0;
    }

    return acc;
}

// ------------------------------------------------------------------------------
// 卡尔曼滤波器实现 (与原 STM32-EMG/USER/main.c 完全一致)
// ------------------------------------------------------------------------------
EMG_KalmanFilter::EMG_KalmanFilter(float q, float r) {
    Q = q;
    R = r;
    reset(1670.0f);
}

void EMG_KalmanFilter::reset(float initVal) {
    K = 0.0f;
    P = 1.0f;
    prevData = initVal;
}

float EMG_KalmanFilter::update(float Z) {
    P = P + Q;
    K = P / (P + R);
    Z = prevData + K * (Z - prevData);
    P = (1.0f - K) * P;
    prevData = Z;
    return Z;
}
