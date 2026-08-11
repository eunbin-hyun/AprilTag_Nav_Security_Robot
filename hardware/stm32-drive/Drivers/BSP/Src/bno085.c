#include "bno085.h"

#include <string.h>

#define BNO085_SHTP_HEADER_LENGTH             4U
#define BNO085_SHTP_CHANNEL_EXECUTABLE        1U
#define BNO085_SHTP_CHANNEL_CONTROL           2U
#define BNO085_SHTP_CHANNEL_REPORTS           3U
#define BNO085_SHTP_CHANNEL_COUNT             6U
#define BNO085_SHTP_CONTINUATION_MASK         0x8000U
#define BNO085_SHTP_LENGTH_MASK               0x7FFFU

#define BNO085_REPORT_PRODUCT_ID_REQUEST      0xF9U
#define BNO085_REPORT_PRODUCT_ID_RESPONSE     0xF8U
#define BNO085_REPORT_SET_FEATURE_COMMAND     0xFDU
#define BNO085_REPORT_BASE_TIMESTAMP          0xFBU
#define BNO085_REPORT_GYROSCOPE_CALIBRATED    0x02U
#define BNO085_REPORT_LINEAR_ACCELERATION     0x04U
#define BNO085_REPORT_GAME_ROTATION_VECTOR    0x08U

#define BNO085_STARTUP_TIMEOUT_MS             300U
#define BNO085_TRANSFER_TIMEOUT_MS            20U
#define BNO085_COMMAND_READY_TIMEOUT_MS       150U
#define BNO085_PRODUCT_RESPONSE_TIMEOUT_MS    300U
#define BNO085_SPI_CHUNK_SIZE                 32U
#define BNO085_GYRO_Q_POINT                   9U
#define BNO085_LINEAR_ACCEL_Q_POINT           8U
#define BNO085_QUATERNION_Q_POINT             14U

static uint16_t ReadU16LittleEndian(const uint8_t *data)
{
  return (uint16_t)data[0] | ((uint16_t)data[1] << 8);
}

static uint32_t ReadU32LittleEndian(const uint8_t *data)
{
  return (uint32_t)data[0] |
         ((uint32_t)data[1] << 8) |
         ((uint32_t)data[2] << 16) |
         ((uint32_t)data[3] << 24);
}

static void WriteU32LittleEndian(uint8_t *data, uint32_t value)
{
  data[0] = (uint8_t)(value & 0xFFU);
  data[1] = (uint8_t)((value >> 8) & 0xFFU);
  data[2] = (uint8_t)((value >> 16) & 0xFFU);
  data[3] = (uint8_t)(value >> 24);
}

static float QToFloat(int16_t value, uint8_t q_point)
{
  return (float)value / (float)(1UL << q_point);
}

static void DelayMilliseconds(const BNO085_Device *device, uint32_t delay_ms)
{
  if ((device != NULL) && (device->delay_ms != NULL))
  {
    device->delay_ms(delay_ms);
  }
  else
  {
    HAL_Delay(delay_ms);
  }
}

bool BNO085_DataReady(const BNO085_Device *device)
{
  return (device != NULL) &&
         (device->int_port != NULL) &&
         (HAL_GPIO_ReadPin(device->int_port, device->int_pin) ==
          GPIO_PIN_RESET);
}

static BNO085_Result WaitForInterrupt(const BNO085_Device *device,
                                      uint32_t timeout_ms)
{
  uint32_t started_at;

  if (device == NULL)
  {
    return BNO085_RESULT_ARGUMENT_ERROR;
  }

  started_at = HAL_GetTick();
  while (!BNO085_DataReady(device))
  {
    if ((uint32_t)(HAL_GetTick() - started_at) >= timeout_ms)
    {
      return BNO085_RESULT_TIMEOUT;
    }
    DelayMilliseconds(device, 1U);
  }

  return BNO085_RESULT_OK;
}

