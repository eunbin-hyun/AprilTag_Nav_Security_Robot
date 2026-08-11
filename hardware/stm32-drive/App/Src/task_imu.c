#include "task_imu.h"

#include "bno085.h"
#include "cmsis_os2.h"
#include "main.h"
#include "vehicle_config.h"

#include <math.h>
#include <string.h>

#define IMU_DATA_READY_THREAD_FLAG (1UL << 0)
#define IMU_WAIT_TIMEOUT_MS         20U
#define IMU_RETRY_DELAY_MS          1000U
#define IMU_MAX_PACKETS_PER_WAKE    16U

static osMutexId_t imu_mutex;
static osThreadId_t imu_thread_id;
static BNO085_Device bno085;
static ImuState imu_state;

static void ImuDelayMilliseconds(uint32_t delay_ms)
{
  (void)osDelay(delay_ms);
}

static void CopyDriverDiagnosticsLocked(void)
{
  imu_state.received_packets = bno085.received_packets;
  imu_state.spi_errors = bno085.spi_errors;
  imu_state.protocol_errors = bno085.protocol_errors;
  imu_state.continuation_packets = bno085.continuation_packets;

  if (bno085.product.valid)
  {
    imu_state.sw_version_major = bno085.product.sw_version_major;
    imu_state.sw_version_minor = bno085.product.sw_version_minor;
    imu_state.sw_version_patch = bno085.product.sw_version_patch;
    imu_state.sw_part_number = bno085.product.sw_part_number;
    imu_state.sw_build_number = bno085.product.sw_build_number;
  }
}

static void MarkConnectionResult(BNO085_Result result, bool connected)
{
  if (osMutexAcquire(imu_mutex, osWaitForever) != osOK)
  {
    return;
  }

  imu_state.status_flags &=
      ~(IMU_STATUS_CONNECTED |
        IMU_STATUS_GYRO_VALID |
        IMU_STATUS_LINEAR_ACCEL_VALID |
        IMU_STATUS_QUATERNION_VALID |
        IMU_STATUS_STALE |
        IMU_STATUS_SPI_ERROR |
        IMU_STATUS_PROTOCOL_ERROR);

  if (connected)
  {
    imu_state.status_flags |= IMU_STATUS_CONNECTED | IMU_STATUS_STALE;
    imu_state.last_packet_at_ms = HAL_GetTick();
    imu_state.reset_count++;
  }
  else if (result == BNO085_RESULT_SPI_ERROR)
  {
    imu_state.status_flags |= IMU_STATUS_SPI_ERROR | IMU_STATUS_STALE;
  }
  else if (result == BNO085_RESULT_PROTOCOL_ERROR)
  {
    imu_state.status_flags |= IMU_STATUS_PROTOCOL_ERROR | IMU_STATUS_STALE;
  }
  else
  {
    imu_state.status_flags |= IMU_STATUS_STALE;
  }

  CopyDriverDiagnosticsLocked();
  (void)osMutexRelease(imu_mutex);
}

static bool QuaternionToYaw(const BNO085_Sample *sample, float *yaw_rad)
{
  float qi = sample->quaternion_i;
  float qj = sample->quaternion_j;
  float qk = sample->quaternion_k;
  float qr = sample->quaternion_real;
  float norm = sqrtf((qi * qi) + (qj * qj) + (qk * qk) + (qr * qr));

  if (!isfinite(norm) || (norm < 0.1f))
  {
    return false;
  }

  qi /= norm;
  qj /= norm;
  qk /= norm;
  qr /= norm;
  *yaw_rad = VEHICLE_IMU_YAW_SIGN *
             atan2f(2.0f * ((qr * qk) + (qi * qj)),
                    1.0f - (2.0f * ((qj * qj) + (qk * qk))));
  return isfinite(*yaw_rad);
}

