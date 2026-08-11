#include "odometry.h"

#include <math.h>
#include <string.h>

#define ODOMETRY_DEG_TO_RAD       0.01745329251994329577f
#define ODOMETRY_RAD_TO_DEG       57.295779513082320876f
#define ODOMETRY_MIN_DENOMINATOR  0.000001f
#define ODOMETRY_MAX_STEERING_DEG 89.0f
#define ODOMETRY_PI               3.14159265358979323846f
#define ODOMETRY_TWO_PI           (2.0f * ODOMETRY_PI)

static float NormalizeYaw(float yaw_rad)
{
  while (yaw_rad > ODOMETRY_PI)
  {
    yaw_rad -= ODOMETRY_TWO_PI;
  }

  while (yaw_rad < -ODOMETRY_PI)
  {
    yaw_rad += ODOMETRY_TWO_PI;
  }

  return yaw_rad;
}

void Odometry_Init(OdometryContext *context)
{
  if (context == NULL)
  {
    return;
  }

  memset(context, 0, sizeof(*context));
  context->state.status_flags = ODOMETRY_STATUS_GEOMETRY_INVALID;
  context->imu_heading_reference_valid = false;
}

bool Odometry_SetGeometry(OdometryContext *context,
                          float wheelbase_m,
                          float front_steering_track_m,
                          OdometrySteeringSensorSide sensor_side)
{
  bool valid;

  if (context == NULL)
  {
    return false;
  }

  valid = isfinite(wheelbase_m) &&
          isfinite(front_steering_track_m) &&
          (wheelbase_m > 0.0f) &&
          (front_steering_track_m > 0.0f) &&
          ((sensor_side == ODOMETRY_STEERING_SENSOR_LEFT) ||
           (sensor_side == ODOMETRY_STEERING_SENSOR_RIGHT));

  context->geometry.wheelbase_m = wheelbase_m;
  context->geometry.front_steering_track_m =
      front_steering_track_m;
  context->geometry.sensor_side = sensor_side;
  context->geometry.valid = valid;

  if (!valid)
  {
    context->state.update_valid = false;
    context->state.status_flags |=
        ODOMETRY_STATUS_GEOMETRY_INVALID;
  }
  else
  {
    context->state.status_flags &=
        ~ODOMETRY_STATUS_GEOMETRY_INVALID;
  }

  return valid;
}

void Odometry_ResetPose(OdometryContext *context,
                        float x_m,
                        float y_m,
                        float yaw_rad)
{
  if ((context == NULL) ||
      !isfinite(x_m) ||
      !isfinite(y_m) ||
      !isfinite(yaw_rad))
  {
    return;
  }

  context->state.x_m = x_m;
  context->state.y_m = y_m;
  context->state.yaw_rad = NormalizeYaw(yaw_rad);
  context->state.distance_m = 0.0f;
  context->state.linear_speed_mps = 0.0f;
  context->state.yaw_rate_rad_s = 0.0f;
  context->state.curvature_per_m = 0.0f;
  context->state.update_valid = false;
  context->imu_heading_reference_valid = false;
}

static bool BeginUpdate(OdometryContext *context,
                        float delta_distance_m,
                        float linear_speed_mps,
                        bool steering_valid,
                        float dt_seconds,
                        uint32_t now_ms)
{
  if (context == NULL)
  {
    return false;
  }

  context->state.update_valid = false;
  context->state.steering_estimated = false;
  context->state.imu_fused = false;
  context->state.updated_at_ms = now_ms;
  context->state.status_flags &=
      ~(ODOMETRY_STATUS_STEERING_ESTIMATED |
        ODOMETRY_STATUS_IMU_FUSED |
        ODOMETRY_STATUS_IMU_HEADING_FALLBACK);

  if (!context->geometry.valid)
  {
    context->state.status_flags |=
        ODOMETRY_STATUS_GEOMETRY_INVALID;
    return false;
  }
  context->state.status_flags &=
      ~ODOMETRY_STATUS_GEOMETRY_INVALID;

  if (!steering_valid)
  {
    context->state.status_flags |=
        ODOMETRY_STATUS_STEERING_INVALID;
    return false;
  }
  context->state.status_flags &=
      ~ODOMETRY_STATUS_STEERING_INVALID;

  if (!isfinite(delta_distance_m) ||
      !isfinite(linear_speed_mps) ||
      !isfinite(dt_seconds) ||
      (dt_seconds <= 0.0f))
  {
    context->state.status_flags |= ODOMETRY_STATUS_INPUT_INVALID;
    return false;
  }
  context->state.status_flags &= ~ODOMETRY_STATUS_INPUT_INVALID;
  return true;
}