static BNO085_Result ReceivePacket(BNO085_Device *device)
{
  uint8_t header_tx[BNO085_SHTP_HEADER_LENGTH] = {0U};
  uint8_t header_rx[BNO085_SHTP_HEADER_LENGTH] = {0U};
  uint8_t dummy_tx[BNO085_SPI_CHUNK_SIZE];
  uint8_t discard_rx[BNO085_SPI_CHUNK_SIZE];
  uint16_t raw_length;
  uint16_t payload_length;
  uint16_t offset = 0U;
  bool continuation;

  if ((device == NULL) || (device->spi == NULL))
  {
    return BNO085_RESULT_ARGUMENT_ERROR;
  }
  if (!BNO085_DataReady(device))
  {
    return BNO085_RESULT_NO_DATA;
  }

  memset(dummy_tx, 0xFF, sizeof(dummy_tx));
  HAL_GPIO_WritePin(device->cs_port, device->cs_pin, GPIO_PIN_RESET);

  if (HAL_SPI_TransmitReceive(device->spi,
                              header_tx,
                              header_rx,
                              sizeof(header_rx),
                              BNO085_TRANSFER_TIMEOUT_MS) != HAL_OK)
  {
    HAL_GPIO_WritePin(device->cs_port, device->cs_pin, GPIO_PIN_SET);
    device->spi_errors++;
    return BNO085_RESULT_SPI_ERROR;
  }

  raw_length = ReadU16LittleEndian(header_rx);
  continuation = (raw_length & BNO085_SHTP_CONTINUATION_MASK) != 0U;
  raw_length &= BNO085_SHTP_LENGTH_MASK;
  if (raw_length < BNO085_SHTP_HEADER_LENGTH)
  {
    HAL_GPIO_WritePin(device->cs_port, device->cs_pin, GPIO_PIN_SET);
    device->protocol_errors++;
    return BNO085_RESULT_PROTOCOL_ERROR;
  }

  payload_length = raw_length - BNO085_SHTP_HEADER_LENGTH;
  while (offset < payload_length)
  {
    uint16_t remaining = payload_length - offset;
    uint16_t chunk = (remaining > BNO085_SPI_CHUNK_SIZE) ?
                         BNO085_SPI_CHUNK_SIZE :
                         remaining;
    uint8_t *receive_target = discard_rx;

    if (offset < sizeof(device->rx_payload))
    {
      uint16_t storable = sizeof(device->rx_payload) - offset;
      if (chunk <= storable)
      {
        receive_target = &device->rx_payload[offset];
      }
    }

    if (HAL_SPI_TransmitReceive(device->spi,
                                dummy_tx,
                                receive_target,
                                chunk,
                                BNO085_TRANSFER_TIMEOUT_MS) != HAL_OK)
    {
      HAL_GPIO_WritePin(device->cs_port, device->cs_pin, GPIO_PIN_SET);
      device->spi_errors++;
      return BNO085_RESULT_SPI_ERROR;
    }
    offset += chunk;
  }

  HAL_GPIO_WritePin(device->cs_port, device->cs_pin, GPIO_PIN_SET);
  device->rx_channel = header_rx[2];
  device->rx_sequence = header_rx[3];
  device->rx_length = payload_length;
  device->received_packets++;

  if (continuation)
  {
    device->continuation_packets++;
    device->protocol_errors++;
    return BNO085_RESULT_PROTOCOL_ERROR;
  }
  if (payload_length > sizeof(device->rx_payload))
  {
    device->protocol_errors++;
    return BNO085_RESULT_PROTOCOL_ERROR;
  }

  return BNO085_RESULT_OK;
}

