#ifndef BSP_STEERING_SENSOR_H
#define BSP_STEERING_SENSOR_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdbool.h>
#include <stdint.h>

#define STEERING_SERVO_DIAGNOSTIC_MIN_PULSE_US 766U
#define STEERING_SERVO_DIAGNOSTIC_MAX_PULSE_US 1696U

typedef struct
{
  uint16_t left_raw;
  uint16_t center_raw;
  uint16_t right_raw;
  /* Vehicle convention: left/CCW is positive, right/CW is negative. */
  float left_angle_deg;
  float right_angle_deg;
  bool valid;
} SteeringSensorCalibration;

typedef struct
{
  uint16_t raw;
  float angle_deg;
  bool adc_valid;
  bool angle_valid;
  uint32_t sampled_at_ms;
} SteeringSensorSample;

typedef struct
{
  /* Vehicle convention: left/CCW is positive, right/CW is negative. */
  float angle_deg;
  uint32_t pulse_us;
} SteeringServoLutPoint;

bool SteeringSensor_Init(void);
bool SteeringSensor_Read(SteeringSensorSample *sample);
bool SteeringSensor_SetCalibration(uint16_t left_raw,
                                   uint16_t center_raw,
                                   uint16_t right_raw,
                                   float left_angle_deg,
                                   float right_angle_deg);
bool SteeringSensor_IsCalibrated(void);
bool SteeringSensor_AdcToAngle(uint16_t adc_raw, float *angle_deg);

bool SteeringServo_SetCalibration(float left_angle_deg,
                                  uint32_t left_pulse_us,
                                  float center_angle_deg,
                                  uint32_t center_pulse_us,
                                  float right_angle_deg,
                                  uint32_t right_pulse_us);
bool SteeringServo_SetLutCalibration(
    const SteeringServoLutPoint *points,
    uint32_t point_count);
bool SteeringServo_IsCalibrated(void);
bool SteeringServo_AngleToPulseUs(float angle_deg, uint32_t *pulse_us);
bool SteeringServo_SetAngle(float angle_deg);
bool SteeringServo_Start(void);
bool SteeringServo_SetDiagnosticPulseUs(uint32_t pulse_us);
void SteeringServo_Stop(void);

#ifdef __cplusplus
}
#endif

#endif
