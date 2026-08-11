#ifndef BSP_BNO085_H
#define BSP_BNO085_H

#ifdef __cplusplus
extern "C" {
#endif

#include "stm32f4xx_hal.h"

#include <stdbool.h>
#include <stdint.h>

#define BNO085_DEFAULT_REPORT_INTERVAL_US 10000UL

typedef void (*BNO085_DelayMilliseconds)(uint32_t delay_ms);

typedef enum
{
  BNO085_RESULT_OK = 0,
  BNO085_RESULT_NO_DATA,
  BNO085_RESULT_ARGUMENT_ERROR,
  BNO085_RESULT_TIMEOUT,
  BNO085_RESULT_SPI_ERROR,
  BNO085_RESULT_PROTOCOL_ERROR
} BNO085_Result;

typedef enum
{
  BNO085_UPDATE_NONE = 0U,
  BNO085_UPDATE_GYROSCOPE = (1UL << 0),
  BNO085_UPDATE_LINEAR_ACCELERATION = (1UL << 1),
  BNO085_UPDATE_GAME_ROTATION_VECTOR = (1UL << 2)
} BNO085_UpdateFlags;

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
  uint8_t gyro_accuracy;
  uint8_t linear_accel_accuracy;
  uint8_t quaternion_accuracy;
  uint32_t sensor_timestamp_us;
} BNO085_Sample;

typedef struct
{
  uint8_t sw_version_major;
  uint8_t sw_version_minor;
  uint16_t sw_version_patch;
  uint32_t sw_part_number;
  uint32_t sw_build_number;
  bool valid;
} BNO085_ProductInfo;

typedef struct
{
  SPI_HandleTypeDef *spi;
  GPIO_TypeDef *cs_port;
  uint16_t cs_pin;
  GPIO_TypeDef *int_port;
  uint16_t int_pin;
  GPIO_TypeDef *reset_port;
  uint16_t reset_pin;
  BNO085_DelayMilliseconds delay_ms;
  uint8_t tx_sequence[6];
  uint8_t rx_channel;
  uint8_t rx_sequence;
  uint16_t rx_length;
  uint8_t rx_payload[320];
  BNO085_Sample sample;
  BNO085_ProductInfo product;
  uint32_t received_packets;
  uint32_t spi_errors;
  uint32_t protocol_errors;
  uint32_t continuation_packets;
  bool initialized;
} BNO085_Device;

void BNO085_InitContext(BNO085_Device *device,
                        SPI_HandleTypeDef *spi,
                        GPIO_TypeDef *cs_port,
                        uint16_t cs_pin,
                        GPIO_TypeDef *int_port,
                        uint16_t int_pin,
                        GPIO_TypeDef *reset_port,
                        uint16_t reset_pin,
                        BNO085_DelayMilliseconds delay_ms);

/*
 * Performs a hardware reset, validates the product-ID response, and enables
 * calibrated gyro, linear acceleration, and game rotation-vector reports.
 */
BNO085_Result BNO085_Begin(BNO085_Device *device,
                           uint32_t report_interval_us);

/* Reads one complete SHTP packet while H_INTN is asserted and parses it. */
BNO085_Result BNO085_ReceiveAndProcess(BNO085_Device *device,
                                       uint32_t *update_flags);

bool BNO085_DataReady(const BNO085_Device *device);

#ifdef __cplusplus
}
#endif

#endif
