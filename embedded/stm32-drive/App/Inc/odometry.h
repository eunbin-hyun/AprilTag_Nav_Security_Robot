#ifndef APP_ODOMETRY_H
#define APP_ODOMETRY_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdbool.h>
#include <stdint.h>

typedef enum
{
  ODOMETRY_STEERING_SENSOR_LEFT = 0,
  ODOMETRY_STEERING_SENSOR_RIGHT = 1
} OdometrySteeringSensorSide;

typedef enum
{
  ODOMETRY_HEADING_MODEL_ONLY = 0U,
  ODOMETRY_HEADING_COMPLEMENTARY = 1U,
  ODOMETRY_HEADING_IMU_ONLY = 2U
} OdometryHeadingMode;

typedef enum
{
  ODOMETRY_STATUS_NONE = 0U,
  ODOMETRY_STATUS_GEOMETRY_INVALID = (1UL << 0),
  ODOMETRY_STATUS_STEERING_INVALID = (1UL << 1),
  ODOMETRY_STATUS_INPUT_INVALID = (1UL << 2),
  ODOMETRY_STATUS_STEERING_ESTIMATED = (1UL << 3),
  ODOMETRY_STATUS_IMU_FUSED = (1UL << 4),
  ODOMETRY_STATUS_IMU_HEADING_FALLBACK = (1UL << 5)
} OdometryStatus;

typedef struct
{
  float wheelbase_m;
  float front_steering_track_m;
  OdometrySteeringSensorSide sensor_side;
  bool valid;
} OdometryGeometry;

typedef struct
{
  float x_m;
  float y_m;
  float yaw_rad;
  float linear_speed_mps;
  float yaw_rate_rad_s;
  float measured_wheel_angle_deg;
  float center_steering_angle_deg;
  float curvature_per_m;
  float distance_m;
  bool update_valid;
  bool steering_estimated;
  bool imu_fused;
  uint32_t status_flags;
  uint32_t updated_at_ms;
} OdometryState;

typedef struct
{
  OdometryGeometry geometry;
  OdometryState state;
  bool imu_heading_reference_valid;
  float imu_heading_offset_rad;
} OdometryContext;

void Odometry_Init(OdometryContext *context);
bool Odometry_SetGeometry(OdometryContext *context,
                          float wheelbase_m,
                          float front_steering_track_m,
                          OdometrySteeringSensorSide sensor_side);
void Odometry_ResetPose(OdometryContext *context,
                        float x_m,
                        float y_m,
                        float yaw_rad);
bool Odometry_Update(OdometryContext *context,
                     float delta_distance_m,
                     float linear_speed_mps,
                     float measured_wheel_angle_deg,
                     bool steering_valid,
                     float dt_seconds,
                     uint32_t now_ms);
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
    float heading_correction_weight);
bool Odometry_UpdateFromCenterSteering(
    OdometryContext *context,
    float delta_distance_m,
    float linear_speed_mps,
    float center_steering_angle_deg,
    bool steering_valid,
    float dt_seconds,
    uint32_t now_ms);
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
    float heading_correction_weight);

#ifdef __cplusplus
}
#endif

#endif
