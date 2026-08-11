#include "comm_service.h"

#include <limits.h>
#include <string.h>

#define CMD_DRIVE_PAYLOAD_LENGTH       5U
#define CMD_STOP_PAYLOAD_LENGTH        1U
#define CMD_RESET_PAYLOAD_LENGTH       4U
#define DRIVE_TELEMETRY_PAYLOAD_LENGTH 26U
#define FAULT_EVENT_PAYLOAD_LENGTH     14U
#define COMMAND_RESULT_PAYLOAD_LENGTH  4U
#define RANGE_TELEMETRY_PAYLOAD_LENGTH 13U
#define IMU_TELEMETRY_PAYLOAD_LENGTH   38U
#define ODOMETRY_TELEMETRY_PAYLOAD_LENGTH 36U
#define MOTOR_DIAGNOSTIC_REQUEST_PAYLOAD_LENGTH 4U
#define MOTOR_DIAGNOSTIC_RESPONSE_PAYLOAD_LENGTH 6U
#define SERVO_DIAGNOSTIC_REQUEST_PAYLOAD_LENGTH 2U
#define SERVO_DIAGNOSTIC_RESPONSE_PAYLOAD_LENGTH 7U

typedef struct
{
  UartTransport transport;
  CommParser parser;

  bool initialized;
  uint8_t tx_sequence;
  bool rx_sequence_valid;
  uint8_t last_rx_sequence;
  bool valid_frame_seen;
  uint32_t last_valid_frame_tick;
  uint8_t last_drive_sequence;
  CommSystemState current_state;
  uint32_t protocol_fault_bits;
  uint32_t observed_rx_overflows;
  uint32_t observed_uart_errors;
  uint32_t observed_tx_failures;
  uint32_t observed_parser_overflows;
  uint32_t observed_response_queue_failures;

  bool drive_limits_configured;
  int16_t max_abs_speed_mm_s;
  int16_t min_steering_cdeg;
  int16_t max_steering_cdeg;

  CommDriveCommand drive_command;
  CommStopCommand stop_command;
  CommResetFaultCommand reset_fault_command;
  CommMotorDiagnosticCommand motor_diagnostic_command;
  CommServoDiagnosticCommand servo_diagnostic_command;
  bool drive_command_pending;
  bool stop_command_pending;
  bool reset_fault_command_pending;
  bool motor_diagnostic_command_pending;
  bool servo_diagnostic_command_pending;

  bool command_timed_out;
  bool rearm_required;
  uint32_t last_valid_drive_tick;

  CommServiceStats stats;
} CommServiceContext;

static CommServiceContext service;

static uint16_t ReadU16LittleEndian(const uint8_t *data)
{
  return (uint16_t)data[0] | ((uint16_t)data[1] << 8);
}

static int16_t ReadI16LittleEndian(const uint8_t *data)
{
  return (int16_t)ReadU16LittleEndian(data);
}

static uint32_t ReadU32LittleEndian(const uint8_t *data)
{
  return (uint32_t)data[0] |
         ((uint32_t)data[1] << 8) |
         ((uint32_t)data[2] << 16) |
         ((uint32_t)data[3] << 24);
}

static void WriteU16LittleEndian(uint8_t *data, uint16_t value)
{
  data[0] = (uint8_t)(value & 0xFFU);
  data[1] = (uint8_t)(value >> 8);
}

static void WriteU32LittleEndian(uint8_t *data, uint32_t value)
{
  data[0] = (uint8_t)(value & 0xFFU);
  data[1] = (uint8_t)((value >> 8) & 0xFFU);
  data[2] = (uint8_t)((value >> 16) & 0xFFU);
  data[3] = (uint8_t)(value >> 24);
}

static bool ValueWithinAbsoluteLimit(int16_t value, int16_t limit)
{
  int32_t wide_value = value;
  int32_t wide_limit = limit;

  if (wide_value < 0)
  {
    wide_value = -wide_value;
  }

  return wide_value <= wide_limit;
}

static bool ValueWithinRange(int16_t value,
                             int16_t minimum,
                             int16_t maximum)
{
  return (value >= minimum) && (value <= maximum);
}

static void SetNeutralDriveCommand(void)
{
  service.drive_command.target_speed_mm_s = 0;
  service.drive_command.target_steering_cdeg = 0;
  service.drive_command.drive_enable = false;
  service.drive_command_pending = true;
}

