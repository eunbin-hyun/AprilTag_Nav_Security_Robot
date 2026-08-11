#include "app_freertos.h"

#include "cmsis_os2.h"
#include "comm_protocol.h"
#include "comm_service.h"
#include "main.h"
#include "task_control.h"
#include "task_imu.h"
#include "task_odometry.h"
#include "task_safety.h"
#include "task_ultrasonic.h"
#include "vehicle_config.h"

#include <limits.h>
#include <math.h>
#include <stdbool.h>

#define CONTROL_TASK_STACK_BYTES 1024U
#define SAFETY_TASK_STACK_BYTES  1024U
#define ULTRA_TASK_STACK_BYTES   1024U
#define ODOMETRY_TASK_STACK_BYTES 1024U
#define IMU_TASK_STACK_BYTES      1536U
#define COMM_TASK_STACK_BYTES    1024U
#define COMM_TASK_PERIOD_MS      1U
#define COMM_STATE_UPDATE_PERIOD_MS 10U
#define TELEMETRY_PERIOD_MS      50U
#define RANGE_TELEMETRY_PERIOD_MS 100U
#define RANGE_VALID_FRONT_PRIMARY (1U << 0)
#define ODOMETRY_RAD_TO_MDEG     57295.77951308232f
#define ODOMETRY_RAD_TO_CDEG     5729.577951308232f

extern SPI_HandleTypeDef hspi5;

static osThreadId_t control_task_handle;
static osThreadId_t safety_task_handle;
static osThreadId_t ultrasonic_task_handle;
static osThreadId_t odometry_task_handle;
static osThreadId_t imu_task_handle;
static osThreadId_t comm_task_handle;
static bool comm_watchdog_armed;
static bool comm_timeout_fault_active;
static bool motor_diagnostic_active;
static uint32_t motor_diagnostic_stop_tick;
static uint32_t last_telemetry_tick;
static uint32_t last_range_telemetry_tick;
static uint32_t last_comm_state_update_tick;
static CommSystemState current_comm_state = COMM_STATE_SAFE_STOP;
static bool fault_event_initialized;
static uint32_t last_fault_event_bits;
static CommSystemState last_fault_event_state;

static const osThreadAttr_t control_task_attributes = {
  .name = "ControlTask",
  .stack_size = CONTROL_TASK_STACK_BYTES,
  .priority = (osPriority_t)osPriorityAboveNormal,
};

static const osThreadAttr_t safety_task_attributes = {
  .name = "SafetyTask",
  .stack_size = SAFETY_TASK_STACK_BYTES,
  .priority = (osPriority_t)osPriorityHigh,
};

static const osThreadAttr_t ultrasonic_task_attributes = {
  .name = "UltrasonicTask",
  .stack_size = ULTRA_TASK_STACK_BYTES,
  .priority = (osPriority_t)osPriorityNormal,
};

static const osThreadAttr_t odometry_task_attributes = {
  .name = "OdometryTask",
  .stack_size = ODOMETRY_TASK_STACK_BYTES,
  .priority = (osPriority_t)osPriorityNormal,
};

static const osThreadAttr_t imu_task_attributes = {
  .name = "ImuTask",
  .stack_size = IMU_TASK_STACK_BYTES,
  .priority = (osPriority_t)osPriorityAboveNormal,
};

static const osThreadAttr_t comm_task_attributes = {
  .name = "CommTask",
  .stack_size = COMM_TASK_STACK_BYTES,
  .priority = (osPriority_t)osPriorityNormal,
};

static int16_t FloatToInt16Saturated(float value)
{
  if (!isfinite(value))
  {
    return 0;
  }

  if (value >= (float)INT16_MAX)
  {
    return INT16_MAX;
  }

  if (value <= (float)INT16_MIN)
  {
    return INT16_MIN;
  }

  return (int16_t)(value + ((value >= 0.0f) ? 0.5f : -0.5f));
}

