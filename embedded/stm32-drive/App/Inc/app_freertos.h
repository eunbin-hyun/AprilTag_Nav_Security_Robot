#ifndef APP_FREERTOS_H
#define APP_FREERTOS_H

#ifdef __cplusplus
extern "C" {
#endif

/* Called after osKernelInitialize() and before osKernelStart(). */
void App_FreeRTOS_Init(void);

#ifdef __cplusplus
}
#endif

#endif
