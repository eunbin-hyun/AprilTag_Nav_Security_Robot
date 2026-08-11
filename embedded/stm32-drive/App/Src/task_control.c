#include "task_control.h"

#include "cmsis_os2.h"
#include "encoder.h"
#include "main.h"
#include "motor.h"
#include "pid.h"
#include "steering_sensor.h"
#include "vehicle_config.h"

#include "FreeRTOS.h"
#include "task.h"

#include <math.h>
#include <string.h>

#define CONTROL_PERIOD_MS          10U
#define CONTROL_PERIOD_SECONDS     0.010f
#define CONTROL_SPEED_WINDOW_SIZE  5U
#define CONTROL_SPEED_WINDOW_SECONDS 0.050f
#define CONTROL_PID_KP             120.0f
#define CONTROL_PID_KI             20.0f
#define CONTROL_PID_KD             0.0f
#define CONTROL_MIN_RUNNING_PWM    22.0f
#define CONTROL_MAX_RUNNING_PWM    95.0f
#define CONTROL_FF_OFFSET_PWM       18.5f
#define CONTROL_FF_GAIN_PWM_PER_MPS 46.5f
#define CONTROL_SAFETY_TIMEOUT_MS  50U
#define CONTROL_SERVO_DIAGNOSTIC_TIMEOUT_MS 300U
#define CONTROL_PID_OUTPUT_MIN     \
  (-(CONTROL_MAX_RUNNING_PWM - CONTROL_MIN_RUNNING_PWM))
#define CONTROL_PID_OUTPUT_MAX     \
  (CONTROL_MAX_RUNNING_PWM - CONTROL_MIN_RUNNING_PWM)
#define CONTROL_ZERO_SPEED_EPSILON 0.001f

static ControlCommand shared_command;
static ControlState shared_state;
static SafetyRequest shared_safety_request;
static osMutexId_t safety_request_mutex;
static uint32_t latched_fault_flags;
static PIDController speed_pid;
static uint16_t shared_servo_diagnostic_pulse_us;
static uint32_t shared_servo_diagnostic_updated_at_ms;

typedef struct
{
  float delta_distance_m[CONTROL_SPEED_WINDOW_SIZE];
  float total_distance_m;
  uint32_t next_index;
  uint32_t sample_count;
} EncoderSpeedWindow;

static EncoderSpeedWindow encoder_speed_window;

#if VEHICLE_STEERING_SERVO_CALIBRATED
static const SteeringServoLutPoint steering_servo_lut[] = {
  {VEHICLE_SERVO_LUT_0_ANGLE_DEG,
   VEHICLE_SERVO_LUT_0_PULSE_US},
  {VEHICLE_SERVO_LUT_1_ANGLE_DEG,
   VEHICLE_SERVO_LUT_1_PULSE_US},
  {VEHICLE_SERVO_LUT_2_ANGLE_DEG,
   VEHICLE_SERVO_LUT_2_PULSE_US},
  {VEHICLE_SERVO_LUT_3_ANGLE_DEG,
   VEHICLE_SERVO_LUT_3_PULSE_US},
  {VEHICLE_SERVO_LUT_4_ANGLE_DEG,
   VEHICLE_SERVO_LUT_4_PULSE_US},
  {VEHICLE_SERVO_LUT_5_ANGLE_DEG,
   VEHICLE_SERVO_LUT_5_PULSE_US},
  {VEHICLE_SERVO_LUT_6_ANGLE_DEG,
   VEHICLE_SERVO_LUT_6_PULSE_US},
};
#endif

static void EncoderSpeedWindow_Reset(EncoderSpeedWindow *window)
{
  memset(window, 0, sizeof(*window));
}

static float EncoderSpeedWindow_Update(EncoderSpeedWindow *window,
                                       float delta_distance_m)
{
  float elapsed_seconds;

  if (window->sample_count == CONTROL_SPEED_WINDOW_SIZE)
  {
    window->total_distance_m -=
        window->delta_distance_m[window->next_index];
  }
  else
  {
    window->sample_count++;
  }

  window->delta_distance_m[window->next_index] = delta_distance_m;
  window->total_distance_m += delta_distance_m;
  window->next_index =
      (window->next_index + 1U) % CONTROL_SPEED_WINDOW_SIZE;

  elapsed_seconds =
      (window->sample_count == CONTROL_SPEED_WINDOW_SIZE) ?
          CONTROL_SPEED_WINDOW_SECONDS :
          ((float)window->sample_count * CONTROL_PERIOD_SECONDS);

  return window->total_distance_m / elapsed_seconds;
}