static BNO085_Result SendPacket(BNO085_Device *device,
                                uint8_t channel,
                                const uint8_t *payload,
                                uint16_t payload_length)
{
  uint8_t header[BNO085_SHTP_HEADER_LENGTH];
  uint16_t packet_length;
  BNO085_Result ready_result;

  if ((device == NULL) ||
      (device->spi == NULL) ||
      (payload == NULL) ||
      (channel >= BNO085_SHTP_CHANNEL_COUNT) ||
      (payload_length >
       (BNO085_SHTP_LENGTH_MASK - BNO085_SHTP_HEADER_LENGTH)))
  {
    return BNO085_RESULT_ARGUMENT_ERROR;
  }

  ready_result = WaitForInterrupt(device, BNO085_COMMAND_READY_TIMEOUT_MS);
  if (ready_result != BNO085_RESULT_OK)
  {
    return ready_result;
  }

  packet_length = payload_length + BNO085_SHTP_HEADER_LENGTH;
  header[0] = (uint8_t)(packet_length & 0xFFU);
  header[1] = (uint8_t)(packet_length >> 8);
  header[2] = channel;
  header[3] = device->tx_sequence[channel]++;

  HAL_GPIO_WritePin(device->cs_port, device->cs_pin, GPIO_PIN_RESET);
  if ((HAL_SPI_Transmit(device->spi,
                        header,
                        sizeof(header),
                        BNO085_TRANSFER_TIMEOUT_MS) != HAL_OK) ||
      (HAL_SPI_Transmit(device->spi,
                        (uint8_t *)payload,
                        payload_length,
                        BNO085_TRANSFER_TIMEOUT_MS) != HAL_OK))
  {
    HAL_GPIO_WritePin(device->cs_port, device->cs_pin, GPIO_PIN_SET);
    device->spi_errors++;
    return BNO085_RESULT_SPI_ERROR;
  }
  HAL_GPIO_WritePin(device->cs_port, device->cs_pin, GPIO_PIN_SET);
  return BNO085_RESULT_OK;
}

static BNO085_Result SetFeature(BNO085_Device *device,
                                uint8_t report_id,
                                uint32_t report_interval_us)
{
  uint8_t payload[17] = {0U};

  payload[0] = BNO085_REPORT_SET_FEATURE_COMMAND;
  payload[1] = report_id;
  WriteU32LittleEndian(&payload[5], report_interval_us);
  return SendPacket(device,
                    BNO085_SHTP_CHANNEL_CONTROL,
                    payload,
                    sizeof(payload));
}

static void ParseProductInfo(BNO085_Device *device)
{
  if ((device->rx_channel != BNO085_SHTP_CHANNEL_CONTROL) ||
      (device->rx_length < 14U) ||
      (device->rx_payload[0] != BNO085_REPORT_PRODUCT_ID_RESPONSE))
  {
    return;
  }

  device->product.sw_version_major = device->rx_payload[2];
  device->product.sw_version_minor = device->rx_payload[3];
  device->product.sw_part_number = ReadU32LittleEndian(&device->rx_payload[4]);
  device->product.sw_build_number = ReadU32LittleEndian(&device->rx_payload[8]);
  device->product.sw_version_patch =
      ReadU16LittleEndian(&device->rx_payload[12]);
  device->product.valid = true;
}

static uint8_t ReportLength(uint8_t report_id)
{
  switch (report_id)
  {
    case BNO085_REPORT_GYROSCOPE_CALIBRATED:
    case BNO085_REPORT_LINEAR_ACCELERATION:
      return 10U;

    case BNO085_REPORT_GAME_ROTATION_VECTOR:
      return 12U;

    default:
      return 0U;
  }
}