static bool SequenceIsNew(uint8_t sequence)
{
  uint8_t difference;

  if (!service.rx_sequence_valid)
  {
    service.last_rx_sequence = sequence;
    service.rx_sequence_valid = true;
    return true;
  }

  difference = (uint8_t)(sequence - service.last_rx_sequence);
  if ((difference == 0U) || (difference >= 128U))
  {
    service.stats.duplicate_or_old_sequences++;
    return false;
  }

  service.last_rx_sequence = sequence;
  return true;
}

static void NoteValidFrame(uint32_t now)
{
  if (service.valid_frame_seen &&
      ((uint32_t)(now - service.last_valid_frame_tick) >=
       COMM_SEQUENCE_SESSION_IDLE_MS))
  {
    service.rx_sequence_valid = false;
    service.stats.sequence_session_resets++;
  }

  service.valid_frame_seen = true;
  service.last_valid_frame_tick = now;
}

static bool QueueFrameWithSequence(uint8_t message_id,
                                   uint8_t sequence,
                                   const uint8_t *payload,
                                   uint8_t payload_length)
{
  uint8_t frame[COMM_PROTOCOL_MAX_FRAME_SIZE];
  size_t frame_length;

  frame_length = CommProtocol_EncodeFrame(message_id,
                                          sequence,
                                          payload,
                                          payload_length,
                                          frame,
                                          sizeof(frame));
  if ((frame_length == 0U) ||
      !UartTransport_Write(&service.transport, frame, frame_length))
  {
    service.stats.response_queue_failures++;
    return false;
  }

  return true;
}

static bool QueueFrame(uint8_t message_id,
                       const uint8_t *payload,
                       uint8_t payload_length)
{
  uint8_t sequence = service.tx_sequence;

  if (!QueueFrameWithSequence(message_id,
                              sequence,
                              payload,
                              payload_length))
  {
    return false;
  }

  service.tx_sequence++;
  return true;
}

static bool QueueCommandResult(uint8_t request_message_id,
                               uint8_t request_sequence,
                               CommCommandResultCode result,
                               CommSystemState state)
{
  uint8_t payload[COMMAND_RESULT_PAYLOAD_LENGTH];

  payload[0] = request_message_id;
  payload[1] = request_sequence;
  payload[2] = (uint8_t)result;
  payload[3] = (uint8_t)state;

  if (!QueueFrame(COMM_MSG_COMMAND_RESULT, payload, sizeof(payload)))
  {
    return false;
  }

  service.stats.command_results++;
  return true;
}

static bool QueueMotorDiagnosticResponse(
    uint8_t request_sequence,
    CommCommandResultCode result,
    int16_t duty_permille,
    uint16_t duration_ms)
{
  uint8_t payload[MOTOR_DIAGNOSTIC_RESPONSE_PAYLOAD_LENGTH];

  payload[0] = request_sequence;
  payload[1] = (uint8_t)result;
  WriteU16LittleEndian(&payload[2], (uint16_t)duty_permille);
  WriteU16LittleEndian(&payload[4], duration_ms);

  return QueueFrame(COMM_MSG_DIAG_MOTOR_TEST_RESPONSE,
                    payload,
                    sizeof(payload));
}