static float ClampPercent(float percent)
{
  if (percent > 100.0f)
  {
    return 100.0f;
  }

  if (percent < -100.0f)
  {
    return -100.0f;
  }

  return percent;
}

static bool IsNearlyZero(float value)
{
  return (value > -CONTROL_ZERO_SPEED_EPSILON) &&
         (value < CONTROL_ZERO_SPEED_EPSILON);
}

static float CalculateSpeedFeedforwardMagnitude(float target_speed_mps)
{
  float feedforward_pwm =
      CONTROL_FF_OFFSET_PWM +
      (CONTROL_FF_GAIN_PWM_PER_MPS * fabsf(target_speed_mps));

  /*
   * Initial fit from closed-loop vehicle test data. Re-tune these constants
   * after validating the feed-forward response on the vehicle.
   */
  if (feedforward_pwm < CONTROL_MIN_RUNNING_PWM)
  {
    return CONTROL_MIN_RUNNING_PWM;
  }

  if (feedforward_pwm > CONTROL_MAX_RUNNING_PWM)
  {
    return CONTROL_MAX_RUNNING_PWM;
  }

  return feedforward_pwm;
}

static float ApplySpeedFeedforward(float target_speed_mps,
                                   float pid_correction)
{
  float feedforward_pwm;
  float output;

  if (target_speed_mps > 0.0f)
  {
    feedforward_pwm =
        CalculateSpeedFeedforwardMagnitude(target_speed_mps);
    output = feedforward_pwm + pid_correction;
    if (output < 0.0f)
    {
      return 0.0f;
    }
    if (output > CONTROL_MAX_RUNNING_PWM)
    {
      return CONTROL_MAX_RUNNING_PWM;
    }
    return output;
  }

  if (target_speed_mps < 0.0f)
  {
    feedforward_pwm =
        CalculateSpeedFeedforwardMagnitude(target_speed_mps);
    output = -feedforward_pwm + pid_correction;
    if (output > 0.0f)
    {
      return 0.0f;
    }
    if (output < -CONTROL_MAX_RUNNING_PWM)
    {
      return -CONTROL_MAX_RUNNING_PWM;
    }
    return output;
  }

  return 0.0f;
}

static void CopyCommand(ControlCommand *command)
{
  taskENTER_CRITICAL();
  *command = shared_command;
  taskEXIT_CRITICAL();
}

static void CopySafetyRequest(SafetyRequest *request)
{
  if ((request == NULL) || (safety_request_mutex == NULL))
  {
    return;
  }

  if (osMutexAcquire(safety_request_mutex, osWaitForever) == osOK)
  {
    *request = shared_safety_request;
    (void)osMutexRelease(safety_request_mutex);
  }
}

static void StoreState(const ControlState *state)
{
  taskENTER_CRITICAL();
  shared_state = *state;
  taskEXIT_CRITICAL();
}

static void CopyServoDiagnosticRequest(uint16_t *pulse_us,
                                       uint32_t *updated_at_ms)
{
  taskENTER_CRITICAL();
  *pulse_us = shared_servo_diagnostic_pulse_us;
  *updated_at_ms = shared_servo_diagnostic_updated_at_ms;
  taskEXIT_CRITICAL();
}

