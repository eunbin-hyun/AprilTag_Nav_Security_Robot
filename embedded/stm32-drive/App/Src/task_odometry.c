#include "task_odometry.h"

#include "cmsis_os2.h"
#include "task_control.h"
#include "task_imu.h"
#include "vehicle_config.h"

#include <math.h>
#include <string.h>

#define ODOMETRY_TASK_PERIOD_MS      20U
#define ODOMETRY_TASK_PERIOD_SECONDS 0.020f

static osMutexId_t odometry_mutex;
static OdometryContext odometry_context;

bool OdometryTask_Init(void)
{
  const osMutexAttr_t mutex_attributes = {
    .name = "OdometryState",
  };

  Odometry_Init(&odometry_context);
  odometry_mutex = osMutexNew(&mutex_attributes);
  if (odometry_mutex == NULL)
  {
    return false;
  }

#if VEHICLE_ODOMETRY_GEOMETRY_CALIBRATED
  return Odometry_SetGeometry(
      &odometry_context,
      VEHICLE_WHEELBASE_M,
      VEHICLE_FRONT_STEERING_TRACK_M,
      (VEHICLE_STEERING_SENSOR_SIDE == 0U) ?
          ODOMETRY_STEERING_SENSOR_LEFT :
          ODOMETRY_STEERING_SENSOR_RIGHT);
#else
  return true;
#endif
}

void Odometry_Task(void *argument)
{
  bool baseline_valid = false;
  float previous_distance_m = 0.0f;
  uint32_t next_wake_tick;

  (void)argument;
  next_wake_tick = osKernelGetTickCount();

  for (;;)
  {
    ControlState control_state;
    ImuState imu_state;
    float delta_distance_m = 0.0f;
    bool imu_heading_valid;
    uint32_t now_ms;

    memset(&control_state, 0, sizeof(control_state));
    memset(&imu_state, 0, sizeof(imu_state));
    Control_GetState(&control_state);
    ImuTask_GetState(&imu_state);
    now_ms = osKernelGetTickCount();
    imu_heading_valid = ImuTask_IsHeadingReady(&imu_state, now_ms);

    if (control_state.encoder_calibrated)
    {
      if (baseline_valid)
      {
        delta_distance_m =
            control_state.encoder_total_distance_m -
            previous_distance_m;
      }
      previous_distance_m =
          control_state.encoder_total_distance_m;
      baseline_valid = true;
    }
    else
    {
      baseline_valid = false;
    }

    if (osMutexAcquire(odometry_mutex, osWaitForever) == osOK)
    {
      if (control_state.steering_angle_valid)
      {
        (void)Odometry_UpdateWithHeading(
            &odometry_context,
            delta_distance_m,
            control_state.measured_speed_mps,
            control_state.measured_steering_deg,
            control_state.encoder_calibrated,
            ODOMETRY_TASK_PERIOD_SECONDS,
            now_ms,
            imu_heading_valid,
            imu_state.yaw_rad,
            (OdometryHeadingMode)VEHICLE_ODOMETRY_HEADING_MODE,
            VEHICLE_IMU_HEADING_CORRECTION_WEIGHT);
      }
#if VEHICLE_ALLOW_STEERING_COMMAND_ESTIMATE
      else
      {
        /*
         * The seven-point servo LUT stores center-equivalent steering
         * angles. Integrate the commanded angle directly and mark the
         * resulting pose as steering-estimated.
         */
        (void)Odometry_UpdateFromCenterSteeringWithHeading(
            &odometry_context,
            delta_distance_m,
            control_state.measured_speed_mps,
            control_state.target_steering_deg,
            control_state.encoder_calibrated &&
                control_state.steering_servo_calibrated,
            ODOMETRY_TASK_PERIOD_SECONDS,
            now_ms,
            imu_heading_valid,
            imu_state.yaw_rad,
            (OdometryHeadingMode)VEHICLE_ODOMETRY_HEADING_MODE,
            VEHICLE_IMU_HEADING_CORRECTION_WEIGHT);
      }
#else
      else
      {
        (void)Odometry_UpdateFromCenterSteering(
            &odometry_context,
            delta_distance_m,
            control_state.measured_speed_mps,
            0.0f,
            false,
            ODOMETRY_TASK_PERIOD_SECONDS,
            osKernelGetTickCount());
      }
#endif
      (void)osMutexRelease(odometry_mutex);
    }

    next_wake_tick += ODOMETRY_TASK_PERIOD_MS;
    (void)osDelayUntil(next_wake_tick);
  }
}

bool OdometryTask_SetGeometry(float wheelbase_m,
                              float front_steering_track_m,
                              OdometrySteeringSensorSide sensor_side)
{
  bool result = false;

  if (odometry_mutex == NULL)
  {
    return false;
  }

  if (osMutexAcquire(odometry_mutex, osWaitForever) == osOK)
  {
    result = Odometry_SetGeometry(&odometry_context,
                                  wheelbase_m,
                                  front_steering_track_m,
                                  sensor_side);
    (void)osMutexRelease(odometry_mutex);
  }

  return result;
}

void OdometryTask_ResetPose(float x_m, float y_m, float yaw_rad)
{
  if (odometry_mutex == NULL)
  {
    return;
  }

  if (osMutexAcquire(odometry_mutex, osWaitForever) == osOK)
  {
    Odometry_ResetPose(&odometry_context, x_m, y_m, yaw_rad);
    (void)osMutexRelease(odometry_mutex);
  }
}

void OdometryTask_GetState(OdometryState *state)
{
  if ((state == NULL) || (odometry_mutex == NULL))
  {
    return;
  }

  if (osMutexAcquire(odometry_mutex, osWaitForever) == osOK)
  {
    *state = odometry_context.state;
    (void)osMutexRelease(odometry_mutex);
  }
}