static int32_t FloatToInt32Saturated(float value)
{
  if (!isfinite(value))
  {
    return 0;
  }

  if (value >= (float)INT32_MAX)
  {
    return INT32_MAX;
  }

  if (value <= (float)INT32_MIN)
  {
    return INT32_MIN;
  }

  return (int32_t)(value + ((value >= 0.0f) ? 0.5f : -0.5f));
}

static int32_t Int64ToInt32Saturated(int64_t value)
{
  if (value > INT32_MAX)
  {
    return INT32_MAX;
  }

  if (value < INT32_MIN)
  {
    return INT32_MIN;
  }

  return (int32_t)value;
}

static uint16_t MetersToMillimetersU16(float distance_m)
{
  float distance_mm;

  if (!isfinite(distance_m) || (distance_m <= 0.0f))
  {
    return 0U;
  }

  distance_mm = distance_m * 1000.0f;
  if (distance_mm >= (float)UINT16_MAX)
  {
    return UINT16_MAX;
  }

  return (uint16_t)(distance_mm + 0.5f);
}

static uint32_t ControlFaultsToCommFaults(
    uint32_t control_faults,
    const SafetyRequest *safety_request,
    const ImuState *imu_state)
{
  uint32_t comm_faults = CommService_GetFaultBits();

  if ((control_faults & CONTROL_FAULT_HARDWARE_INIT) != 0U)
  {
    comm_faults |= COMM_FAULT_INTERNAL;
  }

  if ((control_faults & CONTROL_FAULT_ENCODER_NOT_CALIBRATED) != 0U)
  {
    comm_faults |= COMM_FAULT_ENCODER_INVALID;
  }

  if ((control_faults &
       (CONTROL_FAULT_STEERING_NOT_CALIBRATED |
        CONTROL_FAULT_STEERING_SENSOR_INVALID |
        CONTROL_FAULT_STEERING_SERVO_INVALID)) != 0U)
  {
    comm_faults |= COMM_FAULT_STEERING_INVALID;
  }

  if (comm_timeout_fault_active)
  {
    comm_faults |= COMM_FAULT_COMM_TIMEOUT;
  }

  if ((imu_state == NULL) ||
      !ImuTask_IsHeadingReady(imu_state, HAL_GetTick()))
  {
    /* Report-only degradation: odometry automatically falls back to Ackermann. */
    comm_faults |= COMM_FAULT_IMU_LOST;
  }

  if (safety_request != NULL)
  {
    if ((safety_request->reason &
         (SAFETY_REASON_SENSOR_INIT |
          SAFETY_REASON_SENSOR_TIMEOUT |
          SAFETY_REASON_SENSOR_OUT_OF_RANGE)) != 0U)
    {
      comm_faults |=
          COMM_FAULT_RANGE_LOST | COMM_FAULT_SENSOR_STALE;
    }

    if ((safety_request->reason & SAFETY_REASON_SENSOR_STALE) != 0U)
    {
      comm_faults |=
          COMM_FAULT_RANGE_LOST | COMM_FAULT_SENSOR_STALE;
    }

    if ((safety_request->reason & SAFETY_REASON_DISTANCE_STOP) != 0U)
    {
      comm_faults |= COMM_FAULT_OBSTACLE_NEAR;
    }

    if ((safety_request->reason & SAFETY_REASON_MANUAL_ESTOP) != 0U)
    {
      comm_faults |= COMM_FAULT_ESTOP_ACTIVE;
    }
    else if (safety_request->latched)
    {
      comm_faults |= COMM_FAULT_INTERNAL;
    }
  }

  return comm_faults;
}