void Control_Init(void)
{
  const osMutexAttr_t mutex_attributes = {
    .name = "ControlSafety",
  };

  memset(&shared_command, 0, sizeof(shared_command));
  memset(&shared_state, 0, sizeof(shared_state));
  memset(&shared_safety_request, 0, sizeof(shared_safety_request));
  shared_command.mode = CONTROL_MODE_DISABLED;
  shared_state.mode = CONTROL_MODE_DISABLED;
  shared_safety_request.level = SAFETY_LEVEL_STOP;
  shared_safety_request.reason = SAFETY_REASON_SENSOR_INIT;
  shared_safety_request.stop_request = true;
  shared_safety_request.block_reverse = true;
  shared_servo_diagnostic_pulse_us = 0U;
  shared_servo_diagnostic_updated_at_ms = 0U;
  latched_fault_flags = CONTROL_FAULT_NONE;
  EncoderSpeedWindow_Reset(&encoder_speed_window);

  safety_request_mutex = osMutexNew(&mutex_attributes);
  if (safety_request_mutex == NULL)
  {
    Error_Handler();
  }

  PID_Init(&speed_pid,
           CONTROL_PID_KP,
           CONTROL_PID_KI,
           CONTROL_PID_KD,
           CONTROL_PID_OUTPUT_MIN,
           CONTROL_PID_OUTPUT_MAX);

#if VEHICLE_STEERING_SENSOR_CALIBRATED
  (void)SteeringSensor_SetCalibration(
      VEHICLE_STEERING_LEFT_RAW,
      VEHICLE_STEERING_CENTER_RAW,
      VEHICLE_STEERING_RIGHT_RAW,
      VEHICLE_STEERING_LEFT_ANGLE_DEG,
      VEHICLE_STEERING_RIGHT_ANGLE_DEG);
#endif

#if VEHICLE_STEERING_SERVO_CALIBRATED
  (void)SteeringServo_SetLutCalibration(
      steering_servo_lut,
      (uint32_t)(sizeof(steering_servo_lut) /
                 sizeof(steering_servo_lut[0])));
#endif
}

