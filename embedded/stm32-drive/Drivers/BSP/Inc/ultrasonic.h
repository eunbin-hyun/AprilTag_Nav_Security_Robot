#ifndef BSP_ULTRASONIC_H
#define BSP_ULTRASONIC_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdbool.h>
#include <stdint.h>

typedef enum
{
  ULTRA_STATUS_INIT = 0,
  ULTRA_STATUS_OK,
  ULTRA_STATUS_NO_ECHO,
  ULTRA_STATUS_TIMEOUT,
  ULTRA_STATUS_OUT_OF_RANGE,
  ULTRA_STATUS_STALE
} UltrasonicStatus;

typedef struct
{
  float distance_m;
  uint32_t pulse_us;
  uint32_t sample_time_ms;
  uint32_t sequence;
  UltrasonicStatus status;
} UltrasonicState;

typedef enum
{
  ULTRASONIC_WAIT_OK = 0,
  ULTRASONIC_WAIT_TIMEOUT,
  ULTRASONIC_WAIT_ERROR
} UltrasonicWaitResult;

bool Ultrasonic_Init(void);
bool Ultrasonic_StartMeasurement(void);
UltrasonicWaitResult Ultrasonic_WaitResult(uint32_t timeout_ms,
                                           uint32_t *pulse_us);

/* Called from HAL_TIM_IC_CaptureCallback() for TIM2 channel 2 only. */
void Ultrasonic_OnInputCapture(void);

#ifdef __cplusplus
}
#endif

#endif
