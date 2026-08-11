#ifndef APP_TASK_ULTRASONIC_H
#define APP_TASK_ULTRASONIC_H

#ifdef __cplusplus
extern "C" {
#endif

#include "ultrasonic.h"

#include <stdbool.h>

bool UltrasonicTask_Init(void);
void Ultrasonic_Task(void *argument);
void UltrasonicTask_GetState(UltrasonicState *state);

#ifdef __cplusplus
}
#endif

#endif