void Control_Task(void *argument)
{
  ControlCommand command;
  ControlState state;
  SafetyRequest safety_request;
  EncoderSample encoder_sample;
  SteeringSensorSample steering_sample;
  ControlMode previous_mode = CONTROL_MODE_DISABLED;
  uint32_t next_wake_tick;
  bool motor_ok;
  bool encoder_ok;
  bool steering_sensor_ok;
  bool steering_servo_ok;
  bool servo_diagnostic_was_active = false;

  (void)argument;
  memset(&state, 0, sizeof(state));
  memset(&safety_request, 0, sizeof(safety_request));
  memset(&encoder_sample, 0, sizeof(encoder_sample));
  memset(&steering_sample, 0, sizeof(steering_sample));

  motor_ok = Motor_Init();
  encoder_ok = Encoder_Init();
  steering_sensor_ok = SteeringSensor_Init();
  steering_servo_ok = SteeringServo_Start();
  if (!motor_ok ||
      !encoder_ok ||
      !steering_sensor_ok ||
      !steering_servo_ok)
  {
    latched_fault_flags |= CONTROL_FAULT_HARDWARE_INIT;
    Motor_Disable();
  }

  next_wake_tick = osKernelGetTickCount();

  for (;;)
  {
    float applied_pwm = 0.0f;
    float measured_speed_mps;
    uint32_t active_faults;
    bool reset_speed_window = false;
    bool safety_request_stale;
    bool steering_read_ok;
    bool steering_sensor_calibrated;
    bool steering_servo_calibrated;
    bool steering_servo_command_ok;
    bool servo_diagnostic_active;
    uint16_t servo_diagnostic_pulse_us;
    uint32_t servo_diagnostic_updated_at_ms;

    taskENTER_CRITICAL();
    active_faults = latched_fault_flags;
    taskEXIT_CRITICAL();

    Encoder_Update(CONTROL_PERIOD_SECONDS, &encoder_sample);
    measured_speed_mps =
        EncoderSpeedWindow_Update(&encoder_speed_window,
                                  encoder_sample.delta_distance_m);
    steering_read_ok = SteeringSensor_Read(&steering_sample);
    CopyCommand(&command);
    CopySafetyRequest(&safety_request);
    CopyServoDiagnosticRequest(&servo_diagnostic_pulse_us,
                               &servo_diagnostic_updated_at_ms);
    safety_request_stale =
        (uint32_t)(HAL_GetTick() -
                   safety_request.updated_at_ms) >
        CONTROL_SAFETY_TIMEOUT_MS;

    steering_sensor_calibrated =
        SteeringSensor_IsCalibrated();
    steering_servo_calibrated =
        SteeringServo_IsCalibrated();
    /*
     * A missing feedback sensor must not be confused with an invalid servo
     * command calibration. The final vehicle may explicitly use the
     * calibrated command angle as an open-loop estimate while continuing to
     * report steering feedback as invalid.
     */
    if (!steering_servo_calibrated ||
        ((VEHICLE_ALLOW_STEERING_COMMAND_ESTIMATE == 0U) &&
         !steering_sensor_calibrated))
    {
      active_faults |= CONTROL_FAULT_STEERING_NOT_CALIBRATED;
    }
    else
    {
      active_faults &= ~CONTROL_FAULT_STEERING_NOT_CALIBRATED;
    }

    if (steering_sensor_calibrated &&
        (!steering_read_ok || !steering_sample.angle_valid))
    {
      active_faults |= CONTROL_FAULT_STEERING_SENSOR_INVALID;
    }
    else
    {
      active_faults &= ~CONTROL_FAULT_STEERING_SENSOR_INVALID;
    }

    servo_diagnostic_active =
        (servo_diagnostic_pulse_us != 0U) &&
        ((uint32_t)(HAL_GetTick() -
                    servo_diagnostic_updated_at_ms) <=
         CONTROL_SERVO_DIAGNOSTIC_TIMEOUT_MS) &&
        (command.mode == CONTROL_MODE_DISABLED);

    if (servo_diagnostic_active)
    {
      steering_servo_command_ok =
          SteeringServo_SetDiagnosticPulseUs(
              servo_diagnostic_pulse_us);
    }
    else if (command.mode == CONTROL_MODE_DISABLED)
    {
      /*
       * A zero diagnostic pulse means "PWM off", not "return to center".
       * Leave TIM3_CH1 stopped after a calibration pulse so the linkage can
       * be marked and measured without an automatic 0-degree command.
       */
      if (servo_diagnostic_was_active)
      {
        SteeringServo_Stop();
      }
      else if (previous_mode != CONTROL_MODE_DISABLED)
      {
        /*
         * Preserve the normal drive-stop behavior: a regular CMD_STOP carries
         * a zero steering target and returns the wheels to calibrated center.
         */
        steering_servo_command_ok =
            SteeringServo_SetAngle(command.target_steering_deg);
      }
      else
      {
        steering_servo_command_ok = true;
      }
    }
    else
    {
      /*
       * Diagnostic PWM may have been stopped while control was disabled.
       * Re-enable calibrated PWM whenever normal driving resumes.
       */
      steering_servo_command_ok = true;
      if (servo_diagnostic_was_active ||
          (previous_mode == CONTROL_MODE_DISABLED))
      {
        SteeringServo_Stop();
        if (steering_servo_calibrated)
        {
          steering_servo_command_ok = SteeringServo_Start();
        }
      }
      if (steering_servo_command_ok)
      {
        steering_servo_command_ok =
            SteeringServo_SetAngle(command.target_steering_deg);
      }
    }
    servo_diagnostic_was_active = servo_diagnostic_active;
    if (!steering_servo_command_ok)
    {
      active_faults |= CONTROL_FAULT_STEERING_SERVO_INVALID;
    }
    else
    {
      active_faults &= ~CONTROL_FAULT_STEERING_SERVO_INVALID;
    }

    if (command.mode != previous_mode)
    {
      PID_Reset(&speed_pid);
      Motor_Disable();
      previous_mode = command.mode;
    }

    if ((active_faults & CONTROL_FAULT_HARDWARE_INIT) != 0U)
    {
      reset_speed_window = true;
      Motor_Disable();
      command.mode = CONTROL_MODE_DISABLED;
    }
    else if ((command.mode == CONTROL_MODE_SPEED_PID) &&
             ((active_faults &
               (CONTROL_FAULT_STEERING_NOT_CALIBRATED |
                CONTROL_FAULT_STEERING_SENSOR_INVALID |
                CONTROL_FAULT_STEERING_SERVO_INVALID)) != 0U))
    {
      reset_speed_window = true;
      PID_Reset(&speed_pid);
      Motor_Disable();
    }
    else if (safety_request.stop_request ||
             safety_request.latched ||
             safety_request_stale)
    {
      reset_speed_window = true;
      PID_Reset(&speed_pid);
      if (safety_request.latched)
      {
        Motor_EmergencyDisable();
      }
      else
      {
        Motor_Disable();
      }
    }
    else
    {
      switch (command.mode)
      {
        case CONTROL_MODE_OPEN_LOOP:
          Motor_Enable();
          applied_pwm = ClampPercent(command.pwm_percent);
          Motor_SetPercent(applied_pwm);
          if (IsNearlyZero(applied_pwm))
          {
            reset_speed_window = true;
          }
          break;

        case CONTROL_MODE_SPEED_PID:
          if (!encoder_sample.calibrated)
          {
            reset_speed_window = true;
            active_faults |= CONTROL_FAULT_ENCODER_NOT_CALIBRATED;
            PID_Reset(&speed_pid);
            Motor_Disable();
          }
          else if (IsNearlyZero(command.target_speed_mps))
          {
            reset_speed_window = true;
            PID_Reset(&speed_pid);
            Motor_Enable();
            Motor_SetPercent(0.0f);
          }
          else
          {
            float pid_correction;

            active_faults &= ~CONTROL_FAULT_ENCODER_NOT_CALIBRATED;
            pid_correction = PID_Update(&speed_pid,
                                        command.target_speed_mps,
                                        measured_speed_mps,
                                        CONTROL_PERIOD_SECONDS);
            applied_pwm = ApplySpeedFeedforward(command.target_speed_mps,
                                                pid_correction);
            Motor_Enable();
            Motor_SetPercent(applied_pwm);
          }
          break;

        case CONTROL_MODE_DISABLED:
        default:
          reset_speed_window = true;
          PID_Reset(&speed_pid);
          Motor_Disable();
          break;
      }
    }

    if (reset_speed_window)
    {
      EncoderSpeedWindow_Reset(&encoder_speed_window);
      measured_speed_mps = 0.0f;
    }

    state.mode = command.mode;
    state.target_speed_mps = command.target_speed_mps;
    state.target_steering_deg = command.target_steering_deg;
    state.encoder_diff = encoder_sample.delta_count;
    state.encoder_total_count = encoder_sample.total_count;
    state.encoder_delta_distance_m =
        encoder_sample.delta_distance_m;
    state.encoder_total_distance_m =
        encoder_sample.total_distance_m;
    state.measured_speed_mps = measured_speed_mps;
    state.steering_adc_raw = steering_sample.raw;
    state.measured_steering_deg = steering_sample.angle_deg;
    state.steering_adc_valid =
        steering_read_ok && steering_sample.adc_valid;
    state.steering_angle_valid =
        steering_read_ok && steering_sample.angle_valid;
    state.steering_sensor_calibrated =
        SteeringSensor_IsCalibrated();
    state.steering_servo_calibrated =
        SteeringServo_IsCalibrated();
    state.steering_servo_diagnostic_pulse_us =
        servo_diagnostic_active ?
            servo_diagnostic_pulse_us :
            0U;
    state.steering_servo_diagnostic_active =
        servo_diagnostic_active;
    state.applied_pwm_percent = Motor_GetAppliedPercent();
    state.encoder_calibrated = encoder_sample.calibrated;
    state.fault_flags = active_faults;
    state.updated_at_ms = HAL_GetTick();
    StoreState(&state);

    next_wake_tick += CONTROL_PERIOD_MS;
    (void)osDelayUntil(next_wake_tick);
  }
}

