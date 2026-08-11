#ifndef APP_SAFETY_H
#define APP_SAFETY_H

#ifdef __cplusplus
extern "C" {
#endif

#include "ultrasonic.h"

#include <stdbool.h>
#include <stdint.h>

#define SAFETY_STALE_TIMEOUT_MS       200U
#define SAFETY_STOP_CLEAR_SAMPLE_COUNT 3U

typedef enum
{
  SAFETY_LEVEL_CLEAR = 0,
  SAFETY_LEVEL_CAUTION,
  SAFETY_LEVEL_STOP,
  SAFETY_LEVEL_ESTOP_LATCHED
} SafetyLevel;

typedef enum
{
  SAFETY_REASON_NONE = 0U,
  SAFETY_REASON_DISTANCE_CAUTION = (1UL << 0),
  SAFETY_REASON_DISTANCE_STOP = (1UL << 1),
  SAFETY_REASON_SENSOR_INIT = (1UL << 3),
  SAFETY_REASON_SENSOR_TIMEOUT = (1UL << 4),
  SAFETY_REASON_SENSOR_OUT_OF_RANGE = (1UL << 5),
  SAFETY_REASON_SENSOR_STALE = (1UL << 6),
  SAFETY_REASON_REVERSE_UNPROTECTED = (1UL << 7),
  SAFETY_REASON_MANUAL_ESTOP = (1UL << 8),
  SAFETY_REASON_MOTOR_NOT_SAFE = (1UL << 9)
} SafetyReason;

typedef struct
{
  UltrasonicState ultrasonic;
  bool forward_requested;
  bool reverse_requested;
  bool vehicle_moving_forward;
  bool vehicle_moving_reverse;
  bool motor_output_zero;
  bool manual_estop;
} SafetyInput;

typedef struct
{
  SafetyLevel level;
  uint32_t reason;
  bool stop_request;
  bool emergency_disable;
  bool latched;
  bool block_reverse;
  uint32_t updated_at_ms;
  uint32_t loop_count;
} SafetyRequest;

typedef struct
{
  SafetyLevel level;
  uint32_t reason;
  uint32_t last_ultrasonic_sequence;
  uint8_t consecutive_clear_samples;
  bool ultrasonic_sequence_seen;
  bool latched;
} SafetyMachine;

void Safety_InitMachine(SafetyMachine *machine);
SafetyRequest Safety_Evaluate(SafetyMachine *machine,
                              const SafetyInput *input,
                              bool reset_requested,
                              uint32_t now_ms);
void Safety_ForceEmergencyStop(SafetyMachine *machine, uint32_t reason);

#ifdef __cplusplus
}
#endif

#endif
