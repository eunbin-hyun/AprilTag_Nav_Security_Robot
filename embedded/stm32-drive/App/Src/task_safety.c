#include "task_safety.h"

#include "cmsis_os2.h"
#include "motor.h"
#include "task_control.h"
#include "task_ultrasonic.h"

#include "FreeRTOS.h"
#include "task.h"

#include <string.h>

#define SAFETY_TASK_PERIOD_MS       10U
#define SAFETY_MOTION_EPSILON_MPS  0.01f
#define SAFETY_PWM_EPSILON_PERCENT 0.01f

static osMutexId_t state_mutex;
static SafetyRequest shared_request;
static bool reset_requested;

static bool IsPositive(float value, float epsilon)
{
  return value > epsilon;
}

static bool IsNegative(float value, float epsilon)
{
  return value < -epsilon;
}

static bool CommandRequestsForward(const ControlCommand *command)
{
  if (command->mode == CONTROL_MODE_OPEN_LOOP)
  {
    return IsPositive(command->pwm_percent,
                      SAFETY_PWM_EPSILON_PERCENT);
  }

  if (command->mode == CONTROL_MODE_SPEED_PID)
  {
    return IsPositive(command->target_speed_mps,
                      SAFETY_MOTION_EPSILON_MPS);
  }

  return false;
}

static bool CommandRequestsReverse(const ControlCommand *command)
{
  if (command->mode == CONTROL_MODE_OPEN_LOOP)
  {
    return IsNegative(command->pwm_percent,
                      SAFETY_PWM_EPSILON_PERCENT);
  }

  if (command->mode == CONTROL_MODE_SPEED_PID)
  {
    return IsNegative(command->target_speed_mps,
                      SAFETY_MOTION_EPSILON_MPS);
  }

  return false;
}

static bool TakeResetRequest(void)
{
  bool requested;

  taskENTER_CRITICAL();
  requested = reset_requested;
  reset_requested = false;
  taskEXIT_CRITICAL();
  return requested;
}

static void StoreRequest(const SafetyRequest *request)
{
  if ((request == NULL) || (state_mutex == NULL))
  {
    return;
  }

  if (osMutexAcquire(state_mutex, osWaitForever) == osOK)
  {
    shared_request = *request;
    (void)osMutexRelease(state_mutex);
  }
}

bool SafetyTask_Init(void)
{
  const osMutexAttr_t mutex_attributes = {
    .name = "SafetyState",
  };

  memset(&shared_request, 0, sizeof(shared_request));
  shared_request.level = SAFETY_LEVEL_STOP;
  shared_request.reason = SAFETY_REASON_SENSOR_INIT;
  shared_request.stop_request = true;
  shared_request.block_reverse = true;
  reset_requested = false;

  state_mutex = osMutexNew(&mutex_attributes);
  return state_mutex != NULL;
}

void Safety_Task(void *argument)
{
  SafetyMachine machine;
  SafetyRequest request;
  bool previous_latched = false;
  uint32_t loop_count = 0U;
  uint32_t next_wake_tick;

  (void)argument;
  Safety_InitMachine(&machine);
  next_wake_tick = osKernelGetTickCount();

  for (;;)
  {
    UltrasonicState ultrasonic;
    ControlCommand command;
    ControlState control_state;
    SafetyInput input;
    bool reset;

    memset(&ultrasonic, 0, sizeof(ultrasonic));
    memset(&command, 0, sizeof(command));
    memset(&control_state, 0, sizeof(control_state));
    memset(&input, 0, sizeof(input));

    UltrasonicTask_GetState(&ultrasonic);
    Control_GetCommand(&command);
    Control_GetState(&control_state);

    input.ultrasonic = ultrasonic;
    input.forward_requested = CommandRequestsForward(&command);
    input.reverse_requested = CommandRequestsReverse(&command);
    input.vehicle_moving_forward =
        IsPositive(control_state.measured_speed_mps,
                   SAFETY_MOTION_EPSILON_MPS);
    input.vehicle_moving_reverse =
        IsNegative(control_state.measured_speed_mps,
                   SAFETY_MOTION_EPSILON_MPS);
    input.motor_output_zero =
        !Motor_IsEnabled() &&
        (Motor_GetAppliedPercent() <= SAFETY_PWM_EPSILON_PERCENT) &&
        (Motor_GetAppliedPercent() >= -SAFETY_PWM_EPSILON_PERCENT);
    input.manual_estop = false;
    reset = TakeResetRequest();

    request = Safety_Evaluate(&machine,
                              &input,
                              reset,
                              osKernelGetTickCount());
    request.loop_count = ++loop_count;

    if (request.emergency_disable)
    {
      Motor_EmergencyDisable();
    }
    else if (previous_latched && !request.latched)
    {
      if (!Motor_ClearEmergencyDisable())
      {
        Safety_ForceEmergencyStop(&machine,
                                  SAFETY_REASON_MOTOR_NOT_SAFE);
        request = Safety_Evaluate(&machine,
                                  &input,
                                  false,
                                  osKernelGetTickCount());
        request.loop_count = loop_count;
        Motor_EmergencyDisable();
      }
    }

    previous_latched = request.latched;
    StoreRequest(&request);
    Control_SetSafetyRequest(&request);

    next_wake_tick += SAFETY_TASK_PERIOD_MS;
    (void)osDelayUntil(next_wake_tick);
  }
}

void SafetyTask_RequestReset(void)
{
  taskENTER_CRITICAL();
  reset_requested = true;
  taskEXIT_CRITICAL();
}

void SafetyTask_GetState(SafetyRequest *request)
{
  if ((request == NULL) || (state_mutex == NULL))
  {
    return;
  }

  if (osMutexAcquire(state_mutex, osWaitForever) == osOK)
  {
    *request = shared_request;
    (void)osMutexRelease(state_mutex);
  }
}