static BNO085_Result ParseInputReports(BNO085_Device *device,
                                       uint32_t *update_flags)
{
  uint16_t offset;
  uint32_t updates = BNO085_UPDATE_NONE;

  if ((device->rx_channel != BNO085_SHTP_CHANNEL_REPORTS) ||
      (device->rx_length < 5U) ||
      (device->rx_payload[0] != BNO085_REPORT_BASE_TIMESTAMP))
  {
    return BNO085_RESULT_OK;
  }

  device->sample.sensor_timestamp_us =
      ReadU32LittleEndian(&device->rx_payload[1]);
  offset = 5U;

  while ((offset + 4U) <= device->rx_length)
  {
    const uint8_t *report = &device->rx_payload[offset];
    uint8_t report_length = ReportLength(report[0]);
    uint8_t accuracy = report[2] & 0x03U;

    if ((report_length == 0U) ||
        ((offset + report_length) > device->rx_length))
    {
      device->protocol_errors++;
      return BNO085_RESULT_PROTOCOL_ERROR;
    }

    if (report[0] == BNO085_REPORT_GYROSCOPE_CALIBRATED)
    {
      device->sample.gyro_x_rad_s =
          QToFloat((int16_t)ReadU16LittleEndian(&report[4]),
                   BNO085_GYRO_Q_POINT);
      device->sample.gyro_y_rad_s =
          QToFloat((int16_t)ReadU16LittleEndian(&report[6]),
                   BNO085_GYRO_Q_POINT);
      device->sample.gyro_z_rad_s =
          QToFloat((int16_t)ReadU16LittleEndian(&report[8]),
                   BNO085_GYRO_Q_POINT);
      device->sample.gyro_accuracy = accuracy;
      updates |= BNO085_UPDATE_GYROSCOPE;
    }
    else if (report[0] == BNO085_REPORT_LINEAR_ACCELERATION)
    {
      device->sample.linear_accel_x_mps2 =
          QToFloat((int16_t)ReadU16LittleEndian(&report[4]),
                   BNO085_LINEAR_ACCEL_Q_POINT);
      device->sample.linear_accel_y_mps2 =
          QToFloat((int16_t)ReadU16LittleEndian(&report[6]),
                   BNO085_LINEAR_ACCEL_Q_POINT);
      device->sample.linear_accel_z_mps2 =
          QToFloat((int16_t)ReadU16LittleEndian(&report[8]),
                   BNO085_LINEAR_ACCEL_Q_POINT);
      device->sample.linear_accel_accuracy = accuracy;
      updates |= BNO085_UPDATE_LINEAR_ACCELERATION;
    }
    else
    {
      device->sample.quaternion_i =
          QToFloat((int16_t)ReadU16LittleEndian(&report[4]),
                   BNO085_QUATERNION_Q_POINT);
      device->sample.quaternion_j =
          QToFloat((int16_t)ReadU16LittleEndian(&report[6]),
                   BNO085_QUATERNION_Q_POINT);
      device->sample.quaternion_k =
          QToFloat((int16_t)ReadU16LittleEndian(&report[8]),
                   BNO085_QUATERNION_Q_POINT);
      device->sample.quaternion_real =
          QToFloat((int16_t)ReadU16LittleEndian(&report[10]),
                   BNO085_QUATERNION_Q_POINT);
      device->sample.quaternion_accuracy = accuracy;
      updates |= BNO085_UPDATE_GAME_ROTATION_VECTOR;
    }

    offset += report_length;
  }

  if (update_flags != NULL)
  {
    *update_flags = updates;
  }
  return BNO085_RESULT_OK;
}

void BNO085_InitContext(BNO085_Device *device,
                        SPI_HandleTypeDef *spi,
                        GPIO_TypeDef *cs_port,
                        uint16_t cs_pin,
                        GPIO_TypeDef *int_port,
                        uint16_t int_pin,
                        GPIO_TypeDef *reset_port,
                        uint16_t reset_pin,
                        BNO085_DelayMilliseconds delay_ms)
{
  if (device == NULL)
  {
    return;
  }

  memset(device, 0, sizeof(*device));
  device->spi = spi;
  device->cs_port = cs_port;
  device->cs_pin = cs_pin;
  device->int_port = int_port;
  device->int_pin = int_pin;
  device->reset_port = reset_port;
  device->reset_pin = reset_pin;
  device->delay_ms = delay_ms;
}

