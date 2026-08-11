#include "pid.h"

#include <stddef.h>

static float ClampOutput(const PIDController *pid, float value)
{
  if (value > pid->output_max)
  {
    return pid->output_max;
  }

  if (value < pid->output_min)
  {
    return pid->output_min;
  }

  return value;
}

void PID_Init(PIDController *pid,
              float kp,
              float ki,
              float kd,
              float output_min,
              float output_max)
{
  if (pid == NULL)
  {
    return;
  }

  pid->kp = kp;
  pid->ki = ki;
  pid->kd = kd;
  pid->output_min = output_min;
  pid->output_max = output_max;
  PID_Reset(pid);
}

void PID_Reset(PIDController *pid)
{
  if (pid == NULL)
  {
    return;
  }

  pid->integral = 0.0f;
  pid->previous_error = 0.0f;
  pid->previous_error_valid = false;
}

void PID_SetGains(PIDController *pid, float kp, float ki, float kd)
{
  if (pid == NULL)
  {
    return;
  }

  pid->kp = kp;
  pid->ki = ki;
  pid->kd = kd;
}

void PID_SetOutputLimits(PIDController *pid,
                         float output_min,
                         float output_max)
{
  if ((pid == NULL) || (output_min >= output_max))
  {
    return;
  }

  pid->output_min = output_min;
  pid->output_max = output_max;
  pid->integral = ClampOutput(pid, pid->integral);
}

float PID_Update(PIDController *pid,
                 float target,
                 float measurement,
                 float dt_seconds)
{
  float error;
  float derivative = 0.0f;
  float integral_candidate;
  float unsaturated_output;
  float output;

  if ((pid == NULL) || (dt_seconds <= 0.0f))
  {
    return 0.0f;
  }

  error = target - measurement;
  if (pid->previous_error_valid)
  {
    derivative = (error - pid->previous_error) / dt_seconds;
  }

  integral_candidate = pid->integral + (error * dt_seconds);
  unsaturated_output = (pid->kp * error) +
                       (pid->ki * integral_candidate) +
                       (pid->kd * derivative);
  output = ClampOutput(pid, unsaturated_output);

#if PID_ANTI_WINDUP_ENABLED

  /*
   * Conditional integration prevents the integral term from winding up while
   * the output is saturated, but allows it to unwind toward the valid range.
   */
  if ((unsaturated_output == output) ||
      ((output >= pid->output_max) && (error < 0.0f)) ||
      ((output <= pid->output_min) && (error > 0.0f)))
  {
    pid->integral = integral_candidate;
  }

#else

  /*
   * Experimental anti-windup OFF: keep integrating regardless of output
   * saturation.
   */
  pid->integral = integral_candidate;

#endif

  pid->previous_error = error;
  pid->previous_error_valid = true;
  return output;
}