static CommSystemState DetermineCommState(
    const ControlState *control_state,
    const SafetyRequest *safety_request,
    uint32_t comm_faults)
{
  const uint32_t safe_stop_faults =
      COMM_FAULT_COMM_TIMEOUT |
      COMM_FAULT_ENCODER_INVALID |
      COMM_FAULT_STEERING_INVALID |
      COMM_FAULT_SENSOR_STALE |
      COMM_FAULT_OBSTACLE_NEAR;

  if ((comm_faults & COMM_FAULT_ESTOP_ACTIVE) != 0U)
  {
    return COMM_STATE_ESTOP;
  }

  if ((comm_faults &
       (COMM_FAULT_INTERNAL |
        COMM_FAULT_CONTROL_OVERRUN |
        COMM_FAULT_MOTOR_STALL |
        COMM_FAULT_DIRECTION)) != 0U)
  {
    return COMM_STATE_FAULT;
  }

  if (((comm_faults & safe_stop_faults) != 0U) ||
      ((safety_request != NULL) && safety_request->stop_request) ||
      CommService_IsRearmRequired())
  {
    return COMM_STATE_SAFE_STOP;
  }

  if ((control_state == NULL) ||
      (control_state->mode == CONTROL_MODE_DISABLED))
  {
    return COMM_STATE_READY;
  }

  return COMM_STATE_DRIVING;
}

static CommFaultAction DetermineFaultAction(uint32_t comm_faults,
                                            CommSystemState state)
{
  if ((comm_faults &
       (COMM_FAULT_MOTOR_STALL | COMM_FAULT_DIRECTION)) != 0U)
  {
    return COMM_FAULT_ACTION_LATCHED_STOP;
  }

  if ((state == COMM_STATE_ESTOP) ||
      ((comm_faults &
        (COMM_FAULT_CONTROL_OVERRUN | COMM_FAULT_INTERNAL)) != 0U))
  {
    return COMM_FAULT_ACTION_OUTPUT_DISABLE;
  }

  if (state == COMM_STATE_SAFE_STOP)
  {
    return COMM_FAULT_ACTION_SAFE_STOP;
  }

  return COMM_FAULT_ACTION_REPORT_ONLY;
}

static void UpdateCommunicationState(bool force)
{
  uint32_t now = HAL_GetTick();
  ControlState control_state;
  SafetyRequest safety_request = {0};
  ImuState imu_state = {0};
  uint32_t comm_faults;

  if (!force &&
      ((uint32_t)(now - last_comm_state_update_tick) <
       COMM_STATE_UPDATE_PERIOD_MS))
  {
    return;
  }

  last_comm_state_update_tick = now;
  Control_GetState(&control_state);
  SafetyTask_GetState(&safety_request);
  ImuTask_GetState(&imu_state);
  comm_faults = ControlFaultsToCommFaults(control_state.fault_flags,
                                          &safety_request,
                                          &imu_state);
  current_comm_state =
      DetermineCommState(&control_state, &safety_request, comm_faults);
  CommService_SetSystemState(current_comm_state);
}