static bool IntegrateWithCurvature(
    OdometryContext *context,
    float delta_distance_m,
    float linear_speed_mps,
    float measured_wheel_angle_deg,
    float center_steering_angle_deg,
    float curvature,
    bool steering_estimated,
    float dt_seconds,
    bool imu_heading_valid,
    float imu_yaw_rad,
    OdometryHeadingMode heading_mode,
    float heading_correction_weight)
{
  float model_delta_yaw;
  float delta_yaw;
  float model_heading;
  float next_heading;
  float heading_midpoint;
  bool imu_fused = false;
  bool imu_fallback = false;

  if (!isfinite(center_steering_angle_deg) ||
      !isfinite(curvature) ||
      (fabsf(center_steering_angle_deg) >=
       ODOMETRY_MAX_STEERING_DEG))
  {
    context->state.status_flags |= ODOMETRY_STATUS_INPUT_INVALID;
    return false;
  }
  context->state.status_flags &= ~ODOMETRY_STATUS_INPUT_INVALID;

  if ((heading_mode > ODOMETRY_HEADING_IMU_ONLY) ||
      !isfinite(heading_correction_weight) ||
      (heading_correction_weight < 0.0f) ||
      (heading_correction_weight > 1.0f))
  {
    context->state.status_flags |= ODOMETRY_STATUS_INPUT_INVALID;
    return false;
  }

  model_delta_yaw = delta_distance_m * curvature;
  model_heading =
      NormalizeYaw(context->state.yaw_rad + model_delta_yaw);
  next_heading = model_heading;

  if ((heading_mode != ODOMETRY_HEADING_MODEL_ONLY) &&
      imu_heading_valid && isfinite(imu_yaw_rad))
  {
    float imu_heading_aligned;

    if (!context->imu_heading_reference_valid)
    {
      context->imu_heading_offset_rad =
          NormalizeYaw(context->state.yaw_rad - imu_yaw_rad);
      context->imu_heading_reference_valid = true;
    }
    imu_heading_aligned =
        NormalizeYaw(imu_yaw_rad + context->imu_heading_offset_rad);

    if (heading_mode == ODOMETRY_HEADING_COMPLEMENTARY)
    {
      float heading_error =
          NormalizeYaw(imu_heading_aligned - model_heading);
      next_heading = NormalizeYaw(
          model_heading + (heading_correction_weight * heading_error));
    }
    else
    {
      next_heading = imu_heading_aligned;
    }
    imu_fused = true;
  }
  else if (heading_mode != ODOMETRY_HEADING_MODEL_ONLY)
  {
    imu_fallback = true;
  }

  delta_yaw = NormalizeYaw(next_heading - context->state.yaw_rad);
  heading_midpoint = NormalizeYaw(
      context->state.yaw_rad + (delta_yaw * 0.5f));

  context->state.x_m += delta_distance_m * cosf(heading_midpoint);
  context->state.y_m += delta_distance_m * sinf(heading_midpoint);
  context->state.yaw_rad = next_heading;
  context->state.distance_m += delta_distance_m;
  context->state.linear_speed_mps = linear_speed_mps;
  context->state.yaw_rate_rad_s = delta_yaw / dt_seconds;
  context->state.measured_wheel_angle_deg =
      measured_wheel_angle_deg;
  context->state.center_steering_angle_deg =
      center_steering_angle_deg;
  context->state.curvature_per_m = curvature;
  context->state.steering_estimated = steering_estimated;
  context->state.imu_fused = imu_fused;
  if (steering_estimated)
  {
    context->state.status_flags |=
        ODOMETRY_STATUS_STEERING_ESTIMATED;
  }
  else
  {
    context->state.status_flags &=
        ~ODOMETRY_STATUS_STEERING_ESTIMATED;
  }
  if (imu_fused)
  {
    context->state.status_flags |= ODOMETRY_STATUS_IMU_FUSED;
  }
  else
  {
    context->state.status_flags &= ~ODOMETRY_STATUS_IMU_FUSED;
  }
  if (imu_fallback)
  {
    context->state.status_flags |= ODOMETRY_STATUS_IMU_HEADING_FALLBACK;
  }
  else
  {
    context->state.status_flags &= ~ODOMETRY_STATUS_IMU_HEADING_FALLBACK;
  }
  context->state.update_valid = true;
  return true;
}

