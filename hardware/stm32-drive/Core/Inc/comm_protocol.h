#ifndef COMM_PROTOCOL_H
#define COMM_PROTOCOL_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define COMM_PROTOCOL_SOF1              0xAAU
#define COMM_PROTOCOL_SOF2              0x55U
#define COMM_PROTOCOL_VERSION           0x02U
#define COMM_PROTOCOL_MAX_PAYLOAD       64U
#define COMM_PROTOCOL_FRAME_OVERHEAD    8U
#define COMM_PROTOCOL_MAX_FRAME_SIZE    \
  (COMM_PROTOCOL_FRAME_OVERHEAD + COMM_PROTOCOL_MAX_PAYLOAD)
#define COMM_PROTOCOL_PARSER_BUFFER_SIZE 256U
#define COMM_PROTOCOL_INCOMPLETE_TIMEOUT_MS 100U

#define COMM_COMMAND_TIMEOUT_MS         300U
#define COMM_SEQUENCE_SESSION_IDLE_MS   350U
#define COMM_DIAG_ECHO_MAX_PAYLOAD      31U
#define COMM_DIAG_MOTOR_MIN_ABS_PERMILLE 200
#define COMM_DIAG_MOTOR_MAX_ABS_PERMILLE 950
#define COMM_DIAG_MOTOR_MIN_DURATION_MS 100U
#define COMM_DIAG_MOTOR_MAX_DURATION_MS 10000U
#define COMM_DIAG_SERVO_MIN_PULSE_US    766U
#define COMM_DIAG_SERVO_MAX_PULSE_US    1696U

typedef enum
{
  COMM_MSG_CMD_DRIVE = 0x10,
  COMM_MSG_CMD_STOP = 0x11,
  COMM_MSG_CMD_RESET_FAULT = 0x12,

  COMM_MSG_TELEMETRY_DRIVE = 0x80,
  COMM_MSG_FAULT_EVENT = 0x81,
  COMM_MSG_COMMAND_RESULT = 0x82,
  COMM_MSG_TELEMETRY_IMU = 0x83,
  COMM_MSG_TELEMETRY_RANGE = 0x84,
  COMM_MSG_TELEMETRY_ODOMETRY = 0x85,

  COMM_MSG_DIAG_ECHO_REQUEST = 0xF0,
  COMM_MSG_DIAG_ECHO_RESPONSE = 0xF1,
  COMM_MSG_DIAG_MOTOR_TEST_REQUEST = 0xF2,
  COMM_MSG_DIAG_MOTOR_TEST_RESPONSE = 0xF3,
  COMM_MSG_DIAG_SERVO_REQUEST = 0xF6,
  COMM_MSG_DIAG_SERVO_RESPONSE = 0xF7
} CommMessageId;

typedef enum
{
  COMM_STOP_REASON_OPERATOR = 0,
  COMM_STOP_REASON_MISSION_COMPLETE = 1,
  COMM_STOP_REASON_OBSTACLE = 2,
  COMM_STOP_REASON_REMOTE_REQUEST = 3,
  COMM_STOP_REASON_INTERNAL = 4,
  COMM_STOP_REASON_ROS_COMMAND_TIMEOUT = 5,
  COMM_STOP_REASON_LINK_SHUTDOWN = 6
} CommStopReason;

typedef enum
{
  COMM_STATE_BOOT = 0,
  COMM_STATE_SELF_TEST = 1,
  COMM_STATE_READY = 2,
  COMM_STATE_DRIVING = 3,
  COMM_STATE_SAFE_STOP = 4,
  COMM_STATE_FAULT = 5,
  COMM_STATE_ESTOP = 6
} CommSystemState;

