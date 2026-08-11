#include "task_ultrasonic.h"

#include "cmsis_os2.h"
#include "main.h"

#include <string.h>

#define ULTRASONIC_TASK_PERIOD_MS  60U
#define ULTRASONIC_ECHO_TIMEOUT_MS 30U
#define ULTRASONIC_MIN_VALID_M     0.03f
#define ULTRASONIC_MAX_VALID_M     4.00f
#define ULTRASONIC_METERS_PER_US   0.0001715f
#define ULTRASONIC_FILTER_SIZE     3U

static osMutexId_t state_mutex;
static UltrasonicState shared_state;

static float Median(const float *values, uint32_t count)
{
  float sorted[ULTRASONIC_FILTER_SIZE];
  uint32_t i;
  uint32_t j;

  for (i = 0U; i < count; i++)
  {
    sorted[i] = values[i];
  }

  for (i = 1U; i < count; i++)
  {
    float value = sorted[i];

    j = i;
    while ((j > 0U) && (sorted[j - 1U] > value))
    {
      sorted[j] = sorted[j - 1U];
      j--;
    }
    sorted[j] = value;
  }

  if (count == 0U)
  {
    return 0.0f;
  }

  if ((count & 1U) != 0U)
  {
    return sorted[count / 2U];
  }

  return (sorted[(count / 2U) - 1U] + sorted[count / 2U]) * 0.5f;
}

static void StoreState(const UltrasonicState *state)
{
  if ((state == NULL) || (state_mutex == NULL))
  {
    return;
  }

  if (osMutexAcquire(state_mutex, osWaitForever) == osOK)
  {
    shared_state = *state;
    (void)osMutexRelease(state_mutex);
  }
}

bool UltrasonicTask_Init(void)
{
  const osMutexAttr_t mutex_attributes = {
    .name = "UltraState",
  };

  memset(&shared_state, 0, sizeof(shared_state));
  shared_state.status = ULTRA_STATUS_INIT;

  state_mutex = osMutexNew(&mutex_attributes);
  if (state_mutex == NULL)
  {
    return false;
  }

  return Ultrasonic_Init();
}

void Ultrasonic_Task(void *argument)
{
  UltrasonicState state;
  float valid_history[ULTRASONIC_FILTER_SIZE] = {0.0f};
  uint32_t valid_count = 0U;
  uint32_t history_index = 0U;
  uint32_t next_wake_tick;

  (void)argument;
  memset(&state, 0, sizeof(state));
  state.status = ULTRA_STATUS_INIT;
  StoreState(&state);

  next_wake_tick = osKernelGetTickCount();
  for (;;)
  {
    uint32_t pulse_us = 0U;
    UltrasonicWaitResult wait_result;

    state.sequence++;
    state.pulse_us = 0U;

    if (!Ultrasonic_StartMeasurement())
    {
      state.status = ULTRA_STATUS_TIMEOUT;
    }
    else
    {
      wait_result =
          Ultrasonic_WaitResult(ULTRASONIC_ECHO_TIMEOUT_MS, &pulse_us);
      if (wait_result == ULTRASONIC_WAIT_OK)
      {
        float raw_distance_m =
            (float)pulse_us * ULTRASONIC_METERS_PER_US;

        state.pulse_us = pulse_us;
        state.distance_m = raw_distance_m;
        if (raw_distance_m < ULTRASONIC_MIN_VALID_M)
        {
          state.status = ULTRA_STATUS_OUT_OF_RANGE;
        }
        else if (raw_distance_m > ULTRASONIC_MAX_VALID_M)
        {
          state.distance_m = ULTRASONIC_MAX_VALID_M;
          state.sample_time_ms = HAL_GetTick();
          state.status = ULTRA_STATUS_NO_ECHO;
        }
        else
        {
          valid_history[history_index] = raw_distance_m;
          history_index =
              (history_index + 1U) % ULTRASONIC_FILTER_SIZE;
          if (valid_count < ULTRASONIC_FILTER_SIZE)
          {
            valid_count++;
          }

          state.distance_m = Median(valid_history, valid_count);
          state.sample_time_ms = HAL_GetTick();
          state.status = ULTRA_STATUS_OK;
        }
      }
      else if (wait_result == ULTRASONIC_WAIT_TIMEOUT)
      {
        state.distance_m = ULTRASONIC_MAX_VALID_M;
        state.sample_time_ms = HAL_GetTick();
        state.status = ULTRA_STATUS_NO_ECHO;
      }
      else
      {
        state.status = ULTRA_STATUS_TIMEOUT;
      }
    }

    StoreState(&state);
    next_wake_tick += ULTRASONIC_TASK_PERIOD_MS;
    (void)osDelayUntil(next_wake_tick);
  }
}

void UltrasonicTask_GetState(UltrasonicState *state)
{
  if ((state == NULL) || (state_mutex == NULL))
  {
    return;
  }

  if (osMutexAcquire(state_mutex, osWaitForever) == osOK)
  {
    *state = shared_state;
    (void)osMutexRelease(state_mutex);
  }
}
