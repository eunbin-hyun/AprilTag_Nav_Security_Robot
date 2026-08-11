#include "encoder.h"

#include "main.h"

#include <string.h>

extern TIM_HandleTypeDef htim4;

static bool encoder_initialized;
static uint16_t previous_counter;
static int64_t total_count;
static float counts_per_wheel_revolution =
    ENCODER_DEFAULT_COUNTS_PER_WHEEL_REV;
static float wheel_circumference_m =
    ENCODER_DEFAULT_WHEEL_CIRCUMFERENCE_M;
static int8_t encoder_direction_sign = 1;
static EncoderSample last_sample;

bool Encoder_Init(void)
{
  if (HAL_TIM_Encoder_Start(&htim4, TIM_CHANNEL_ALL) != HAL_OK)
  {
    return false;
  }

  previous_counter = (uint16_t)__HAL_TIM_GET_COUNTER(&htim4);
  total_count = 0;
  memset(&last_sample, 0, sizeof(last_sample));
  last_sample.calibrated = Encoder_IsCalibrated();
  encoder_initialized = true;
  return true;
}

void Encoder_Reset(void)
{
  __HAL_TIM_SET_COUNTER(&htim4, 0U);
  previous_counter = 0U;
  total_count = 0;
  memset(&last_sample, 0, sizeof(last_sample));
  last_sample.calibrated = Encoder_IsCalibrated();
}

void Encoder_Update(float dt_seconds, EncoderSample *sample)
{
  uint16_t current_counter;
  int16_t wrapped_delta;
  int32_t signed_delta;
  bool calibrated;

  if (sample == NULL)
  {
    return;
  }

  if (!encoder_initialized)
  {
    memset(sample, 0, sizeof(*sample));
    return;
  }

  current_counter = (uint16_t)__HAL_TIM_GET_COUNTER(&htim4);

  /*
   * TIM4 is 16-bit. Casting the unsigned subtraction to int16_t preserves
   * a small forward/reverse delta even when CNT wraps at 0 or 65535.
   */
  wrapped_delta =
      (int16_t)(uint16_t)(current_counter - previous_counter);
  previous_counter = current_counter;
  signed_delta = (int32_t)wrapped_delta * (int32_t)encoder_direction_sign;
  total_count += signed_delta;

  calibrated = Encoder_IsCalibrated();
  last_sample.delta_count = signed_delta;
  last_sample.total_count = total_count;
  last_sample.delta_distance_m = 0.0f;
  last_sample.total_distance_m = 0.0f;
  last_sample.calibrated = calibrated;
  last_sample.speed_mps = 0.0f;

  if (calibrated)
  {
    float delta_revolutions =
        (float)signed_delta / counts_per_wheel_revolution;
    float total_revolutions =
        (float)total_count / counts_per_wheel_revolution;

    last_sample.delta_distance_m =
        delta_revolutions * wheel_circumference_m;
    last_sample.total_distance_m =
        total_revolutions * wheel_circumference_m;
    if (dt_seconds > 0.0f)
    {
      last_sample.speed_mps =
          last_sample.delta_distance_m / dt_seconds;
    }
  }

  *sample = last_sample;
}

bool Encoder_SetCalibration(float counts_per_revolution,
                            float circumference_m)
{
  if ((counts_per_revolution <= 0.0f) || (circumference_m <= 0.0f))
  {
    counts_per_wheel_revolution = 0.0f;
    wheel_circumference_m = 0.0f;
    return false;
  }

  counts_per_wheel_revolution = counts_per_revolution;
  wheel_circumference_m = circumference_m;
  return true;
}

bool Encoder_IsCalibrated(void)
{
  return (counts_per_wheel_revolution > 0.0f) &&
         (wheel_circumference_m > 0.0f);
}

void Encoder_SetDirectionSign(int8_t direction_sign)
{
  encoder_direction_sign = (direction_sign < 0) ? -1 : 1;
}