void Control_SetCommand(const ControlCommand *command)
{
  ControlCommand safe_command;

  if (command == NULL)
  {
    return;
  }

  safe_command = *command;
  safe_command.pwm_percent = ClampPercent(safe_command.pwm_percent);
  if ((safe_command.mode < CONTROL_MODE_DISABLED) ||
      (safe_command.mode > CONTROL_MODE_SPEED_PID))
  {
    safe_command.mode = CONTROL_MODE_DISABLED;
  }

  taskENTER_CRITICAL();
  shared_command = safe_command;
  taskEXIT_CRITICAL();
}

void Control_SetDisabled(void)
{
  ControlCommand command = {0};

  command.mode = CONTROL_MODE_DISABLED;
  Control_SetCommand(&command);
}

void Control_SetOpenLoopPercent(float pwm_percent)
{
  ControlCommand command = {0};

  command.mode = CONTROL_MODE_OPEN_LOOP;
  command.pwm_percent = pwm_percent;
  Control_SetCommand(&command);
}

void Control_SetSpeedTarget(float target_speed_mps,
                            float target_steering_deg)
{
  ControlCommand command = {0};

  command.mode = CONTROL_MODE_SPEED_PID;
  command.target_speed_mps = target_speed_mps;
  command.target_steering_deg = target_steering_deg;
  Control_SetCommand(&command);
}

