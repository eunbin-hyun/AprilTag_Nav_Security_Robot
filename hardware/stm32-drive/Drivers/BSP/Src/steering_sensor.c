#include "steering_sensor.h"

#include "main.h"

#include <math.h>

extern ADC_HandleTypeDef hadc1;
extern TIM_HandleTypeDef htim3;

#define STEERING_ADC_AVERAGE_SAMPLES 8U
#define STEERING_ADC_TIMEOUT_MS      2U
#define STEERING_ADC_MAX_RAW         4095U
#define STEERING_ADC_MIN_SPAN        16
#define STEERING_ADC_OUTSIDE_MARGIN  32
#define SERVO_DEFAULT_CENTER_PULSE_US 1231U
#define SERVO_MIN_ALLOWED_PULSE_US    500U
#define SERVO_MAX_ALLOWED_PULSE_US    2500U
#define SERVO_ZERO_ANGLE_EPSILON_DEG  0.001f
#define SERVO_MAX_LUT_POINTS          16U

static SteeringSensorCalibration sensor_calibration;
static bool sensor_initialized;
static bool servo_calibrated;
static bool servo_started;
static SteeringServoLutPoint servo_lut[SERVO_MAX_LUT_POINTS];
static uint32_t servo_lut_point_count;
static uint32_t servo_center_pulse_us =
    SERVO_DEFAULT_CENTER_PULSE_US;

static float ClampFloat(float value, float minimum, float maximum)
{
  if (value < minimum)
  {
    return minimum;
  }

  if (value > maximum)
  {
    return maximum;
  }

  return value;
}

static bool IsNearlyZero(float value)
{
  return (value > -SERVO_ZERO_ANGLE_EPSILON_DEG) &&
         (value < SERVO_ZERO_ANGLE_EPSILON_DEG);
}

bool SteeringSensor_Init(void)
{
  sensor_initialized = hadc1.Instance == ADC1;
  return sensor_initialized;
}

bool SteeringSensor_Read(SteeringSensorSample *sample)
{
  uint32_t sum = 0U;
  uint32_t index;
  uint16_t averaged_raw;

  if (sample == NULL)
  {
    return false;
  }

  sample->raw = 0U;
  sample->angle_deg = 0.0f;
  sample->adc_valid = false;
  sample->angle_valid = false;
  sample->sampled_at_ms = HAL_GetTick();

  if (!sensor_initialized)
  {
    return false;
  }

  for (index = 0U; index < STEERING_ADC_AVERAGE_SAMPLES; index++)
  {
    if (HAL_ADC_Start(&hadc1) != HAL_OK)
    {
      (void)HAL_ADC_Stop(&hadc1);
      return false;
    }

    if (HAL_ADC_PollForConversion(&hadc1,
                                  STEERING_ADC_TIMEOUT_MS) != HAL_OK)
    {
      (void)HAL_ADC_Stop(&hadc1);
      return false;
    }

    sum += HAL_ADC_GetValue(&hadc1);
    if (HAL_ADC_Stop(&hadc1) != HAL_OK)
    {
      return false;
    }
  }

  averaged_raw =
      (uint16_t)((sum + (STEERING_ADC_AVERAGE_SAMPLES / 2U)) /
                 STEERING_ADC_AVERAGE_SAMPLES);
  sample->raw = averaged_raw;
  sample->adc_valid = true;
  sample->angle_valid =
      SteeringSensor_AdcToAngle(averaged_raw, &sample->angle_deg);
  sample->sampled_at_ms = HAL_GetTick();
  return true;
}

bool SteeringSensor_SetCalibration(uint16_t left_raw,
                                   uint16_t center_raw,
                                   uint16_t right_raw,
                                   float left_angle_deg,
                                   float right_angle_deg)
{
  bool raw_order_valid;

  sensor_calibration.left_raw = left_raw;
  sensor_calibration.center_raw = center_raw;
  sensor_calibration.right_raw = right_raw;
  sensor_calibration.left_angle_deg = left_angle_deg;
  sensor_calibration.right_angle_deg = right_angle_deg;

  raw_order_valid =
      ((left_raw < center_raw) && (center_raw < right_raw)) ||
      ((right_raw < center_raw) && (center_raw < left_raw));
  sensor_calibration.valid =
      (left_raw <= STEERING_ADC_MAX_RAW) &&
      (center_raw <= STEERING_ADC_MAX_RAW) &&
      (right_raw <= STEERING_ADC_MAX_RAW) &&
      raw_order_valid &&
      (((int32_t)left_raw - (int32_t)center_raw >=
        STEERING_ADC_MIN_SPAN) ||
       ((int32_t)center_raw - (int32_t)left_raw >=
        STEERING_ADC_MIN_SPAN)) &&
      (((int32_t)right_raw - (int32_t)center_raw >=
        STEERING_ADC_MIN_SPAN) ||
       ((int32_t)center_raw - (int32_t)right_raw >=
        STEERING_ADC_MIN_SPAN)) &&
      (left_angle_deg > 0.0f) &&
      (right_angle_deg < 0.0f);
  return sensor_calibration.valid;
}

