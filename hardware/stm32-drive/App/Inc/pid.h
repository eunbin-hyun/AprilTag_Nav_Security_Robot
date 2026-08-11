#ifndef APP_PID_H
#define APP_PID_H

#ifndef PID_ANTI_WINDUP_ENABLED
#define PID_ANTI_WINDUP_ENABLED 0
#endif



#include <stdbool.h>

typedef struct
{
  float kp;
  float ki;
  float kd;
  float output_min;
  float output_max;
  float integral;
  float previous_error;
  bool previous_error_valid;
} PIDController;

void PID_Init(PIDController *pid,
              float kp,
              float ki,
              float kd,
              float output_min,
              float output_max);
void PID_Reset(PIDController *pid);
void PID_SetGains(PIDController *pid, float kp, float ki, float kd);
void PID_SetOutputLimits(PIDController *pid,
                         float output_min,
                         float output_max);
float PID_Update(PIDController *pid,
                 float target,
                 float measurement,
                 float dt_seconds);


#endif
