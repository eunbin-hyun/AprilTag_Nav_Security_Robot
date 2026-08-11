#ifndef APP_TASK_SAFETY_H
#define APP_TASK_SAFETY_H

#ifdef __cplusplus
extern "C" {
#endif

#include "safety.h"

#include <stdbool.h>

bool SafetyTask_Init(void);
void Safety_Task(void *argument);
void SafetyTask_RequestReset(void);
void SafetyTask_GetState(SafetyRequest *request);

#ifdef __cplusplus
}
#endif

#endif
