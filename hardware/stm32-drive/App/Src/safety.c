#include "safety.h"

#include "vehicle_config.h"

#include <string.h>

#define SAFETY_CAUTION_ENTER_M  0.60f
#define SAFETY_CAUTION_CLEAR_M  0.65f

#define SAFETY_STOP_ENTER_M     0.40f
#define SAFETY_STOP_CLEAR_M     0.50f

static SafetyRequest BuildRequest(const SafetyMachine *machine,
                                  bool reverse_unprotected,
                                  uint32_t now_ms)
{
  SafetyRequest request;

  memset(&request, 0, sizeof(request));
  request.level = machine->level;
  request.reason = machine->reason;
  request.latched = machine->latched;
  request.block_reverse = reverse_unprotected;
  request.stop_request =
      (machine->level == SAFETY_LEVEL_STOP) ||
      machine->latched ||
      reverse_unprotected;
  request.emergency_disable = machine->latched;
  request.updated_at_ms = now_ms;

  if (reverse_unprotected)
  {
    request.reason |= SAFETY_REASON_REVERSE_UNPROTECTED;
    if (!request.latched)
    {
      request.level = SAFETY_LEVEL_STOP;
    }
  }

  return request;
}

#if VEHICLE_ENFORCE_ULTRASONIC_SAFETY
static uint32_t StatusToReason(UltrasonicStatus status)
{
  switch (status)
  {
    case ULTRA_STATUS_NO_ECHO:
      return SAFETY_REASON_NONE;

    case ULTRA_STATUS_TIMEOUT:
      return SAFETY_REASON_SENSOR_TIMEOUT;

    case ULTRA_STATUS_OUT_OF_RANGE:
      return SAFETY_REASON_SENSOR_OUT_OF_RANGE;

    case ULTRA_STATUS_STALE:
      return SAFETY_REASON_SENSOR_STALE;

    case ULTRA_STATUS_INIT:
    default:
      return SAFETY_REASON_SENSOR_INIT;
  }
}

static bool IsStale(const UltrasonicState *ultrasonic, uint32_t now_ms)
{
  if (ultrasonic->sample_time_ms == 0U)
  {
    return now_ms > SAFETY_STALE_TIMEOUT_MS;
  }

  return (uint32_t)(now_ms - ultrasonic->sample_time_ms) >
         SAFETY_STALE_TIMEOUT_MS;
}

static void EvaluateDistance(SafetyMachine *machine,
                             float distance_m,
                             bool new_sample)
{
  if (distance_m < SAFETY_STOP_ENTER_M)
  {
    machine->level = SAFETY_LEVEL_STOP;
    machine->reason = SAFETY_REASON_DISTANCE_STOP;
    machine->consecutive_clear_samples = 0U;
    return;
  }

  if (machine->level == SAFETY_LEVEL_STOP)
  {
    if (distance_m >= SAFETY_STOP_CLEAR_M)
    {
      if (new_sample &&
          (machine->consecutive_clear_samples < UINT8_MAX))
      {
        machine->consecutive_clear_samples++;
      }
    }
    else
    {
      machine->consecutive_clear_samples = 0U;
    }

    if (machine->consecutive_clear_samples <
        SAFETY_STOP_CLEAR_SAMPLE_COUNT)
    {
      machine->reason = SAFETY_REASON_DISTANCE_STOP;
      return;
    }
  }

  machine->consecutive_clear_samples = 0U;
  if (distance_m < SAFETY_CAUTION_ENTER_M)
  {
    machine->level = SAFETY_LEVEL_CAUTION;
    machine->reason = SAFETY_REASON_DISTANCE_CAUTION;
  }
  else if ((machine->level == SAFETY_LEVEL_CAUTION) &&
           (distance_m < SAFETY_CAUTION_CLEAR_M))
  {
    machine->reason = SAFETY_REASON_DISTANCE_CAUTION;
  }
  else
  {
    machine->level = SAFETY_LEVEL_CLEAR;
    machine->reason = SAFETY_REASON_NONE;
  }
}
#endif

void Safety_InitMachine(SafetyMachine *machine)
{
  if (machine == NULL)
  {
    return;
  }

  memset(machine, 0, sizeof(*machine));
  machine->level = SAFETY_LEVEL_STOP;
  machine->reason = SAFETY_REASON_SENSOR_INIT;
}