bool SteeringSensor_IsCalibrated(void)
{
  return sensor_calibration.valid;
}

bool SteeringSensor_AdcToAngle(uint16_t adc_raw, float *angle_deg)
{
  int32_t raw_offset;
  int32_t raw_span;
  int32_t raw_min;
  int32_t raw_max;
  float result;

  if ((angle_deg == NULL) || !sensor_calibration.valid)
  {
    return false;
  }

  raw_min = sensor_calibration.left_raw;
  if ((int32_t)sensor_calibration.right_raw < raw_min)
  {
    raw_min = sensor_calibration.right_raw;
  }
  raw_max = sensor_calibration.left_raw;
  if ((int32_t)sensor_calibration.right_raw > raw_max)
  {
    raw_max = sensor_calibration.right_raw;
  }

  if (((int32_t)adc_raw <
       (raw_min - STEERING_ADC_OUTSIDE_MARGIN)) ||
      ((int32_t)adc_raw >
       (raw_max + STEERING_ADC_OUTSIDE_MARGIN)))
  {
    return false;
  }

  if (((sensor_calibration.left_raw < sensor_calibration.center_raw) &&
       (adc_raw <= sensor_calibration.center_raw)) ||
      ((sensor_calibration.left_raw > sensor_calibration.center_raw) &&
       (adc_raw >= sensor_calibration.center_raw)))
  {
    raw_offset =
        (int32_t)adc_raw - (int32_t)sensor_calibration.center_raw;
    raw_span =
        (int32_t)sensor_calibration.left_raw -
        (int32_t)sensor_calibration.center_raw;
    result = ((float)raw_offset / (float)raw_span) *
             sensor_calibration.left_angle_deg;
    *angle_deg = ClampFloat(result,
                            0.0f,
                            sensor_calibration.left_angle_deg);
  }

  else
  {
    raw_offset =
        (int32_t)adc_raw - (int32_t)sensor_calibration.center_raw;
    raw_span =
        (int32_t)sensor_calibration.right_raw -
        (int32_t)sensor_calibration.center_raw;
    result = ((float)raw_offset / (float)raw_span) *
             sensor_calibration.right_angle_deg;
    *angle_deg = ClampFloat(result,
                            sensor_calibration.right_angle_deg,
                            0.0f);
  }

  return true;
}

bool SteeringServo_SetCalibration(float left_angle_deg,
                                  uint32_t left_pulse_us,
                                  float center_angle_deg,
                                  uint32_t center_pulse_us,
                                  float right_angle_deg,
                                  uint32_t right_pulse_us)
{
  const SteeringServoLutPoint points[] = {
    {left_angle_deg, left_pulse_us},
    {center_angle_deg, center_pulse_us},
    {right_angle_deg, right_pulse_us},
  };

  return SteeringServo_SetLutCalibration(
      points,
      (uint32_t)(sizeof(points) / sizeof(points[0])));
}