static void HandleDriveCommand(const CommFrame *frame)
{
  CommDriveCommand command;
  bool neutral_command;

  if (frame->payload_length != CMD_DRIVE_PAYLOAD_LENGTH)
  {
    service.stats.invalid_payloads++;
    return;
  }

  command.target_speed_mm_s = ReadI16LittleEndian(&frame->payload[0]);
  command.target_steering_cdeg = ReadI16LittleEndian(&frame->payload[2]);

  if (frame->payload[4] > 1U)
  {
    service.stats.invalid_payloads++;
    service.protocol_fault_bits |= COMM_FAULT_BAD_COMMAND;
    return;
  }
  command.drive_enable = frame->payload[4] != 0U;

  if (!SequenceIsNew(frame->sequence))
  {
    return;
  }

  neutral_command = !command.drive_enable &&
                    (command.target_speed_mm_s == 0) &&
                    (command.target_steering_cdeg == 0);

  if (!command.drive_enable && !neutral_command)
  {
    service.stats.invalid_payloads++;
    service.protocol_fault_bits |= COMM_FAULT_BAD_COMMAND;
    return;
  }

  if ((service.current_state == COMM_STATE_BOOT) ||
      (service.current_state == COMM_STATE_SELF_TEST))
  {
    service.stats.unsafe_rearm_rejections++;
    return;
  }

  if (command.drive_enable &&
      ((service.current_state == COMM_STATE_FAULT) ||
       (service.current_state == COMM_STATE_ESTOP)))
  {
    service.stats.unsafe_rearm_rejections++;
    return;
  }

  if (command.drive_enable &&
      (service.rearm_required ||
       (service.current_state == COMM_STATE_SAFE_STOP)))
  {
    service.stats.unsafe_rearm_rejections++;
    return;
  }

  if (!neutral_command)
  {
    if (!service.drive_limits_configured)
    {
      service.stats.unconfigured_limit_rejections++;
      service.protocol_fault_bits |= COMM_FAULT_COMMAND_LIMIT;
      return;
    }

    if (!ValueWithinAbsoluteLimit(command.target_speed_mm_s,
                                  service.max_abs_speed_mm_s) ||
        !ValueWithinRange(command.target_steering_cdeg,
                          service.min_steering_cdeg,
                          service.max_steering_cdeg))
    {
      service.stats.invalid_payloads++;
      service.protocol_fault_bits |= COMM_FAULT_COMMAND_LIMIT;
      return;
    }
  }

  service.drive_command = command;
  service.drive_command_pending = true;
  service.last_valid_drive_tick = HAL_GetTick();
  service.last_drive_sequence = frame->sequence;
  service.command_timed_out = false;
  if (neutral_command &&
      (service.current_state != COMM_STATE_FAULT) &&
      (service.current_state != COMM_STATE_ESTOP))
  {
    service.rearm_required = false;
  }
  service.stats.accepted_commands++;
}

static void HandleStopCommand(const CommFrame *frame)
{
  if ((frame->payload_length != CMD_STOP_PAYLOAD_LENGTH) ||
      (frame->payload[0] > COMM_STOP_REASON_LINK_SHUTDOWN))
  {
    service.stats.invalid_payloads++;
    service.protocol_fault_bits |= COMM_FAULT_BAD_COMMAND;
    return;
  }

  if (!SequenceIsNew(frame->sequence))
  {
    (void)QueueCommandResult(frame->message_id,
                             frame->sequence,
                             COMM_RESULT_DUPLICATE,
                             service.current_state);
    return;
  }

  if ((service.current_state == COMM_STATE_BOOT) ||
      (service.current_state == COMM_STATE_SELF_TEST))
  {
    (void)QueueCommandResult(frame->message_id,
                             frame->sequence,
                             COMM_RESULT_INVALID_STATE,
                             service.current_state);
    return;
  }

  service.stop_command.reason = (CommStopReason)frame->payload[0];
  service.stop_command_pending = true;
  service.rearm_required = true;
  SetNeutralDriveCommand();
  service.current_state = COMM_STATE_SAFE_STOP;
  service.stats.accepted_commands++;
  (void)QueueCommandResult(frame->message_id,
                           frame->sequence,
                           COMM_RESULT_ACCEPTED,
                           service.current_state);
}

static void HandleResetFaultCommand(const CommFrame *frame)
{
  if (frame->payload_length != CMD_RESET_PAYLOAD_LENGTH)
  {
    service.stats.invalid_payloads++;
    service.protocol_fault_bits |= COMM_FAULT_BAD_COMMAND;
    return;
  }

  if (!SequenceIsNew(frame->sequence))
  {
    (void)QueueCommandResult(frame->message_id,
                             frame->sequence,
                             COMM_RESULT_DUPLICATE,
                             service.current_state);
    return;
  }

  if ((service.current_state != COMM_STATE_SAFE_STOP) &&
      (service.current_state != COMM_STATE_FAULT) &&
      (service.current_state != COMM_STATE_ESTOP))
  {
    (void)QueueCommandResult(frame->message_id,
                             frame->sequence,
                             COMM_RESULT_INVALID_STATE,
                             service.current_state);
    return;
  }

  service.reset_fault_command.acknowledged_fault_bits =
      ReadU32LittleEndian(frame->payload);
  service.reset_fault_command.request_sequence = frame->sequence;
  service.reset_fault_command_pending = true;
  service.protocol_fault_bits &=
      ~service.reset_fault_command.acknowledged_fault_bits;
  service.stats.accepted_commands++;
}