static void PublishUpdates(uint32_t updates)
{
  uint32_t now = HAL_GetTick();
  float yaw_rad = 0.0f;
  bool yaw_valid = false;

  if ((updates & BNO085_UPDATE_GAME_ROTATION_VECTOR) != 0U)
  {
    yaw_valid = QuaternionToYaw(&bno085.sample, &yaw_rad);
  }

  if (osMutexAcquire(imu_mutex, osWaitForever) != osOK)
  {
    return;
  }

  imu_state.status_flags |= IMU_STATUS_CONNECTED;
  imu_state.status_flags &=
      ~(IMU_STATUS_STALE | IMU_STATUS_SPI_ERROR | IMU_STATUS_PROTOCOL_ERROR);
  imu_state.last_packet_at_ms = now;
  imu_state.sensor_timestamp_us = bno085.sample.sensor_timestamp_us;

  if ((updates & BNO085_UPDATE_GYROSCOPE) != 0U)
  {
    imu_state.gyro_x_rad_s = bno085.sample.gyro_x_rad_s;
    imu_state.gyro_y_rad_s = bno085.sample.gyro_y_rad_s;
    imu_state.gyro_z_rad_s =
        VEHICLE_IMU_YAW_SIGN * bno085.sample.gyro_z_rad_s;
    imu_state.gyro_accuracy = bno085.sample.gyro_accuracy;
    imu_state.gyro_updated_at_ms = now;
    imu_state.status_flags |= IMU_STATUS_GYRO_VALID;
  }

  if ((updates & BNO085_UPDATE_LINEAR_ACCELERATION) != 0U)
  {
    imu_state.linear_accel_x_mps2 = bno085.sample.linear_accel_x_mps2;
    imu_state.linear_accel_y_mps2 = bno085.sample.linear_accel_y_mps2;
    imu_state.linear_accel_z_mps2 = bno085.sample.linear_accel_z_mps2;
    imu_state.linear_accel_accuracy = bno085.sample.linear_accel_accuracy;
    imu_state.linear_accel_updated_at_ms = now;
    imu_state.status_flags |= IMU_STATUS_LINEAR_ACCEL_VALID;
  }

  if (((updates & BNO085_UPDATE_GAME_ROTATION_VECTOR) != 0U) && yaw_valid)
  {
    imu_state.quaternion_i = bno085.sample.quaternion_i;
    imu_state.quaternion_j = bno085.sample.quaternion_j;
    imu_state.quaternion_k = bno085.sample.quaternion_k;
    imu_state.quaternion_real = bno085.sample.quaternion_real;
    imu_state.quaternion_accuracy = bno085.sample.quaternion_accuracy;
    imu_state.quaternion_updated_at_ms = now;
    imu_state.yaw_rad = yaw_rad;
    imu_state.status_flags |= IMU_STATUS_QUATERNION_VALID;
  }

  CopyDriverDiagnosticsLocked();
  (void)osMutexRelease(imu_mutex);
}

bool ImuTask_Init(SPI_HandleTypeDef *spi)
{
  const osMutexAttr_t mutex_attributes = {
    .name = "ImuState",
  };

  if (spi == NULL)
  {
    return false;
  }

  memset(&imu_state, 0, sizeof(imu_state));
  imu_state.status_flags = IMU_STATUS_STALE;
  BNO085_InitContext(&bno085,
                     spi,
                     BNO085_CS_GPIO_Port,
                     BNO085_CS_Pin,
                     BNO085_INT_GPIO_Port,
                     BNO085_INT_Pin,
                     BNO085_RST_GPIO_Port,
                     BNO085_RST_Pin,
                     ImuDelayMilliseconds);

  imu_mutex = osMutexNew(&mutex_attributes);
  return imu_mutex != NULL;
}

bool ImuTask_IsFusionReady(const ImuState *state, uint32_t now_ms)
{
  if (state == NULL)
  {
    return false;
  }

  return ((state->status_flags &
           (IMU_STATUS_CONNECTED | IMU_STATUS_GYRO_VALID)) ==
         (IMU_STATUS_CONNECTED | IMU_STATUS_GYRO_VALID)) &&
         ((uint32_t)(now_ms - state->gyro_updated_at_ms) <=
          VEHICLE_IMU_STALE_TIMEOUT_MS) &&
#if VEHICLE_IMU_ENFORCE_ACCURACY_GATE
         (state->gyro_accuracy >= VEHICLE_IMU_MIN_FUSION_ACCURACY) &&
#endif
         isfinite(state->gyro_z_rad_s) &&
         (fabsf(state->gyro_z_rad_s) <= VEHICLE_IMU_MAX_YAW_RATE_RAD_S);
}

