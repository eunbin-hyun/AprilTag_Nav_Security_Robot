#include "ultrasonic.h"

#include "cmsis_os2.h"
#include "main.h"

extern TIM_HandleTypeDef htim2;

#define ULTRASONIC_CAPTURE_CHANNEL TIM_CHANNEL_2
#define ULTRASONIC_TRIGGER_US      10U

typedef enum
{
  ULTRASONIC_EDGE_IDLE = 0,
  ULTRASONIC_EDGE_WAIT_RISING,
  ULTRASONIC_EDGE_WAIT_FALLING
} UltrasonicEdgeState;

static osSemaphoreId_t result_semaphore;
static volatile UltrasonicEdgeState edge_state;
static volatile bool measurement_active;
static volatile bool pulse_ready;
static volatile uint32_t rising_capture;
static volatile uint32_t captured_pulse_us;

static uint32_t MillisecondsToKernelTicks(uint32_t milliseconds)
{
  uint32_t tick_frequency = osKernelGetTickFreq();
  uint64_t ticks =
      ((uint64_t)milliseconds * (uint64_t)tick_frequency + 999ULL) /
      1000ULL;

  if ((milliseconds > 0U) && (ticks == 0ULL))
  {
    ticks = 1ULL;
  }

  if (ticks > UINT32_MAX)
  {
    return UINT32_MAX;
  }

  return (uint32_t)ticks;
}

static void SetCapturePolarity(uint32_t polarity)
{
  __HAL_TIM_SET_CAPTUREPOLARITY(&htim2,
                                ULTRASONIC_CAPTURE_CHANNEL,
                                polarity);
}

static void CancelMeasurement(void)
{
  uint32_t primask = __get_PRIMASK();

  __disable_irq();
  measurement_active = false;
  pulse_ready = false;
  edge_state = ULTRASONIC_EDGE_IDLE;
  SetCapturePolarity(TIM_INPUTCHANNELPOLARITY_RISING);
  __HAL_TIM_CLEAR_FLAG(&htim2, TIM_FLAG_CC2);
  __set_PRIMASK(primask);
}

bool Ultrasonic_Init(void)
{
  const osSemaphoreAttr_t semaphore_attributes = {
    .name = "UltraResult",
  };

  HAL_GPIO_WritePin(ULTRASONIC_TRIG_GPIO_Port,
                    ULTRASONIC_TRIG_Pin,
                    GPIO_PIN_RESET);

  edge_state = ULTRASONIC_EDGE_IDLE;
  measurement_active = false;
  pulse_ready = false;
  rising_capture = 0U;
  captured_pulse_us = 0U;

  result_semaphore =
      osSemaphoreNew(1U, 0U, &semaphore_attributes);
  if (result_semaphore == NULL)
  {
    return false;
  }

  SetCapturePolarity(TIM_INPUTCHANNELPOLARITY_RISING);
  __HAL_TIM_CLEAR_FLAG(&htim2, TIM_FLAG_CC2);
  return HAL_TIM_IC_Start_IT(&htim2,
                             ULTRASONIC_CAPTURE_CHANNEL) == HAL_OK;
}

bool Ultrasonic_StartMeasurement(void)
{
  uint32_t start_count;
  uint32_t primask;

  if (result_semaphore == NULL)
  {
    return false;
  }

  while (osSemaphoreAcquire(result_semaphore, 0U) == osOK)
  {
    /* Drain a late completion token before starting the next measurement. */
  }

  primask = __get_PRIMASK();
  __disable_irq();
  if (measurement_active)
  {
    __set_PRIMASK(primask);
    return false;
  }

  pulse_ready = false;
  captured_pulse_us = 0U;
  edge_state = ULTRASONIC_EDGE_WAIT_RISING;
  measurement_active = true;
  SetCapturePolarity(TIM_INPUTCHANNELPOLARITY_RISING);
  __HAL_TIM_CLEAR_FLAG(&htim2, TIM_FLAG_CC2);
  __set_PRIMASK(primask);

  HAL_GPIO_WritePin(ULTRASONIC_TRIG_GPIO_Port,
                    ULTRASONIC_TRIG_Pin,
                    GPIO_PIN_SET);
  start_count = __HAL_TIM_GET_COUNTER(&htim2);
  while ((uint32_t)(__HAL_TIM_GET_COUNTER(&htim2) - start_count) <
         ULTRASONIC_TRIGGER_US)
  {
    /* The HC-SR04 requires a minimum 10 us TRIG pulse. */
  }
  HAL_GPIO_WritePin(ULTRASONIC_TRIG_GPIO_Port,
                    ULTRASONIC_TRIG_Pin,
                    GPIO_PIN_RESET);

  return true;
}

UltrasonicWaitResult Ultrasonic_WaitResult(uint32_t timeout_ms,
                                           uint32_t *pulse_us)
{
  osStatus_t wait_status;
  uint32_t primask;

  if ((result_semaphore == NULL) || (pulse_us == NULL))
  {
    return ULTRASONIC_WAIT_ERROR;
  }

  wait_status = osSemaphoreAcquire(
      result_semaphore,
      MillisecondsToKernelTicks(timeout_ms));
  if (wait_status != osOK)
  {
    CancelMeasurement();
    return (wait_status == osErrorTimeout) ?
           ULTRASONIC_WAIT_TIMEOUT :
           ULTRASONIC_WAIT_ERROR;
  }

  primask = __get_PRIMASK();
  __disable_irq();
  if (!pulse_ready)
  {
    __set_PRIMASK(primask);
    return ULTRASONIC_WAIT_ERROR;
  }

  *pulse_us = captured_pulse_us;
  pulse_ready = false;
  __set_PRIMASK(primask);
  return ULTRASONIC_WAIT_OK;
}

void Ultrasonic_OnInputCapture(void)
{
  uint32_t capture;

  if (!measurement_active)
  {
    return;
  }

  capture = HAL_TIM_ReadCapturedValue(&htim2,
                                      ULTRASONIC_CAPTURE_CHANNEL);

  if (edge_state == ULTRASONIC_EDGE_WAIT_RISING)
  {
    rising_capture = capture;
    edge_state = ULTRASONIC_EDGE_WAIT_FALLING;
    SetCapturePolarity(TIM_INPUTCHANNELPOLARITY_FALLING);
    return;
  }

  if (edge_state == ULTRASONIC_EDGE_WAIT_FALLING)
  {
    captured_pulse_us = capture - rising_capture;
    pulse_ready = true;
    measurement_active = false;
    edge_state = ULTRASONIC_EDGE_IDLE;
    SetCapturePolarity(TIM_INPUTCHANNELPOLARITY_RISING);
    (void)osSemaphoreRelease(result_semaphore);
  }
}