static void HandleMotorDiagnosticCommand(const CommFrame *frame)
{
  CommMotorDiagnosticCommand command;
  bool stop_request;

  if (frame->payload_length !=
      MOTOR_DIAGNOSTIC_REQUEST_PAYLOAD_LENGTH)
  {
    service.stats.invalid_payloads++;
    service.protocol_fault_bits |= COMM_FAULT_BAD_COMMAND;
    (void)QueueCommandResult(frame->message_id,
                             frame->sequence,
                             COMM_RESULT_INVALID_VALUE,
                             service.current_state);
    return;
  }

  if (!SequenceIsNew(frame->sequence))
  {
    (void)QueueCommandResult(frame->message_id,
                             frame->sequence,
                             COMM_RESULT_DUPLICATE,
                             service.current_state);
    return;
  }

  command.duty_permille = ReadI16LittleEndian(&frame->payload[0]);
  command.duration_ms = ReadU16LittleEndian(&frame->payload[2]);
  command.request_sequence = frame->sequence;
  stop_request = command.duty_permille == 0;

  if (!ValueWithinAbsoluteLimit(
          command.duty_permille,
          COMM_DIAG_MOTOR_MAX_ABS_PERMILLE) ||
      (!stop_request &&
       (command.duty_permille >
        -COMM_DIAG_MOTOR_MIN_ABS_PERMILLE) &&
       (command.duty_permille <
        COMM_DIAG_MOTOR_MIN_ABS_PERMILLE)) ||
      (!stop_request &&
       ((command.duration_ms <
         COMM_DIAG_MOTOR_MIN_DURATION_MS) ||
        (command.duration_ms >
         COMM_DIAG_MOTOR_MAX_DURATION_MS))))
  {
    service.stats.invalid_payloads++;
    (void)QueueMotorDiagnosticResponse(
        frame->sequence,
        COMM_RESULT_OUT_OF_RANGE,
        command.duty_permille,
        command.duration_ms);
    return;
  }

  if (stop_request)
  {
    command.duration_ms = 0U;
  }
  else if ((service.current_state != COMM_STATE_READY) &&
           (service.current_state != COMM_STATE_SAFE_STOP))
  {
    service.stats.unsafe_rearm_rejections++;
    (void)QueueMotorDiagnosticResponse(
        frame->sequence,
        COMM_RESULT_INVALID_STATE,
        command.duty_permille,
        command.duration_ms);
    return;
  }

  /*
   * Do not arm physical motion unless its acknowledgement has already been
   * accepted by the UART transmit queue.
   */
  if (!QueueMotorDiagnosticResponse(
          frame->sequence,
          COMM_RESULT_ACCEPTED,
          command.duty_permille,
          command.duration_ms))
  {
    return;
  }

  service.motor_diagnostic_command = command;
  service.motor_diagnostic_command_pending = true;
  service.stats.accepted_commands++;
}

static void HandleServoDiagnosticCommand(const CommFrame *frame)
{
  uint16_t pulse_us;

  if (frame->payload_length !=
      SERVO_DIAGNOSTIC_REQUEST_PAYLOAD_LENGTH)
  {
    service.stats.invalid_payloads++;
    service.protocol_fault_bits |= COMM_FAULT_BAD_COMMAND;
    (void)QueueCommandResult(frame->message_id,
                             frame->sequence,
                             COMM_RESULT_INVALID_VALUE,
                             service.current_state);
    return;
  }

  if (!SequenceIsNew(frame->sequence))
  {
    (void)QueueCommandResult(frame->message_id,
                             frame->sequence,
                             COMM_RESULT_DUPLICATE,
                             service.current_state);
    return;
  }

  pulse_us = ReadU16LittleEndian(frame->payload);
  if ((pulse_us != 0U) &&
      ((pulse_us < COMM_DIAG_SERVO_MIN_PULSE_US) ||
       (pulse_us > COMM_DIAG_SERVO_MAX_PULSE_US)))
  {
    service.stats.invalid_payloads++;
    (void)QueueCommandResult(frame->message_id,
                             frame->sequence,
                             COMM_RESULT_OUT_OF_RANGE,
                             service.current_state);
    return;
  }

  if ((pulse_us != 0U) &&
      (service.current_state != COMM_STATE_READY) &&
      (service.current_state != COMM_STATE_SAFE_STOP))
  {
    service.stats.unsafe_rearm_rejections++;
    (void)QueueCommandResult(frame->message_id,
                             frame->sequence,
                             COMM_RESULT_INVALID_STATE,
                             service.current_state);
    return;
  }

  service.servo_diagnostic_command.pulse_us = pulse_us;
  service.servo_diagnostic_command.request_sequence = frame->sequence;
  service.servo_diagnostic_command_pending = true;
  service.stats.accepted_commands++;
}