static void ApplyPendingCommands(void)
{
  CommDriveCommand drive_command;
  CommStopCommand stop_command;
  CommResetFaultCommand reset_command;
  CommMotorDiagnosticCommand motor_command;
  CommServoDiagnosticCommand servo_command;

  if (CommService_TakeMotorDiagnosticCommand(&motor_command))
  {
    (void)Control_SetSteeringServoDiagnosticPulse(0U);
    comm_watchdog_armed = false;
    comm_timeout_fault_active = false;

    if (motor_command.duty_permille == 0)
    {
      Control_SetDisabled();
      motor_diagnostic_active = false;
    }
    else
    {
      Control_SetOpenLoopPercent(
          (float)motor_command.duty_permille / 10.0f);
      motor_diagnostic_active = true;
      motor_diagnostic_stop_tick =
          HAL_GetTick() + (uint32_t)motor_command.duration_ms;
    }
  }

  if (CommService_TakeServoDiagnosticCommand(&servo_command))
  {
    ControlState control_state;
    CommCommandResultCode result;
    uint8_t status_flags = 0U;

    Control_SetDisabled();
    motor_diagnostic_active = false;
    Control_GetState(&control_state);
    if (Control_SetSteeringServoDiagnosticPulse(
            servo_command.pulse_us))
    {
      result = COMM_RESULT_ACCEPTED;
    }
    else
    {
      result = COMM_RESULT_INVALID_STATE;
    }

    Control_GetState(&control_state);
    if (control_state.steering_servo_diagnostic_active)
    {
      status_flags |= (1U << 0);
    }
    if (control_state.steering_adc_valid)
    {
      status_flags |= (1U << 1);
    }
    if (control_state.steering_angle_valid)
    {
      status_flags |= (1U << 2);
    }

    (void)CommService_SendServoDiagnosticResponse(
        servo_command.request_sequence,
        result,
        servo_command.pulse_us,
        control_state.steering_adc_raw,
        status_flags);
  }

  if (CommService_TakeResetFaultCommand(&reset_command))
  {
    uint32_t control_fault_mask = 0U;

    if ((reset_command.acknowledged_fault_bits &
         COMM_FAULT_ENCODER_INVALID) != 0U)
    {
      control_fault_mask |= CONTROL_FAULT_ENCODER_NOT_CALIBRATED;
    }
    Control_ClearFaults(control_fault_mask);

    if ((reset_command.acknowledged_fault_bits &
         (COMM_FAULT_RANGE_LOST | COMM_FAULT_ESTOP_ACTIVE)) != 0U)
    {
      SafetyTask_RequestReset();
    }

    (void)CommService_SendCommandResult(
        COMM_MSG_CMD_RESET_FAULT,
        reset_command.request_sequence,
        COMM_RESULT_ACCEPTED,
        current_comm_state);
  }

  if (CommService_TakeDriveCommand(&drive_command))
  {
    (void)Control_SetSteeringServoDiagnosticPulse(0U);
    motor_diagnostic_active = false;
    comm_watchdog_armed = true;
    comm_timeout_fault_active = false;
    if (drive_command.drive_enable)
    {
      Control_SetSpeedTarget(
          (float)drive_command.target_speed_mm_s / 1000.0f,
          (float)drive_command.target_steering_cdeg / 100.0f);
      comm_timeout_fault_active = false;
    }
    else
    {
      Control_SetDisabled();
    }
  }

  /* STOP has the final say if several frames arrive in one CommTask cycle. */
  if (CommService_TakeStopCommand(&stop_command))
  {
    (void)stop_command;
    (void)Control_SetSteeringServoDiagnosticPulse(0U);
    Control_SetDisabled();
    motor_diagnostic_active = false;
  }

  if (motor_diagnostic_active &&
      ((int32_t)(HAL_GetTick() -
                 motor_diagnostic_stop_tick) >= 0))
  {
    Control_SetDisabled();
    motor_diagnostic_active = false;
  }

  if (comm_watchdog_armed && CommService_IsCommandTimedOut())
  {
    Control_SetDisabled();
    comm_timeout_fault_active = true;
  }
}