bool ImuTask_IsHeadingReady(const ImuState *state, uint32_t now_ms)
{
  if (state == NULL)
  {
    return false;
  }

  return ((state->status_flags &
           (IMU_STATUS_CONNECTED | IMU_STATUS_QUATERNION_VALID)) ==
          (IMU_STATUS_CONNECTED | IMU_STATUS_QUATERNION_VALID)) &&
         ((uint32_t)(now_ms - state->quaternion_updated_at_ms) <=
          VEHICLE_IMU_STALE_TIMEOUT_MS) &&
#if VEHICLE_IMU_ENFORCE_ACCURACY_GATE
         (state->quaternion_accuracy >= VEHICLE_IMU_MIN_FUSION_ACCURACY) &&
#endif
         isfinite(state->yaw_rad);
}

void ImuTask_GetState(ImuState *state)
{
  uint32_t now;

  if ((state == NULL) || (imu_mutex == NULL))
  {
    return;
  }

  if (osMutexAcquire(imu_mutex, osWaitForever) != osOK)
  {
    return;
  }
  *state = imu_state;
  (void)osMutexRelease(imu_mutex);

  now = HAL_GetTick();
  if (((state->status_flags &
        (IMU_STATUS_CONNECTED | IMU_STATUS_GYRO_VALID)) !=
       (IMU_STATUS_CONNECTED | IMU_STATUS_GYRO_VALID)) ||
      ((uint32_t)(now - state->gyro_updated_at_ms) >
       VEHICLE_IMU_STALE_TIMEOUT_MS))
  {
    state->status_flags |= IMU_STATUS_STALE;
  }
  else
  {
    state->status_flags &= ~IMU_STATUS_STALE;
  }
}

void ImuTask_OnDataReadyInterrupt(void)
{
  if (imu_thread_id != NULL)
  {
    (void)osThreadFlagsSet(imu_thread_id, IMU_DATA_READY_THREAD_FLAG);
  }
}

void Imu_Task(void *argument)
{
  bool connected = false;

  (void)argument;
  imu_thread_id = osThreadGetId();

  for (;;)
  {
    if (!connected)
    {
      BNO085_Result begin_result =
          BNO085_Begin(&bno085, VEHICLE_IMU_REPORT_INTERVAL_US);

      connected = begin_result == BNO085_RESULT_OK;
      MarkConnectionResult(begin_result, connected);
      if (!connected)
      {
        (void)osDelay(IMU_RETRY_DELAY_MS);
        continue;
      }
    }

    (void)osThreadFlagsWait(IMU_DATA_READY_THREAD_FLAG,
                            osFlagsWaitAny,
                            IMU_WAIT_TIMEOUT_MS);

    for (uint32_t packet = 0U;
         (packet < IMU_MAX_PACKETS_PER_WAKE) && BNO085_DataReady(&bno085);
         packet++)
    {
      uint32_t updates = BNO085_UPDATE_NONE;
      BNO085_Result result = BNO085_ReceiveAndProcess(&bno085, &updates);

      if (result != BNO085_RESULT_OK)
      {
        connected = false;
        MarkConnectionResult(result, false);
        break;
      }
      PublishUpdates(updates);
    }

    if (connected)
    {
      ImuState snapshot = {0};

      ImuTask_GetState(&snapshot);
      if ((uint32_t)(HAL_GetTick() - snapshot.last_packet_at_ms) >
          VEHICLE_IMU_DISCONNECT_TIMEOUT_MS)
      {
        connected = false;
        MarkConnectionResult(BNO085_RESULT_TIMEOUT, false);
      }
    }
  }
}