static void HandleEchoRequest(const CommFrame *frame)
{
  uint8_t payload[COMM_DIAG_ECHO_MAX_PAYLOAD + 1U];

  if (frame->payload_length > COMM_DIAG_ECHO_MAX_PAYLOAD)
  {
    service.stats.invalid_payloads++;
    service.protocol_fault_bits |= COMM_FAULT_BAD_COMMAND;
    return;
  }

  if (!SequenceIsNew(frame->sequence))
  {
    return;
  }

  payload[0] = frame->sequence;
  if (frame->payload_length > 0U)
  {
    memcpy(&payload[1], frame->payload, frame->payload_length);
  }

  if (QueueFrame(COMM_MSG_DIAG_ECHO_RESPONSE,
                 payload,
                 (uint8_t)(frame->payload_length + 1U)))
  {
    service.stats.echo_requests++;
  }
}

static void HandleFrame(const CommFrame *frame)
{
  switch (frame->message_id)
  {
    case COMM_MSG_CMD_DRIVE:
      HandleDriveCommand(frame);
      break;

    case COMM_MSG_CMD_STOP:
      HandleStopCommand(frame);
      break;

    case COMM_MSG_CMD_RESET_FAULT:
      HandleResetFaultCommand(frame);
      break;

    case COMM_MSG_DIAG_MOTOR_TEST_REQUEST:
      HandleMotorDiagnosticCommand(frame);
      break;

    case COMM_MSG_DIAG_SERVO_REQUEST:
      HandleServoDiagnosticCommand(frame);
      break;

    case COMM_MSG_DIAG_ECHO_REQUEST:
      HandleEchoRequest(frame);
      break;

    default:
      (void)SequenceIsNew(frame->sequence);
      service.stats.unknown_message_ids++;
      service.protocol_fault_bits |= COMM_FAULT_BAD_COMMAND;
      break;
  }
}

static void HandleParseResult(CommParseResult result,
                              const CommFrame *frame,
                              uint32_t now)
{
  if (result == COMM_PARSE_FRAME_READY)
  {
    NoteValidFrame(now);
    HandleFrame(frame);
  }
  else if (result == COMM_PARSE_CRC_ERROR)
  {
    service.protocol_fault_bits |= COMM_FAULT_CRC_ERROR;
  }
  else if ((result == COMM_PARSE_LENGTH_ERROR) ||
           (result == COMM_PARSE_VERSION_ERROR))
  {
    service.protocol_fault_bits |= COMM_FAULT_BAD_COMMAND;
  }
}

bool CommService_Init(UART_HandleTypeDef *uart)
{
  memset(&service, 0, sizeof(service));
  CommProtocol_ParserInit(&service.parser);

  service.command_timed_out = true;
  service.rearm_required = true;
  service.current_state = COMM_STATE_SAFE_STOP;

  if (!UartTransport_Init(&service.transport, uart))
  {
    return false;
  }

  service.initialized = true;
  return true;
}