bool Odometry_UpdateWithHeading(
    OdometryContext *context,
    float delta_distance_m,
    float linear_speed_mps,
    float measured_wheel_angle_deg,
    bool steering_valid,
    float dt_seconds,
    uint32_t now_ms,
    bool imu_heading_valid,
    float imu_yaw_rad,
    OdometryHeadingMode heading_mode,
    float heading_correction_weight)
{
  float wheel_angle_rad;
  float tangent;
  float lateral_offset_m;
  float denominator;
  float curvature;
  float center_steering_angle_deg;

  if (!BeginUpdate(context,
                   delta_distance_m,
                   linear_speed_mps,
                   steering_valid,
                   dt_seconds,
                   now_ms))
  {
    return false;
  }

  if (!isfinite(measured_wheel_angle_deg) ||
      (fabsf(measured_wheel_angle_deg) >=
       ODOMETRY_MAX_STEERING_DEG))
  {
    context->state.status_flags |= ODOMETRY_STATUS_INPUT_INVALID;
    return false;
  }

  wheel_angle_rad =
      measured_wheel_angle_deg * ODOMETRY_DEG_TO_RAD;
  tangent = tanf(wheel_angle_rad);
  lateral_offset_m =
      context->geometry.front_steering_track_m * 0.5f;
  if (context->geometry.sensor_side ==
      ODOMETRY_STEERING_SENSOR_RIGHT)
  {
    lateral_offset_m = -lateral_offset_m;
  }

  /*
   * Ackermann geometry for one measured front wheel:
   *
   *   tan(alpha) = L / (R - y_sensor)
   *   curvature  = 1 / R
   *              = tan(alpha) / (L + y_sensor * tan(alpha))
   */
  denominator = context->geometry.wheelbase_m +
                lateral_offset_m * tangent;
  if (fabsf(denominator) < ODOMETRY_MIN_DENOMINATOR)
  {
    context->state.status_flags |= ODOMETRY_STATUS_INPUT_INVALID;
    return false;
  }

  curvature = tangent / denominator;
  center_steering_angle_deg =
      atanf(context->geometry.wheelbase_m * curvature) *
      ODOMETRY_RAD_TO_DEG;

  return IntegrateWithCurvature(context,
                                delta_distance_m,
                                linear_speed_mps,
                                measured_wheel_angle_deg,
                                center_steering_angle_deg,
                                curvature,
                                false,
                                dt_seconds,
                                imu_heading_valid,
                                imu_yaw_rad,
                                heading_mode,
                                heading_correction_weight);
}

bool Odometry_Update(OdometryContext *context,
                     float delta_distance_m,
                     float linear_speed_mps,
                     float measured_wheel_angle_deg,
                     bool steering_valid,
                     float dt_seconds,
                     uint32_t now_ms)
{
  return Odometry_UpdateWithHeading(
      context,
      delta_distance_m,
      linear_speed_mps,
      measured_wheel_angle_deg,
      steering_valid,
      dt_seconds,
      now_ms,
      false,
      0.0f,
      ODOMETRY_HEADING_MODEL_ONLY,
      0.0f);
}

bool Odometry_UpdateFromCenterSteeringWithHeading(
    OdometryContext *context,
    float delta_distance_m,
    float linear_speed_mps,
    float center_steering_angle_deg,
    bool steering_valid,
    float dt_seconds,
    uint32_t now_ms,
    bool imu_heading_valid,
    float imu_yaw_rad,
    OdometryHeadingMode heading_mode,
    float heading_correction_weight)
{
  float center_angle_rad;
  float curvature;

  if (!BeginUpdate(context,
                   delta_distance_m,
                   linear_speed_mps,
                   steering_valid,
                   dt_seconds,
                   now_ms))
  {
    return false;
  }

  if (!isfinite(center_steering_angle_deg) ||
      (fabsf(center_steering_angle_deg) >=
       ODOMETRY_MAX_STEERING_DEG))
  {
    context->state.status_flags |= ODOMETRY_STATUS_INPUT_INVALID;
    return false;
  }

  center_angle_rad =
      center_steering_angle_deg * ODOMETRY_DEG_TO_RAD;
  curvature =
      tanf(center_angle_rad) / context->geometry.wheelbase_m;

  /*
   * The servo LUT already stores a center-equivalent Ackermann angle.
   * Do not apply the one-wheel track-offset correction a second time.
   */
  return IntegrateWithCurvature(context,
                                delta_distance_m,
                                linear_speed_mps,
                                0.0f,
                                center_steering_angle_deg,
                                curvature,
                                true,
                                dt_seconds,
                                imu_heading_valid,
                                imu_yaw_rad,
                                heading_mode,
                                heading_correction_weight);
}

bool Odometry_UpdateFromCenterSteering(
    OdometryContext *context,
    float delta_distance_m,
    float linear_speed_mps,
    float center_steering_angle_deg,
    bool steering_valid,
    float dt_seconds,
    uint32_t now_ms)
{
  return Odometry_UpdateFromCenterSteeringWithHeading(
      context,
      delta_distance_m,
      linear_speed_mps,
      center_steering_angle_deg,
      steering_valid,
      dt_seconds,
      now_ms,
      false,
      0.0f,
      ODOMETRY_HEADING_MODEL_ONLY,
      0.0f);
}
