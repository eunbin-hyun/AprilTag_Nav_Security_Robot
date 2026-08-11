#include "motor.h"

#include "main.h"

#include <stdint.h>

extern TIM_HandleTypeDef htim5;

#define MOTOR_FORWARD_CHANNEL TIM_CHANNEL_1
#define MOTOR_REVERSE_CHANNEL TIM_CHANNEL_4
#define MOTOR_MIN_PERCENT     (-100.0f)
#define MOTOR_MAX_PERCENT     100.0f

static bool motor_initialized;
static bool motor_enabled;
static bool motor_emergency_disabled;
static bool motor_direction_inverted;
static float motor_applied_percent;

static uint32_t LockMotorState(void)
{
  uint32_t primask = __get_PRIMASK();

  __disable_irq();
  return primask;
}

static void UnlockMotorState(uint32_t primask)
{
  __set_PRIMASK(primask);
}

static float ClampPercent(float percent)
{
  if (percent > MOTOR_MAX_PERCENT)
  {
    return MOTOR_MAX_PERCENT;
  }

  if (percent < MOTOR_MIN_PERCENT)
  {
    return MOTOR_MIN_PERCENT;
  }

  return percent;
}

static uint32_t PercentToCompare(TIM_HandleTypeDef *timer,
                                 float magnitude_percent)
{
  uint32_t period_counts = __HAL_TIM_GET_AUTORELOAD(timer) + 1U;
  float compare = (magnitude_percent * (float)period_counts) / 100.0f;

  if (compare <= 0.0f)
  {
    return 0U;
  }

  if (compare >= (float)period_counts)
  {
    return period_counts;
  }

  return (uint32_t)(compare + 0.5f);
}

static void SetBothPwmChannelsToZero(void)
{
  __HAL_TIM_SET_COMPARE(&htim5, MOTOR_FORWARD_CHANNEL, 0U);
  __HAL_TIM_SET_COMPARE(&htim5, MOTOR_REVERSE_CHANNEL, 0U);
}

bool Motor_Init(void)
{
  bool emergency_disabled = motor_emergency_disabled;

  HAL_GPIO_WritePin(BTS_R_EN_GPIO_Port,
                    BTS_R_EN_Pin | BTS_L_EN_Pin,
                    GPIO_PIN_RESET);
  SetBothPwmChannelsToZero();

  if (HAL_TIM_PWM_Start(&htim5, MOTOR_FORWARD_CHANNEL) != HAL_OK)
  {
    return false;
  }

  if (HAL_TIM_PWM_Start(&htim5, MOTOR_REVERSE_CHANNEL) != HAL_OK)
  {
    (void)HAL_TIM_PWM_Stop(&htim5, MOTOR_FORWARD_CHANNEL);
    return false;
  }

  motor_initialized = true;
  motor_enabled = false;
  motor_emergency_disabled = emergency_disabled;
  motor_applied_percent = 0.0f;
  return true;
}

void Motor_Enable(void)
{
  uint32_t primask = LockMotorState();

  if (!motor_initialized || motor_emergency_disabled)
  {
    UnlockMotorState(primask);
    return;
  }

  HAL_GPIO_WritePin(BTS_R_EN_GPIO_Port,
                    BTS_R_EN_Pin | BTS_L_EN_Pin,
                    GPIO_PIN_SET);
  motor_enabled = true;
  UnlockMotorState(primask);
}

void Motor_Disable(void)
{
  uint32_t primask = LockMotorState();

  SetBothPwmChannelsToZero();
  HAL_GPIO_WritePin(BTS_R_EN_GPIO_Port,
                    BTS_R_EN_Pin | BTS_L_EN_Pin,
                    GPIO_PIN_RESET);
  motor_enabled = false;
  motor_applied_percent = 0.0f;
  UnlockMotorState(primask);
}

bool Motor_IsEnabled(void)
{
  bool enabled;
  uint32_t primask = LockMotorState();

  enabled = motor_enabled;
  UnlockMotorState(primask);
  return enabled;
}

void Motor_EmergencyDisable(void)
{
  uint32_t primask = LockMotorState();

  motor_emergency_disabled = true;
  SetBothPwmChannelsToZero();
  HAL_GPIO_WritePin(BTS_R_EN_GPIO_Port,
                    BTS_R_EN_Pin | BTS_L_EN_Pin,
                    GPIO_PIN_RESET);
  motor_enabled = false;
  motor_applied_percent = 0.0f;
  UnlockMotorState(primask);
}

bool Motor_ClearEmergencyDisable(void)
{
  bool cleared = false;
  uint32_t primask = LockMotorState();

  SetBothPwmChannelsToZero();
  HAL_GPIO_WritePin(BTS_R_EN_GPIO_Port,
                    BTS_R_EN_Pin | BTS_L_EN_Pin,
                    GPIO_PIN_RESET);
  motor_enabled = false;
  motor_applied_percent = 0.0f;

  if (motor_initialized)
  {
    motor_emergency_disabled = false;
    cleared = true;
  }

  UnlockMotorState(primask);
  return cleared;
}

bool Motor_IsEmergencyDisabled(void)
{
  bool emergency_disabled;
  uint32_t primask = LockMotorState();

  emergency_disabled = motor_emergency_disabled;
  UnlockMotorState(primask);
  return emergency_disabled;
}

void Motor_SetPercent(float percent)
{
  float requested_percent;
  float magnitude_percent;
  uint32_t compare;
  uint32_t primask = LockMotorState();

  if (!motor_initialized || !motor_enabled || motor_emergency_disabled)
  {
    SetBothPwmChannelsToZero();
    motor_applied_percent = 0.0f;
    UnlockMotorState(primask);
    return;
  }

  requested_percent = ClampPercent(percent);
  if (motor_direction_inverted)
  {
    requested_percent = -requested_percent;
  }

  /*
   * Clear both sides before selecting a direction. This prevents forward and
   * reverse PWM from being active at the same time during a direction change.
   */
  SetBothPwmChannelsToZero();

  if (requested_percent > 0.0f)
  {
    compare = PercentToCompare(&htim5, requested_percent);
    __HAL_TIM_SET_COMPARE(&htim5, MOTOR_FORWARD_CHANNEL, compare);
  }
  else if (requested_percent < 0.0f)
  {
    magnitude_percent = -requested_percent;
    compare = PercentToCompare(&htim5, magnitude_percent);
    __HAL_TIM_SET_COMPARE(&htim5, MOTOR_REVERSE_CHANNEL, compare);
  }

  motor_applied_percent =
      motor_direction_inverted ? -requested_percent : requested_percent;
  UnlockMotorState(primask);
}

float Motor_GetAppliedPercent(void)
{
  float applied_percent;
  uint32_t primask = LockMotorState();

  applied_percent = motor_applied_percent;
  UnlockMotorState(primask);
  return applied_percent;
}

void Motor_SetDirectionInverted(bool inverted)
{
  uint32_t primask = LockMotorState();

  motor_direction_inverted = inverted;
  UnlockMotorState(primask);
}