void CommService_Process(void)
{
  uint8_t received[64];
  size_t count;
  uint32_t now;
  UartTransportStats transport_stats;

  if (!service.initialized)
  {
    return;
  }

  UartTransport_Process(&service.transport);

  do
  {
    count = UartTransport_Read(&service.transport,
                               received,
                               sizeof(received));
    for (size_t index = 0U; index < count; index++)
    {
      CommFrame frame;
      CommParseResult result;

      now = HAL_GetTick();
      result = CommProtocol_ParserPushTimed(&service.parser,
                                            received[index],
                                            now,
                                            &frame);
      HandleParseResult(result, &frame, now);
    }
  } while (count == sizeof(received));

  do
  {
    CommFrame frame;
    CommParseResult result;

    now = HAL_GetTick();
    result = CommProtocol_ParserPoll(&service.parser, now, &frame);
    HandleParseResult(result, &frame, now);
    if (result != COMM_PARSE_FRAME_READY)
    {
      break;
    }
  } while (true);

  if (!service.command_timed_out)
  {
    now = HAL_GetTick();
    if ((uint32_t)(now - service.last_valid_drive_tick) >=
        COMM_COMMAND_TIMEOUT_MS)
    {
      service.command_timed_out = true;
      service.rearm_required = true;
      service.current_state = COMM_STATE_SAFE_STOP;
      SetNeutralDriveCommand();
      service.stats.command_timeouts++;
    }
  }

  UartTransport_Process(&service.transport);
  UartTransport_GetStats(&service.transport, &transport_stats);

  if ((transport_stats.rx_overflows != service.observed_rx_overflows) ||
      (transport_stats.uart_errors != service.observed_uart_errors) ||
      (service.parser.stats.buffer_overflows !=
       service.observed_parser_overflows))
  {
    service.protocol_fault_bits |= COMM_FAULT_UART_RX_OVERFLOW;
    service.observed_rx_overflows = transport_stats.rx_overflows;
    service.observed_uart_errors = transport_stats.uart_errors;
    service.observed_parser_overflows =
        service.parser.stats.buffer_overflows;
  }

  if (((transport_stats.tx_overflows +
        transport_stats.tx_start_failures) !=
       service.observed_tx_failures) ||
      (service.stats.response_queue_failures !=
       service.observed_response_queue_failures))
  {
    service.protocol_fault_bits |= COMM_FAULT_UART_TX_ERROR;
    service.observed_tx_failures =
        transport_stats.tx_overflows + transport_stats.tx_start_failures;
    service.observed_response_queue_failures =
        service.stats.response_queue_failures;
  }
}

void CommService_SetDriveLimits(int16_t max_abs_speed_mm_s,
                                int16_t min_steering_cdeg,
                                int16_t max_steering_cdeg)
{
  if ((max_abs_speed_mm_s <= 0) ||
      (min_steering_cdeg >= 0) ||
      (max_steering_cdeg <= 0) ||
      (min_steering_cdeg >= max_steering_cdeg))
  {
    service.drive_limits_configured = false;
    service.max_abs_speed_mm_s = 0;
    service.min_steering_cdeg = 0;
    service.max_steering_cdeg = 0;
    return;
  }

  service.max_abs_speed_mm_s = max_abs_speed_mm_s;
  service.min_steering_cdeg = min_steering_cdeg;
  service.max_steering_cdeg = max_steering_cdeg;
  service.drive_limits_configured = true;
}

bool CommService_TakeDriveCommand(CommDriveCommand *command)
{
  if ((command == NULL) || !service.drive_command_pending)
  {
    return false;
  }

  *command = service.drive_command;
  service.drive_command_pending = false;
  return true;
}

bool CommService_TakeStopCommand(CommStopCommand *command)
{
  if ((command == NULL) || !service.stop_command_pending)
  {
    return false;
  }

  *command = service.stop_command;
  service.stop_command_pending = false;
  return true;
}

bool CommService_TakeResetFaultCommand(CommResetFaultCommand *command)
{
  if ((command == NULL) || !service.reset_fault_command_pending)
  {
    return false;
  }

  *command = service.reset_fault_command;
  service.reset_fault_command_pending = false;
  return true;
}

bool CommService_TakeMotorDiagnosticCommand(
    CommMotorDiagnosticCommand *command)
{
  if ((command == NULL) ||
      !service.motor_diagnostic_command_pending)
  {
    return false;
  }

  *command = service.motor_diagnostic_command;
  service.motor_diagnostic_command_pending = false;
  return true;
}

bool CommService_TakeServoDiagnosticCommand(
    CommServoDiagnosticCommand *command)
{
  if ((command == NULL) ||
      !service.servo_diagnostic_command_pending)
  {
    return false;
  }

  *command = service.servo_diagnostic_command;
  service.servo_diagnostic_command_pending = false;
  return true;
}

bool CommService_IsCommandTimedOut(void)
{
  return service.command_timed_out;
}

bool CommService_IsRearmRequired(void)
{
  return service.rearm_required;
}

uint8_t CommService_GetLastDriveSequence(void)
{
  return service.last_drive_sequence;
}

uint32_t CommService_GetFaultBits(void)
{
  return service.protocol_fault_bits;
}

void CommService_SetSystemState(CommSystemState state)
{
  if (state <= COMM_STATE_ESTOP)
  {
    service.current_state = state;
  }
}