typedef enum
{
  COMM_FAULT_COMM_TIMEOUT = (1UL << 0),
  COMM_FAULT_CRC_ERROR = (1UL << 1),
  COMM_FAULT_BAD_COMMAND = (1UL << 2),
  COMM_FAULT_ENCODER_INVALID = (1UL << 3),
  COMM_FAULT_MOTOR_STALL = (1UL << 4),
  COMM_FAULT_DIRECTION = (1UL << 5),
  COMM_FAULT_CONTROL_OVERRUN = (1UL << 6),
  COMM_FAULT_IMU_LOST = (1UL << 7),
  COMM_FAULT_RANGE_LOST = (1UL << 8),
  COMM_FAULT_STEERING_INVALID = (1UL << 9),
  COMM_FAULT_ESTOP_ACTIVE = (1UL << 10),
  COMM_FAULT_INTERNAL = (1UL << 11),
  COMM_FAULT_COMMAND_LIMIT = (1UL << 12),
  COMM_FAULT_UART_RX_OVERFLOW = (1UL << 13),
  COMM_FAULT_UART_TX_ERROR = (1UL << 14),
  COMM_FAULT_SENSOR_STALE = (1UL << 15),
  COMM_FAULT_OBSTACLE_NEAR = (1UL << 16)
} CommFaultBit;

typedef enum
{
  COMM_FAULT_ACTION_REPORT_ONLY = 0,
  COMM_FAULT_ACTION_HOLD_NEUTRAL = 1,
  COMM_FAULT_ACTION_SAFE_STOP = 2,
  COMM_FAULT_ACTION_OUTPUT_DISABLE = 3,
  COMM_FAULT_ACTION_LATCHED_STOP = 4
} CommFaultAction;

typedef enum
{
  COMM_ODOMETRY_STATUS_NONE = 0U,
  COMM_ODOMETRY_STATUS_VALID = (1U << 0),
  COMM_ODOMETRY_STATUS_ENCODER_CALIBRATED = (1U << 1),
  COMM_ODOMETRY_STATUS_GEOMETRY_CALIBRATED = (1U << 2),
  COMM_ODOMETRY_STATUS_STEERING_ESTIMATED = (1U << 3),
  COMM_ODOMETRY_STATUS_IMU_FUSED = (1U << 4),
  COMM_ODOMETRY_STATUS_INPUT_INVALID = (1U << 5),
  COMM_ODOMETRY_STATUS_IMU_HEADING_FALLBACK = (1U << 6)
} CommOdometryStatus;

typedef enum
{
  COMM_IMU_STATUS_NONE = 0U,
  COMM_IMU_STATUS_CONNECTED = (1U << 0),
  COMM_IMU_STATUS_GYRO_VALID = (1U << 1),
  COMM_IMU_STATUS_LINEAR_ACCEL_VALID = (1U << 2),
  COMM_IMU_STATUS_QUATERNION_VALID = (1U << 3),
  COMM_IMU_STATUS_STALE = (1U << 4),
  COMM_IMU_STATUS_SPI_ERROR = (1U << 5),
  COMM_IMU_STATUS_PROTOCOL_ERROR = (1U << 6)
} CommImuStatus;

typedef enum
{
  COMM_ODOMETRY_STEERING_NONE = 0,
  COMM_ODOMETRY_STEERING_SENSOR = 1,
  COMM_ODOMETRY_STEERING_COMMAND_ESTIMATE = 2
} CommOdometrySteeringSource;

typedef enum
{
  COMM_RESULT_ACCEPTED = 0,
  COMM_RESULT_INVALID_STATE = 1,
  COMM_RESULT_OUT_OF_RANGE = 2,
  COMM_RESULT_FAULT_ACTIVE = 3,
  COMM_RESULT_UNSUPPORTED = 4,
  COMM_RESULT_INVALID_VALUE = 5,
  COMM_RESULT_DUPLICATE = 6,
  COMM_RESULT_NOT_ARMED = 7
} CommCommandResultCode;

typedef struct
{
  int16_t target_speed_mm_s;
  int16_t target_steering_cdeg;
  bool drive_enable;
} CommDriveCommand;

typedef struct
{
  CommStopReason reason;
} CommStopCommand;

typedef struct
{
  uint32_t acknowledged_fault_bits;
  uint8_t request_sequence;
} CommResetFaultCommand;

typedef struct
{
  uint16_t pulse_us;
  uint8_t request_sequence;
} CommServoDiagnosticCommand;

typedef struct
{
  int16_t duty_permille;
  uint16_t duration_ms;
  uint8_t request_sequence;
} CommMotorDiagnosticCommand;