BNO085_Result BNO085_Begin(BNO085_Device *device,
                           uint32_t report_interval_us)
{
  uint8_t product_request[2] = {
      BNO085_REPORT_PRODUCT_ID_REQUEST,
      0U,
  };
  uint32_t started_at;
  BNO085_Result result;

  if ((device == NULL) ||
      (device->spi == NULL) ||
      (device->cs_port == NULL) ||
      (device->int_port == NULL) ||
      (device->reset_port == NULL) ||
      (report_interval_us == 0U))
  {
    return BNO085_RESULT_ARGUMENT_ERROR;
  }

  memset(device->tx_sequence, 0, sizeof(device->tx_sequence));
  memset(&device->sample, 0, sizeof(device->sample));
  memset(&device->product, 0, sizeof(device->product));
  device->initialized = false;
  HAL_GPIO_WritePin(device->cs_port, device->cs_pin, GPIO_PIN_SET);

  HAL_GPIO_WritePin(device->reset_port, device->reset_pin, GPIO_PIN_RESET);
  DelayMilliseconds(device, 2U);
  HAL_GPIO_WritePin(device->reset_port, device->reset_pin, GPIO_PIN_SET);

  /* Startup advertisement followed by the unsolicited initialize response. */
  result = WaitForInterrupt(device, BNO085_STARTUP_TIMEOUT_MS);
  if (result != BNO085_RESULT_OK)
  {
    return result;
  }
  result = ReceivePacket(device);
  if (result != BNO085_RESULT_OK)
  {
    return result;
  }

  result = WaitForInterrupt(device, BNO085_STARTUP_TIMEOUT_MS);
  if (result != BNO085_RESULT_OK)
  {
    return result;
  }
  result = ReceivePacket(device);
  if (result != BNO085_RESULT_OK)
  {
    return result;
  }

  result = SendPacket(device,
                      BNO085_SHTP_CHANNEL_CONTROL,
                      product_request,
                      sizeof(product_request));
  if (result != BNO085_RESULT_OK)
  {
    return result;
  }

  started_at = HAL_GetTick();
  while (!device->product.valid)
  {
    uint32_t elapsed = HAL_GetTick() - started_at;

    if (elapsed >= BNO085_PRODUCT_RESPONSE_TIMEOUT_MS)
    {
      return BNO085_RESULT_TIMEOUT;
    }
    result = WaitForInterrupt(device,
                              BNO085_PRODUCT_RESPONSE_TIMEOUT_MS - elapsed);
    if (result != BNO085_RESULT_OK)
    {
      return result;
    }
    result = ReceivePacket(device);
    if (result != BNO085_RESULT_OK)
    {
      return result;
    }
    ParseProductInfo(device);
  }

  result = SetFeature(device,
                      BNO085_REPORT_GYROSCOPE_CALIBRATED,
                      report_interval_us);
  if (result != BNO085_RESULT_OK)
  {
    return result;
  }
  result = SetFeature(device,
                      BNO085_REPORT_LINEAR_ACCELERATION,
                      report_interval_us);
  if (result != BNO085_RESULT_OK)
  {
    return result;
  }
  result = SetFeature(device,
                      BNO085_REPORT_GAME_ROTATION_VECTOR,
                      report_interval_us);
  if (result != BNO085_RESULT_OK)
  {
    return result;
  }

  device->initialized = true;
  return BNO085_RESULT_OK;
}

BNO085_Result BNO085_ReceiveAndProcess(BNO085_Device *device,
                                       uint32_t *update_flags)
{
  BNO085_Result result;

  if (update_flags != NULL)
  {
    *update_flags = BNO085_UPDATE_NONE;
  }

  result = ReceivePacket(device);
  if (result != BNO085_RESULT_OK)
  {
    return result;
  }

  if (device->rx_channel == BNO085_SHTP_CHANNEL_REPORTS)
  {
    return ParseInputReports(device, update_flags);
  }

  if ((device->rx_channel == BNO085_SHTP_CHANNEL_CONTROL) &&
      (device->rx_length > 0U) &&
      (device->rx_payload[0] == BNO085_REPORT_PRODUCT_ID_RESPONSE))
  {
    ParseProductInfo(device);
  }
  else if ((device->rx_channel == BNO085_SHTP_CHANNEL_EXECUTABLE) &&
           (device->rx_length > 0U))
  {
    /* Executable-channel packets are expected during reset and recovery. */
  }

  return BNO085_RESULT_OK;
}