static void SendTelemetryIfDue(void)
{
  uint32_t now = HAL_GetTick();
  ControlState control_state;
  OdometryState odometry_state = {0};
  SafetyRequest safety_request = {0};
  ImuState imu_state = {0};
  CommDriveTelemetry telemetry = {0};
  CommImuTelemetry imu = {0};
  CommOdometryTelemetry odometry = {0};
  uint32_t comm_faults;

  if ((uint32_t)(now - last_telemetry_tick) < TELEMETRY_PERIOD_MS)
  {
    return;
  }
  last_telemetry_tick = now;

  Control_GetState(&control_state);
  OdometryTask_GetState(&odometry_state);
  SafetyTask_GetState(&safety_request);
  ImuTask_GetState(&imu_state);
  comm_faults = ControlFaultsToCommFaults(control_state.fault_flags,
                                          &safety_request,
                                          &imu_state);

  current_comm_state =
      DetermineCommState(&control_state, &safety_request, comm_faults);
  CommService_SetSystemState(current_comm_state);

  telemetry.mcu_time_ms = now;
  telemetry.target_speed_mm_s =
      FloatToInt16Saturated(control_state.target_speed_mps * 1000.0f);
  telemetry.measured_speed_mm_s =
      FloatToInt16Saturated(control_state.measured_speed_mps * 1000.0f);
  telemetry.motor_duty_permille =
      FloatToInt16Saturated(control_state.applied_pwm_percent * 10.0f);
  telemetry.steering_cmd_cdeg =
      FloatToInt16Saturated(control_state.target_steering_deg * 100.0f);
  telemetry.steering_feedback_cdeg =
      control_state.steering_angle_valid ?
          FloatToInt16Saturated(
              control_state.measured_steering_deg * 100.0f) :
          0;
  telemetry.encoder_count =
      Int64ToInt32Saturated(control_state.encoder_total_count);
  telemetry.yaw_cdeg = odometry_state.update_valid ?
      FloatToInt16Saturated(
          odometry_state.yaw_rad * ODOMETRY_RAD_TO_CDEG) :
      0;
  telemetry.state = current_comm_state;
  telemetry.last_drive_seq = CommService_GetLastDriveSequence();
  telemetry.active_fault_bits = comm_faults;

  if (!fault_event_initialized ||
      (last_fault_event_bits != comm_faults) ||
      (last_fault_event_state != current_comm_state))
  {
    CommFaultEvent event = {0};

    event.occurred_at_ms = now;
    event.active_fault_bits = comm_faults;
    if ((current_comm_state == COMM_STATE_FAULT) ||
        (current_comm_state == COMM_STATE_ESTOP))
    {
      event.latched_fault_bits =
          comm_faults &
          (COMM_FAULT_MOTOR_STALL |
           COMM_FAULT_DIRECTION |
           COMM_FAULT_ESTOP_ACTIVE |
           COMM_FAULT_INTERNAL);
    }
    event.action = DetermineFaultAction(comm_faults,
                                        current_comm_state);
    event.state = current_comm_state;
    if (CommService_SendFaultEvent(&event))
    {
      fault_event_initialized = true;
      last_fault_event_bits = comm_faults;
      last_fault_event_state = current_comm_state;
    }
  }

  (void)CommService_SendDriveTelemetry(&telemetry);

  imu.mcu_time_ms = now;
  imu.quaternion_i_q14 =
      FloatToInt16Saturated(imu_state.quaternion_i * 16384.0f);
  imu.quaternion_j_q14 =
      FloatToInt16Saturated(imu_state.quaternion_j * 16384.0f);
  imu.quaternion_k_q14 =
      FloatToInt16Saturated(imu_state.quaternion_k * 16384.0f);
  imu.quaternion_real_q14 =
      FloatToInt16Saturated(imu_state.quaternion_real * 16384.0f);
  imu.gyro_x_mdeg_s =
      FloatToInt32Saturated(
          imu_state.gyro_x_rad_s * ODOMETRY_RAD_TO_MDEG);
  imu.gyro_y_mdeg_s =
      FloatToInt32Saturated(
          imu_state.gyro_y_rad_s * ODOMETRY_RAD_TO_MDEG);
  imu.gyro_z_mdeg_s =
      FloatToInt32Saturated(
          imu_state.gyro_z_rad_s * ODOMETRY_RAD_TO_MDEG);
  imu.linear_accel_x_mm_s2 =
      FloatToInt16Saturated(imu_state.linear_accel_x_mps2 * 1000.0f);
  imu.linear_accel_y_mm_s2 =
      FloatToInt16Saturated(imu_state.linear_accel_y_mps2 * 1000.0f);
  imu.linear_accel_z_mm_s2 =
      FloatToInt16Saturated(imu_state.linear_accel_z_mps2 * 1000.0f);
  imu.yaw_mdeg =
      FloatToInt32Saturated(imu_state.yaw_rad * ODOMETRY_RAD_TO_MDEG);
  imu.gyro_accuracy = imu_state.gyro_accuracy;
  imu.linear_accel_accuracy = imu_state.linear_accel_accuracy;
  imu.quaternion_accuracy = imu_state.quaternion_accuracy;
  if ((imu_state.status_flags & IMU_STATUS_CONNECTED) != 0U)
  {
    imu.status_flags |= COMM_IMU_STATUS_CONNECTED;
  }
  if ((imu_state.status_flags & IMU_STATUS_GYRO_VALID) != 0U)
  {
    imu.status_flags |= COMM_IMU_STATUS_GYRO_VALID;
  }
  if ((imu_state.status_flags & IMU_STATUS_LINEAR_ACCEL_VALID) != 0U)
  {
    imu.status_flags |= COMM_IMU_STATUS_LINEAR_ACCEL_VALID;
  }
  if ((imu_state.status_flags & IMU_STATUS_QUATERNION_VALID) != 0U)
  {
    imu.status_flags |= COMM_IMU_STATUS_QUATERNION_VALID;
  }
  if ((imu_state.status_flags & IMU_STATUS_STALE) != 0U)
  {
    imu.status_flags |= COMM_IMU_STATUS_STALE;
  }
  if ((imu_state.status_flags & IMU_STATUS_SPI_ERROR) != 0U)
  {
    imu.status_flags |= COMM_IMU_STATUS_SPI_ERROR;
  }
  if ((imu_state.status_flags & IMU_STATUS_PROTOCOL_ERROR) != 0U)
  {
    imu.status_flags |= COMM_IMU_STATUS_PROTOCOL_ERROR;
  }
  (void)CommService_SendImuTelemetry(&imu);

  odometry.mcu_time_ms = odometry_state.updated_at_ms;
  odometry.x_mm =
      FloatToInt32Saturated(odometry_state.x_m * 1000.0f);
  odometry.y_mm =
      FloatToInt32Saturated(odometry_state.y_m * 1000.0f);
  odometry.yaw_mdeg =
      FloatToInt32Saturated(
          odometry_state.yaw_rad * ODOMETRY_RAD_TO_MDEG);
  odometry.distance_mm =
      FloatToInt32Saturated(odometry_state.distance_m * 1000.0f);
  odometry.linear_speed_mm_s =
      FloatToInt16Saturated(
          odometry_state.linear_speed_mps * 1000.0f);
  odometry.yaw_rate_mdeg_s =
      FloatToInt32Saturated(
          odometry_state.yaw_rate_rad_s *
          ODOMETRY_RAD_TO_MDEG);
  odometry.steering_cdeg =
      FloatToInt16Saturated(
          odometry_state.center_steering_angle_deg * 100.0f);
  odometry.curvature_micro_per_m =
      FloatToInt32Saturated(
          odometry_state.curvature_per_m * 1000000.0f);
  if (odometry_state.update_valid)
  {
    odometry.status_flags |= COMM_ODOMETRY_STATUS_VALID;
  }
  if (control_state.encoder_calibrated)
  {
    odometry.status_flags |=
        COMM_ODOMETRY_STATUS_ENCODER_CALIBRATED;
  }
#if VEHICLE_ODOMETRY_GEOMETRY_CALIBRATED
  odometry.status_flags |=
      COMM_ODOMETRY_STATUS_GEOMETRY_CALIBRATED;
#endif
  if (odometry_state.steering_estimated)
  {
    odometry.status_flags |=
        COMM_ODOMETRY_STATUS_STEERING_ESTIMATED;
    odometry.steering_source =
        COMM_ODOMETRY_STEERING_COMMAND_ESTIMATE;
  }
  else if (odometry_state.update_valid)
  {
    odometry.steering_source =
        COMM_ODOMETRY_STEERING_SENSOR;
  }
  else
  {
    odometry.steering_source = COMM_ODOMETRY_STEERING_NONE;
  }
  if (odometry_state.imu_fused)
  {
    odometry.status_flags |= COMM_ODOMETRY_STATUS_IMU_FUSED;
  }
  if ((odometry_state.status_flags &
       ODOMETRY_STATUS_IMU_HEADING_FALLBACK) != 0U)
  {
    odometry.status_flags |=
        COMM_ODOMETRY_STATUS_IMU_HEADING_FALLBACK;
  }
  if ((odometry_state.status_flags &
       (ODOMETRY_STATUS_GEOMETRY_INVALID |
        ODOMETRY_STATUS_STEERING_INVALID |
        ODOMETRY_STATUS_INPUT_INVALID)) != 0U)
  {
    odometry.status_flags |= COMM_ODOMETRY_STATUS_INPUT_INVALID;
  }
  odometry.last_drive_seq = CommService_GetLastDriveSequence();
  (void)CommService_SendOdometryTelemetry(&odometry);

  if ((uint32_t)(now - last_range_telemetry_tick) >=
      RANGE_TELEMETRY_PERIOD_MS)
  {
    CommRangeTelemetry range = {0};
    UltrasonicState ultrasonic_state = {0};

    last_range_telemetry_tick = now;
    range.mcu_time_ms = now;
    range.front_left_mm = UINT16_MAX;
    range.front_right_mm = UINT16_MAX;
    range.rear_left_mm = UINT16_MAX;
    range.rear_right_mm = UINT16_MAX;
    range.valid_mask = 0U;

    UltrasonicTask_GetState(&ultrasonic_state);
    if ((ultrasonic_state.status == ULTRA_STATUS_OK) &&
        (ultrasonic_state.sample_time_ms != 0U) &&
        ((uint32_t)(now - ultrasonic_state.sample_time_ms) <=
         SAFETY_STALE_TIMEOUT_MS))
    {
      uint16_t distance_mm =
          MetersToMillimetersU16(ultrasonic_state.distance_m);

      if (distance_mm != 0U)
      {
        /*
         * There is one installed forward HC-SR04. Until its left/right
         * placement is physically confirmed, expose it as the primary
         * front slot (front_left / valid bit 0) only. Do not fabricate a
         * second front sensor by copying the value to front_right.
         */
        range.front_left_mm = distance_mm;
        range.valid_mask = RANGE_VALID_FRONT_PRIMARY;
      }
    }
    (void)CommService_SendRangeTelemetry(&range);
  }
}

