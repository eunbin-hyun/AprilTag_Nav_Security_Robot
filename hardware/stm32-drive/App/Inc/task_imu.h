#ifndef APP_TASK_IMU_H
#define APP_TASK_IMU_H

#ifdef __cplusplus
extern "C" {
#endif

#include "stm32f4xx_hal.h"

#include <stdbool.h>
#include <stdint.h>

typedef enum
{
  IMU_STATUS_NONE = 0U,
  IMU_STATUS_CONNECTED = (1UL << 0),
  IMU_STATUS_GYRO_VALID = (1UL << 1),
  IMU_STATUS_LINEAR_ACCEL_VALID = (1UL << 2),
  IMU_STATUS_QUATERNION_VALID = (1UL << 3),
  IMU_STATUS_STALE = (1UL << 4),
  IMU_STATUS_SPI_ERROR = (1UL << 5),
  IMU_STATUS_PROTOCOL_ERROR = (1UL << 6)
} ImuStatus;

typedef struct
{
  float gyro_x_rad_s;
  float gyro_y_rad_s;
  float gyro_z_rad_s;
  float linear_accel_x_mps2;
  float linear_accel_y_mps2;
  float linear_accel_z_mps2;
  float quaternion_i;
  float quaternion_j;
  float quaternion_k;
  float quaternion_real;
  float yaw_rad;
  uint8_t gyro_accuracy;
  uint8_t linear_accel_accuracy;
  uint8_t quaternion_accuracy;
  uint32_t gyro_updated_at_ms;
  uint32_t linear_accel_updated_at_ms;
  uint32_t quaternion_updated_at_ms;
  uint32_t last_packet_at_ms;
  uint32_t sensor_timestamp_us;
  uint32_t status_flags;
  uint32_t reset_count;
  uint32_t received_packets;
  uint32_t spi_errors;
  uint32_t protocol_errors;
  uint32_t continuation_packets;
  uint8_t sw_version_major;
  uint8_t sw_version_minor;
  uint16_t sw_version_patch;
  uint32_t sw_part_number;
  uint32_t sw_build_number;
} ImuState;

bool ImuTask_Init(SPI_HandleTypeDef *spi);
void Imu_Task(void *argument);
void ImuTask_GetState(ImuState *state);
bool ImuTask_IsFusionReady(const ImuState *state, uint32_t now_ms);
bool ImuTask_IsHeadingReady(const ImuState *state, uint32_t now_ms);

/* Called by HAL_GPIO_EXTI_Callback; no SPI transaction occurs in the ISR. */
void ImuTask_OnDataReadyInterrupt(void);

#ifdef __cplusplus
}
#endif

#endif
