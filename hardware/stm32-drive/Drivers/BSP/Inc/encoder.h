#ifndef BSP_ENCODER_H
#define BSP_ENCODER_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdbool.h>
#include <stdint.h>

/*
 * Vehicle wheel calibration measured in both directions:
 *   - forward: +2473 counts / 3 wheel revolutions = 824.33 counts/rev
 *   - reverse: -2464 counts / 3 wheel revolutions = 821.33 counts/rev
 *   - bidirectional average = 822.83 counts/rev, rounded to 823 counts/rev
 *
 * The 64 mm nominal tire diameter gives pi * 0.064 m = 0.20106 m.
 * Verify the rolling circumference over a measured floor distance under load.
 */
#define ENCODER_DEFAULT_COUNTS_PER_WHEEL_REV 823.0f
#define ENCODER_DEFAULT_WHEEL_CIRCUMFERENCE_M 0.20106f

typedef struct
{
  int32_t delta_count;
  int64_t total_count;
  float delta_distance_m;
  float total_distance_m;
  float speed_mps;
  bool calibrated;
} EncoderSample;

bool Encoder_Init(void);
void Encoder_Reset(void);
void Encoder_Update(float dt_seconds, EncoderSample *sample);

bool Encoder_SetCalibration(float counts_per_wheel_revolution,
                            float wheel_circumference_m);
bool Encoder_IsCalibrated(void);

/* Use +1 normally or -1 if forward movement produces negative counts. */
void Encoder_SetDirectionSign(int8_t direction_sign);

#ifdef __cplusplus
}
#endif

#endif