SafetyRequest Safety_Evaluate(SafetyMachine *machine,
                              const SafetyInput *input,
                              bool reset_requested,
                              uint32_t now_ms)
{
  bool reverse_unprotected;
#if VEHICLE_ENFORCE_ULTRASONIC_SAFETY
  bool stale;
  bool new_ultrasonic_sample;
#endif

  if ((machine == NULL) || (input == NULL))
  {
    SafetyRequest request;

    memset(&request, 0, sizeof(request));
    request.level = SAFETY_LEVEL_ESTOP_LATCHED;
    request.reason = SAFETY_REASON_MOTOR_NOT_SAFE;
    request.stop_request = true;
    request.emergency_disable = true;
    request.latched = true;
    request.block_reverse = true;
    request.updated_at_ms = now_ms;
    return request;
  }

  reverse_unprotected =
      (VEHICLE_ALLOW_REVERSE_WITHOUT_REAR_SENSOR == 0U) &&
      (input->reverse_requested || input->vehicle_moving_reverse);

#if VEHICLE_ENFORCE_ULTRASONIC_SAFETY
  stale = IsStale(&input->ultrasonic, now_ms);
  new_ultrasonic_sample =
      !machine->ultrasonic_sequence_seen ||
      (input->ultrasonic.sequence != machine->last_ultrasonic_sequence);
  if (new_ultrasonic_sample)
  {
    machine->last_ultrasonic_sequence = input->ultrasonic.sequence;
    machine->ultrasonic_sequence_seen = true;
  }
#endif

  if (input->manual_estop)
  {
    Safety_ForceEmergencyStop(machine, SAFETY_REASON_MANUAL_ESTOP);
  }

  if (machine->latched)
  {
    bool reset_safe =
        reset_requested &&
        !input->manual_estop &&
        input->motor_output_zero;
#if VEHICLE_ENFORCE_ULTRASONIC_SAFETY
    reset_safe =
        reset_safe &&
        ((input->ultrasonic.status == ULTRA_STATUS_OK) ||
         (input->ultrasonic.status == ULTRA_STATUS_NO_ECHO)) &&
        !stale &&
        ((input->ultrasonic.status == ULTRA_STATUS_NO_ECHO) ||
         (input->ultrasonic.distance_m >= SAFETY_CAUTION_ENTER_M));
#endif

    if (reset_safe)
    {
      machine->latched = false;
      machine->level = SAFETY_LEVEL_CLEAR;
      machine->reason = SAFETY_REASON_NONE;
      machine->consecutive_clear_samples = 0U;
    }
    else
    {
      return BuildRequest(machine, reverse_unprotected, now_ms);
    }
  }

#if VEHICLE_ENFORCE_ULTRASONIC_SAFETY
  if (input->ultrasonic.status == ULTRA_STATUS_NO_ECHO)
  {
    if (stale)
    {
      machine->level = SAFETY_LEVEL_STOP;
      machine->reason = SAFETY_REASON_SENSOR_STALE;
      machine->consecutive_clear_samples = 0U;
    }
    else
    {
      EvaluateDistance(machine,
                       SAFETY_CAUTION_CLEAR_M,
                       new_ultrasonic_sample);
    }
  }
  else if (input->ultrasonic.status != ULTRA_STATUS_OK)
  {
    machine->level = SAFETY_LEVEL_STOP;
    machine->reason = StatusToReason(input->ultrasonic.status);
    machine->consecutive_clear_samples = 0U;
  }
  else if (stale)
  {
    machine->level = SAFETY_LEVEL_STOP;
    machine->reason = SAFETY_REASON_SENSOR_STALE;
    machine->consecutive_clear_samples = 0U;
  }
  else
  {
    EvaluateDistance(machine,
                     input->ultrasonic.distance_m,
                     new_ultrasonic_sample);
  }
#else
  /*
   * Temporary commissioning mode: measurements remain available in range
   * telemetry, but sensor status and distance do not request a motor stop.
   */
  machine->level = SAFETY_LEVEL_CLEAR;
  machine->reason = SAFETY_REASON_NONE;
  machine->consecutive_clear_samples = 0U;
#endif

  return BuildRequest(machine, reverse_unprotected, now_ms);
}

void Safety_ForceEmergencyStop(SafetyMachine *machine, uint32_t reason)
{
  if (machine == NULL)
  {
    return;
  }

  machine->level = SAFETY_LEVEL_ESTOP_LATCHED;
  machine->reason = reason;
  machine->latched = true;
  machine->consecutive_clear_samples = 0U;
}