void Control_GetState(ControlState *state)
{
  if (state == NULL)
  {
    return;
  }

  taskENTER_CRITICAL();
  *state = shared_state;
  taskEXIT_CRITICAL();
}

void Control_GetCommand(ControlCommand *command)
{
  if (command == NULL)
  {
    return;
  }

  CopyCommand(command);
}

void Control_SetSafetyRequest(const SafetyRequest *request)
{
  if ((request == NULL) || (safety_request_mutex == NULL))
  {
    return;
  }

  if (osMutexAcquire(safety_request_mutex, osWaitForever) == osOK)
  {
    shared_safety_request = *request;
    (void)osMutexRelease(safety_request_mutex);
  }
}

bool Control_SetEncoderCalibration(float counts_per_wheel_revolution,
                                   float wheel_circumference_m)
{
  bool result;

  taskENTER_CRITICAL();
  result = Encoder_SetCalibration(counts_per_wheel_revolution,
                                  wheel_circumference_m);
  taskEXIT_CRITICAL();
  return result;
}

bool Control_SetSteeringSensorCalibration(uint16_t left_raw,
                                          uint16_t center_raw,
                                          uint16_t right_raw,
                                          float left_angle_deg,
                                          float right_angle_deg)
{
  bool result;

  taskENTER_CRITICAL();
  result = SteeringSensor_SetCalibration(left_raw,
                                         center_raw,
                                         right_raw,
                                         left_angle_deg,
                                         right_angle_deg);
  taskEXIT_CRITICAL();
  return result;
}

bool Control_SetSteeringServoCalibration(float left_angle_deg,
                                         uint32_t left_pulse_us,
                                         float center_angle_deg,
                                         uint32_t center_pulse_us,
                                         float right_angle_deg,
                                         uint32_t right_pulse_us)
{
  bool result;

  taskENTER_CRITICAL();
  result = SteeringServo_SetCalibration(left_angle_deg,
                                        left_pulse_us,
                                        center_angle_deg,
                                        center_pulse_us,
                                        right_angle_deg,
                                        right_pulse_us);
  if (result)
  {
    result = SteeringServo_Start();
  }
  taskEXIT_CRITICAL();
  return result;
}

bool Control_SetSteeringServoDiagnosticPulse(uint16_t pulse_us)
{
  bool allowed;

  taskENTER_CRITICAL();
  allowed =
      (pulse_us == 0U) ||
      (((pulse_us >=
         STEERING_SERVO_DIAGNOSTIC_MIN_PULSE_US) &&
        (pulse_us <=
         STEERING_SERVO_DIAGNOSTIC_MAX_PULSE_US)) &&
       (shared_state.mode == CONTROL_MODE_DISABLED) &&
       IsNearlyZero(shared_state.applied_pwm_percent) &&
       ((shared_state.fault_flags &
         CONTROL_FAULT_HARDWARE_INIT) == 0U));

  if (allowed)
  {
    shared_servo_diagnostic_pulse_us = pulse_us;
    shared_servo_diagnostic_updated_at_ms = HAL_GetTick();
  }
  taskEXIT_CRITICAL();
  return allowed;
}

void Control_SetEncoderDirectionSign(int8_t direction_sign)
{
  taskENTER_CRITICAL();
  Encoder_SetDirectionSign(direction_sign);
  taskEXIT_CRITICAL();
}

void Control_SetMotorDirectionInverted(bool inverted)
{
  taskENTER_CRITICAL();
  Motor_SetDirectionInverted(inverted);
  taskEXIT_CRITICAL();
}

void Control_SetPidGains(float kp, float ki, float kd)
{
  taskENTER_CRITICAL();
  PID_SetGains(&speed_pid, kp, ki, kd);
  PID_Reset(&speed_pid);
  taskEXIT_CRITICAL();
}

void Control_ClearFaults(uint32_t fault_mask)
{
  /*
   * A failed HAL timer start cannot be recovered by merely acknowledging a
   * fault. Keep the hardware-init bit latched until the MCU is restarted.
   */
  fault_mask &= ~CONTROL_FAULT_HARDWARE_INIT;

  taskENTER_CRITICAL();
  latched_fault_flags &= ~fault_mask;
  taskEXIT_CRITICAL();
}