bool CommService_SendDriveTelemetry(const CommDriveTelemetry *telemetry)
{
  uint8_t payload[DRIVE_TELEMETRY_PAYLOAD_LENGTH];

  if ((telemetry == NULL) || !service.initialized)
  {
    return false;
  }

  WriteU32LittleEndian(&payload[0], telemetry->mcu_time_ms);
  WriteU16LittleEndian(&payload[4], (uint16_t)telemetry->target_speed_mm_s);
  WriteU16LittleEndian(&payload[6], (uint16_t)telemetry->measured_speed_mm_s);
  WriteU16LittleEndian(&payload[8], (uint16_t)telemetry->motor_duty_permille);
  WriteU16LittleEndian(&payload[10],
                       (uint16_t)telemetry->steering_cmd_cdeg);
  WriteU16LittleEndian(&payload[12],
                       (uint16_t)telemetry->steering_feedback_cdeg);
  WriteU32LittleEndian(&payload[14], (uint32_t)telemetry->encoder_count);
  WriteU16LittleEndian(&payload[18], (uint16_t)telemetry->yaw_cdeg);
  payload[20] = (uint8_t)telemetry->state;
  payload[21] = telemetry->last_drive_seq;
  WriteU32LittleEndian(&payload[22], telemetry->active_fault_bits);

  return QueueFrame(COMM_MSG_TELEMETRY_DRIVE,
                    payload,
                    sizeof(payload));
}

bool CommService_SendFaultEvent(const CommFaultEvent *fault_event)
{
  uint8_t payload[FAULT_EVENT_PAYLOAD_LENGTH];

  if ((fault_event == NULL) || !service.initialized)
  {
    return false;
  }

  WriteU32LittleEndian(&payload[0], fault_event->occurred_at_ms);
  WriteU32LittleEndian(&payload[4], fault_event->active_fault_bits);
  WriteU32LittleEndian(&payload[8], fault_event->latched_fault_bits);
  payload[12] = (uint8_t)fault_event->action;
  payload[13] = (uint8_t)fault_event->state;

  return QueueFrame(COMM_MSG_FAULT_EVENT,
                    payload,
                    sizeof(payload));
}

bool CommService_SendRangeTelemetry(const CommRangeTelemetry *telemetry)
{
  uint8_t payload[RANGE_TELEMETRY_PAYLOAD_LENGTH];

  if ((telemetry == NULL) || !service.initialized)
  {
    return false;
  }

  WriteU32LittleEndian(&payload[0], telemetry->mcu_time_ms);
  WriteU16LittleEndian(&payload[4], telemetry->front_left_mm);
  WriteU16LittleEndian(&payload[6], telemetry->front_right_mm);
  WriteU16LittleEndian(&payload[8], telemetry->rear_left_mm);
  WriteU16LittleEndian(&payload[10], telemetry->rear_right_mm);
  payload[12] = (uint8_t)(telemetry->valid_mask & 0x0FU);

  return QueueFrame(COMM_MSG_TELEMETRY_RANGE,
                    payload,
                    sizeof(payload));
}

bool CommService_SendImuTelemetry(const CommImuTelemetry *telemetry)
{
  uint8_t payload[IMU_TELEMETRY_PAYLOAD_LENGTH];

  if ((telemetry == NULL) || !service.initialized)
  {
    return false;
  }

  WriteU32LittleEndian(&payload[0], telemetry->mcu_time_ms);
  WriteU16LittleEndian(&payload[4], (uint16_t)telemetry->quaternion_i_q14);
  WriteU16LittleEndian(&payload[6], (uint16_t)telemetry->quaternion_j_q14);
  WriteU16LittleEndian(&payload[8], (uint16_t)telemetry->quaternion_k_q14);
  WriteU16LittleEndian(&payload[10],
                       (uint16_t)telemetry->quaternion_real_q14);
  WriteU32LittleEndian(&payload[12], (uint32_t)telemetry->gyro_x_mdeg_s);
  WriteU32LittleEndian(&payload[16], (uint32_t)telemetry->gyro_y_mdeg_s);
  WriteU32LittleEndian(&payload[20], (uint32_t)telemetry->gyro_z_mdeg_s);
  WriteU16LittleEndian(&payload[24],
                       (uint16_t)telemetry->linear_accel_x_mm_s2);
  WriteU16LittleEndian(&payload[26],
                       (uint16_t)telemetry->linear_accel_y_mm_s2);
  WriteU16LittleEndian(&payload[28],
                       (uint16_t)telemetry->linear_accel_z_mm_s2);
  WriteU32LittleEndian(&payload[30], (uint32_t)telemetry->yaw_mdeg);
  payload[34] = telemetry->gyro_accuracy;
  payload[35] = telemetry->linear_accel_accuracy;
  payload[36] = telemetry->quaternion_accuracy;
  payload[37] = telemetry->status_flags;

  return QueueFrame(COMM_MSG_TELEMETRY_IMU, payload, sizeof(payload));
}

