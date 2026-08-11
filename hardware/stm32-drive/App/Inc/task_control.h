#ifndef APP_TASK_CONTROL_H
#define APP_TASK_CONTROL_H

#ifdef __cplusplus
extern "C" {
#endif

#include "safety.h"

#include <stdbool.h>
#include <stdint.h>

typedef enum
{
  CONTROL_MODE_DISABLED = 0,
  CONTROL_MODE_OPEN_LOOP,
  CONTROL_MODE_SPEED_PID
} ControlMode;

typedef enum
{
  CONTROL_FAULT_NONE = 0U,
  CONTROL_FAULT_HARDWARE_INIT = (1UL << 0),
  CONTROL_FAULT_ENCODER_NOT_CALIBRATED = (1UL << 1),
  CONTROL_FAULT_STEERING_NOT_CALIBRATED = (1UL << 2),
  CONTROL_FAULT_STEERING_SENSOR_INVALID = (1UL << 3),
  CONTROL_FAULT_STEERING_SERVO_INVALID = (1UL << 4)
} ControlFault;

typedef struct
{
  ControlMode mode;
  float pwm_percent;
  float target_speed_mps;
  float target_steering_deg;
} ControlCommand;

typedef struct
{
  ControlMode mode;
  float target_speed_mps;
  float target_steering_deg;
  int32_t encoder_diff;
  int64_t encoder_total_count;
  float encoder_delta_distance_m;
  float encoder_total_distance_m;
  float measured_speed_mps;
  uint16_t steering_adc_raw;
  float measured_steering_deg;
  bool steering_adc_valid;
  bool steering_angle_valid;
  bool steering_sensor_calibrated;
  bool steering_servo_calibrated;
  uint16_t steering_servo_diagnostic_pulse_us;
  bool steering_servo_diagnostic_active;
  float applied_pwm_percent;
  bool encoder_calibrated;
  uint32_t fault_flags;
  uint32_t updated_at_ms;
} ControlState;

void Control_Init(void);
void Control_Task(void *argument);

void Control_SetCommand(const ControlCommand *command);
void Control_SetDisabled(void);
void Control_SetOpenLoopPercent(float pwm_percent);
void Control_SetSpeedTarget(float target_speed_mps,
                            float target_steering_deg);
void Control_GetCommand(ControlCommand *command);
void Control_GetState(ControlState *state);
void Control_SetSafetyRequest(const SafetyRequest *request);

bool Control_SetEncoderCalibration(float counts_per_wheel_revolution,
                                   float wheel_circumference_m);
bool Control_SetSteeringSensorCalibration(uint16_t left_raw,
                                          uint16_t center_raw,
                                          uint16_t right_raw,
                                          float left_angle_deg,
                                          float right_angle_deg);
bool Control_SetSteeringServoCalibration(float left_angle_deg,
                                         uint32_t left_pulse_us,
                                         float center_angle_deg,
                                         uint32_t center_pulse_us,
                                         float right_angle_deg,
                                         uint32_t right_pulse_us);
bool Control_SetSteeringServoDiagnosticPulse(uint16_t pulse_us);
void Control_SetEncoderDirectionSign(int8_t direction_sign);
void Control_SetMotorDirectionInverted(bool inverted);
void Control_SetPidGains(float kp, float ki, float kd);
void Control_ClearFaults(uint32_t fault_mask);

#ifdef __cplusplus
}
#endif

#endif
