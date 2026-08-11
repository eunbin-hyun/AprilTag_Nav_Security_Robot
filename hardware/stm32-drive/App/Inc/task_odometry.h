#ifndef APP_TASK_ODOMETRY_H
#define APP_TASK_ODOMETRY_H

#ifdef __cplusplus
extern "C" {
#endif

#include "odometry.h"

#include <stdbool.h>

bool OdometryTask_Init(void);
void Odometry_Task(void *argument);
bool OdometryTask_SetGeometry(float wheelbase_m,
                              float front_steering_track_m,
                              OdometrySteeringSensorSide sensor_side);
void OdometryTask_ResetPose(float x_m, float y_m, float yaw_rad);
void OdometryTask_GetState(OdometryState *state);

#ifdef __cplusplus
}
#endif

#endif