bool SteeringServo_SetLutCalibration(
    const SteeringServoLutPoint *points,
    uint32_t point_count)
{
  bool center_found = false;
  bool pulse_increasing;
  uint32_t index;

  servo_calibrated = false;
  servo_lut_point_count = 0U;

  if ((points == NULL) ||
      (point_count < 3U) ||
      (point_count > SERVO_MAX_LUT_POINTS))
  {
    return false;
  }

  pulse_increasing = points[1].pulse_us > points[0].pulse_us;
  if (points[1].pulse_us == points[0].pulse_us)
  {
    return false;
  }

  for (index = 0U; index < point_count; index++)
  {
    if (!isfinite(points[index].angle_deg) ||
        (points[index].pulse_us < SERVO_MIN_ALLOWED_PULSE_US) ||
        (points[index].pulse_us > SERVO_MAX_ALLOWED_PULSE_US))
    {
      return false;
    }

    if (index > 0U)
    {
      if (points[index - 1U].angle_deg <= points[index].angle_deg)
      {
        return false;
      }

      if ((pulse_increasing &&
           (points[index - 1U].pulse_us >= points[index].pulse_us)) ||
          (!pulse_increasing &&
           (points[index - 1U].pulse_us <= points[index].pulse_us)))
      {
        return false;
      }
    }

    if (IsNearlyZero(points[index].angle_deg))
    {
      if (center_found)
      {
        return false;
      }
      servo_center_pulse_us = points[index].pulse_us;
      center_found = true;
    }

    servo_lut[index] = points[index];
  }

  if (!center_found)
  {
    return false;
  }

  servo_lut_point_count = point_count;
  servo_calibrated = true;
  return true;
}

bool SteeringServo_IsCalibrated(void)
{
  return servo_calibrated;
}

bool SteeringServo_AngleToPulseUs(float angle_deg, uint32_t *pulse_us)
{
  uint32_t index;
  float ratio;
  float pulse;

  if ((pulse_us == NULL) || !isfinite(angle_deg))
  {
    return false;
  }

  if (!servo_calibrated)
  {
    if (!IsNearlyZero(angle_deg))
    {
      return false;
    }

    *pulse_us = servo_center_pulse_us;
    return true;
  }

  if (angle_deg >= servo_lut[0].angle_deg)
  {
    *pulse_us = servo_lut[0].pulse_us;
    return true;
  }

  if (angle_deg <=
      servo_lut[servo_lut_point_count - 1U].angle_deg)
  {
    *pulse_us =
        servo_lut[servo_lut_point_count - 1U].pulse_us;
    return true;
  }

  for (index = 0U;
       index < (servo_lut_point_count - 1U);
       index++)
  {
    if ((angle_deg <= servo_lut[index].angle_deg) &&
        (angle_deg >= servo_lut[index + 1U].angle_deg))
    {
      ratio =
          (angle_deg - servo_lut[index].angle_deg) /
          (servo_lut[index + 1U].angle_deg -
           servo_lut[index].angle_deg);
      pulse =
          (float)servo_lut[index].pulse_us +
          ratio *
              ((float)servo_lut[index + 1U].pulse_us -
               (float)servo_lut[index].pulse_us);
      *pulse_us = (uint32_t)(pulse + 0.5f);
      return true;
    }
  }

  return false;
}

bool SteeringServo_SetAngle(float angle_deg)
{
  uint32_t pulse_us;

  if (!SteeringServo_AngleToPulseUs(angle_deg, &pulse_us))
  {
    __HAL_TIM_SET_COMPARE(&htim3,
                          TIM_CHANNEL_1,
                          servo_center_pulse_us);
    return false;
  }

  __HAL_TIM_SET_COMPARE(&htim3,
                        TIM_CHANNEL_1,
                        pulse_us);
  return true;
}

bool SteeringServo_Start(void)
{
  /*
   * Do not energize the steering actuator from an assumed 1500 us neutral.
   * The assembled linkage may not be centered there. PWM is enabled only
   * after the vehicle-specific pulse/angle calibration has been accepted.
   */
  if (!servo_calibrated)
  {
    servo_started = false;
    return true;
  }

  __HAL_TIM_SET_COMPARE(&htim3,
                        TIM_CHANNEL_1,
                        servo_center_pulse_us);
  servo_started =
      HAL_TIM_PWM_Start(&htim3, TIM_CHANNEL_1) == HAL_OK;
  return servo_started;
}

bool SteeringServo_SetDiagnosticPulseUs(uint32_t pulse_us)
{
  if ((pulse_us < STEERING_SERVO_DIAGNOSTIC_MIN_PULSE_US) ||
      (pulse_us > STEERING_SERVO_DIAGNOSTIC_MAX_PULSE_US))
  {
    return false;
  }

  __HAL_TIM_SET_COMPARE(&htim3, TIM_CHANNEL_1, pulse_us);
  if (!servo_started)
  {
    servo_started =
        HAL_TIM_PWM_Start(&htim3, TIM_CHANNEL_1) == HAL_OK;
  }

  return servo_started;
}

void SteeringServo_Stop(void)
{
  (void)HAL_TIM_PWM_Stop(&htim3, TIM_CHANNEL_1);
  servo_started = false;
}