bool CommService_SendOdometryTelemetry(
    const CommOdometryTelemetry *telemetry)
{
  uint8_t payload[ODOMETRY_TELEMETRY_PAYLOAD_LENGTH];

  if ((telemetry == NULL) || !service.initialized)
  {
    return false;
  }

  WriteU32LittleEndian(&payload[0], telemetry->mcu_time_ms);
  WriteU32LittleEndian(&payload[4], (uint32_t)telemetry->x_mm);
  WriteU32LittleEndian(&payload[8], (uint32_t)telemetry->y_mm);
  WriteU32LittleEndian(&payload[12], (uint32_t)telemetry->yaw_mdeg);
  WriteU32LittleEndian(&payload[16], (uint32_t)telemetry->distance_mm);
  WriteU16LittleEndian(&payload[20],
                       (uint16_t)telemetry->linear_speed_mm_s);
  WriteU32LittleEndian(&payload[22],
                       (uint32_t)telemetry->yaw_rate_mdeg_s);
  WriteU16LittleEndian(&payload[26],
                       (uint16_t)telemetry->steering_cdeg);
  WriteU32LittleEndian(&payload[28],
                       (uint32_t)telemetry->curvature_micro_per_m);
  WriteU16LittleEndian(&payload[32], telemetry->status_flags);
  payload[34] = (uint8_t)telemetry->steering_source;
  payload[35] = telemetry->last_drive_seq;

  return QueueFrame(COMM_MSG_TELEMETRY_ODOMETRY,
                    payload,
                    sizeof(payload));
}

bool CommService_SendCommandResult(uint8_t request_message_id,
                                   uint8_t request_sequence,
                                   CommCommandResultCode result,
                                   CommSystemState state)
{
  if (!service.initialized)
  {
    return false;
  }

  return QueueCommandResult(request_message_id,
                            request_sequence,
                            result,
                            state);
}

bool CommService_SendServoDiagnosticResponse(
    uint8_t request_sequence,
    CommCommandResultCode result,
    uint16_t pulse_us,
    uint16_t steering_adc_raw,
    uint8_t status_flags)
{
  uint8_t payload[SERVO_DIAGNOSTIC_RESPONSE_PAYLOAD_LENGTH];

  if (!service.initialized)
  {
    return false;
  }

  payload[0] = request_sequence;
  payload[1] = (uint8_t)result;
  WriteU16LittleEndian(&payload[2], pulse_us);
  WriteU16LittleEndian(&payload[4], steering_adc_raw);
  payload[6] = status_flags;

  return QueueFrame(COMM_MSG_DIAG_SERVO_RESPONSE,
                    payload,
                    sizeof(payload));
}

void CommService_OnUartRxEvent(UART_HandleTypeDef *uart, uint16_t size)
{
  UartTransport_OnRxEvent(&service.transport, uart, size);
}

void CommService_OnUartTxComplete(UART_HandleTypeDef *uart)
{
  UartTransport_OnTxComplete(&service.transport, uart);
}

void CommService_OnUartError(UART_HandleTypeDef *uart)
{
  UartTransport_OnError(&service.transport, uart);
}

void CommService_GetStats(CommServiceStats *service_stats,
                          CommParserStats *parser_stats,
                          UartTransportStats *transport_stats)
{
  uint32_t primask = __get_PRIMASK();

  __disable_irq();
  if (service_stats != NULL)
  {
    *service_stats = service.stats;
  }
  if (parser_stats != NULL)
  {
    *parser_stats = service.parser.stats;
  }
  if (primask == 0U)
  {
    __enable_irq();
  }

  if (transport_stats != NULL)
  {
    UartTransport_GetStats(&service.transport, transport_stats);
  }
}