typedef struct
{
  uint32_t mcu_time_ms;
  int16_t target_speed_mm_s;
  int16_t measured_speed_mm_s;
  int16_t motor_duty_permille;
  int16_t steering_cmd_cdeg;
  int16_t steering_feedback_cdeg;
  int32_t encoder_count;
  int16_t yaw_cdeg;
  CommSystemState state;
  uint8_t last_drive_seq;
  uint32_t active_fault_bits;
} CommDriveTelemetry;

typedef struct
{
  uint32_t occurred_at_ms;
  uint32_t active_fault_bits;
  uint32_t latched_fault_bits;
  CommFaultAction action;
  CommSystemState state;
} CommFaultEvent;

typedef struct
{
  uint32_t mcu_time_ms;
  uint16_t front_left_mm;
  uint16_t front_right_mm;
  uint16_t rear_left_mm;
  uint16_t rear_right_mm;
  uint8_t valid_mask;
} CommRangeTelemetry;

typedef struct
{
  uint32_t mcu_time_ms;
  int16_t quaternion_i_q14;
  int16_t quaternion_j_q14;
  int16_t quaternion_k_q14;
  int16_t quaternion_real_q14;
  int32_t gyro_x_mdeg_s;
  int32_t gyro_y_mdeg_s;
  int32_t gyro_z_mdeg_s;
  int16_t linear_accel_x_mm_s2;
  int16_t linear_accel_y_mm_s2;
  int16_t linear_accel_z_mm_s2;
  int32_t yaw_mdeg;
  uint8_t gyro_accuracy;
  uint8_t linear_accel_accuracy;
  uint8_t quaternion_accuracy;
  uint8_t status_flags;
} CommImuTelemetry;

typedef struct
{
  uint32_t mcu_time_ms;
  int32_t x_mm;
  int32_t y_mm;
  int32_t yaw_mdeg;
  int32_t distance_mm;
  int16_t linear_speed_mm_s;
  int32_t yaw_rate_mdeg_s;
  int16_t steering_cdeg;
  int32_t curvature_micro_per_m;
  uint16_t status_flags;
  CommOdometrySteeringSource steering_source;
  uint8_t last_drive_seq;
} CommOdometryTelemetry;

typedef struct
{
  uint8_t version;
  uint8_t message_id;
  uint8_t sequence;
  uint8_t payload_length;
  uint8_t payload[COMM_PROTOCOL_MAX_PAYLOAD];
} CommFrame;

typedef enum
{
  COMM_PARSE_NONE = 0,
  COMM_PARSE_FRAME_READY,
  COMM_PARSE_CRC_ERROR,
  COMM_PARSE_LENGTH_ERROR,
  COMM_PARSE_VERSION_ERROR
} CommParseResult;

typedef struct
{
  uint32_t valid_frames;
  uint32_t crc_errors;
  uint32_t length_errors;
  uint32_t version_errors;
  uint32_t discarded_bytes;
  uint32_t buffer_overflows;
  uint32_t incomplete_timeouts;
} CommParserStats;

typedef struct
{
  uint8_t buffer[COMM_PROTOCOL_PARSER_BUFFER_SIZE];
  uint16_t head;
  uint16_t count;
  uint32_t incomplete_started_ms;
  bool incomplete_timer_active;
  CommParserStats stats;
} CommParser;

void CommProtocol_ParserInit(CommParser *parser);
CommParseResult CommProtocol_ParserPush(CommParser *parser,
                                        uint8_t byte,
                                        CommFrame *completed_frame);
CommParseResult CommProtocol_ParserPushTimed(CommParser *parser,
                                             uint8_t byte,
                                             uint32_t now_ms,
                                             CommFrame *completed_frame);
CommParseResult CommProtocol_ParserPoll(CommParser *parser,
                                        uint32_t now_ms,
                                        CommFrame *completed_frame);

uint16_t CommProtocol_Crc16CcittFalse(const uint8_t *data, size_t length);
size_t CommProtocol_EncodeFrame(uint8_t message_id,
                                uint8_t sequence,
                                const uint8_t *payload,
                                uint8_t payload_length,
                                uint8_t *output,
                                size_t output_capacity);

#ifdef __cplusplus
}
#endif

#endif