static void Comm_Task(void *argument)
{
  (void)argument;

  for (;;)
  {
    UpdateCommunicationState(false);
    CommService_Process();
    ApplyPendingCommands();
    SendTelemetryIfDue();
    (void)osDelay(COMM_TASK_PERIOD_MS);
  }
}

void App_FreeRTOS_Init(void)
{
  Control_Init();
  if (!UltrasonicTask_Init() ||
      !SafetyTask_Init() ||
      !ImuTask_Init(&hspi5) ||
      !OdometryTask_Init())
  {
    Error_Handler();
  }

  CommService_SetDriveLimits(VEHICLE_MAX_ABS_SPEED_MM_S,
                             VEHICLE_MIN_STEERING_CDEG,
                             VEHICLE_MAX_STEERING_CDEG);

  safety_task_handle =
      osThreadNew(Safety_Task, NULL, &safety_task_attributes);
  control_task_handle =
      osThreadNew(Control_Task, NULL, &control_task_attributes);
  ultrasonic_task_handle =
      osThreadNew(Ultrasonic_Task, NULL, &ultrasonic_task_attributes);
  imu_task_handle =
      osThreadNew(Imu_Task, NULL, &imu_task_attributes);
  odometry_task_handle =
      osThreadNew(Odometry_Task, NULL, &odometry_task_attributes);
  comm_task_handle =
      osThreadNew(Comm_Task, NULL, &comm_task_attributes);

  if ((safety_task_handle == NULL) ||
      (control_task_handle == NULL) ||
      (ultrasonic_task_handle == NULL) ||
      (imu_task_handle == NULL) ||
      (odometry_task_handle == NULL) ||
      (comm_task_handle == NULL))
  {
    Error_Handler();
  }
}
