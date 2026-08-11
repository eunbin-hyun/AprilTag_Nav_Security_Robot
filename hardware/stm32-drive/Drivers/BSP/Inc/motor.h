#ifndef BSP_MOTOR_H
#define BSP_MOTOR_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdbool.h>

/*
 * BTS7960 mapping used by this project:
 *   TIM5_CH1 (PA0) -> RPWM / forward PWM
 *   TIM5_CH4 (PA3) -> LPWM / reverse PWM
 *   PE2            -> R_EN
 *   PE3            -> L_EN
 */
bool Motor_Init(void);
void Motor_Enable(void);
void Motor_Disable(void);
bool Motor_IsEnabled(void);

/*
 * Emergency disable is latched. Motor_Enable() cannot raise PE2/PE3 again
 * until Motor_ClearEmergencyDisable() succeeds while PWM remains zero.
 */
void Motor_EmergencyDisable(void);
bool Motor_ClearEmergencyDisable(void);
bool Motor_IsEmergencyDisabled(void);

/* Positive is forward, negative is reverse, and zero is coast/stop. */
void Motor_SetPercent(float percent);
float Motor_GetAppliedPercent(void);

/*
 * Set true if the installed motor runs backwards for a positive command.
 * This changes only the software direction convention.
 */
void Motor_SetDirectionInverted(bool inverted);

#ifdef __cplusplus
}
#endif

#endif
