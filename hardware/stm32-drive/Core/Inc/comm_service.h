#ifndef COMM_SERVICE_H
#define COMM_SERVICE_H

#ifdef __cplusplus
extern "C" {
#endif

#include "comm_protocol.h"
#include "uart_transport.h"

#include <stdbool.h>
#include <stdint.h>

typedef struct
{
  uint32_t accepted_commands;
  uint32_t duplicate_or_old_sequences;
  uint32_t invalid_payloads;
  uint32_t unknown_message_ids;
  uint32_t unsafe_rearm_rejections;
  uint32_t unconfigured_limit_rejections;
  uint32_t command_timeouts;
  uint32_t sequence_session_resets;
  uint32_t echo_requests;
  uint32_t command_results;
  uint32_t response_queue_failures;
} CommServiceStats;

bool CommService_Init(UART_HandleTypeDef *uart);
void CommService_Process(void);

/*
 * Non-zero drive commands remain blocked until measured vehicle limits are
 * provided. Steering limits are asymmetric because the final linkage travel
 * is asymmetric about center.
 */
void CommService_SetDriveLimits(int16_t max_abs_speed_mm_s,
                                int16_t min_steering_cdeg,
                                int16_t max_steering_cdeg);

bool CommService_TakeDriveCommand(CommDriveCommand *command);
bool CommService_TakeStopCommand(CommStopCommand *command);
bool CommService_TakeResetFaultCommand(CommResetFaultCommand *command);
bool CommService_TakeMotorDiagnosticCommand(
    CommMotorDiagnosticCommand *command);
bool CommService_TakeServoDiagnosticCommand(
    CommServoDiagnosticCommand *command);

bool CommService_IsCommandTimedOut(void);
bool CommService_IsRearmRequired(void);
uint8_t CommService_GetLastDriveSequence(void);
uint32_t CommService_GetFaultBits(void);
void CommService_SetSystemState(CommSystemState state);

bool CommService_SendDriveTelemetry(const CommDriveTelemetry *telemetry);
bool CommService_SendFaultEvent(const CommFaultEvent *fault_event);
bool CommService_SendRangeTelemetry(const CommRangeTelemetry *telemetry);
bool CommService_SendImuTelemetry(const CommImuTelemetry *telemetry);
bool CommService_SendOdometryTelemetry(
    const CommOdometryTelemetry *telemetry);
bool CommService_SendCommandResult(uint8_t request_message_id,
                                   uint8_t request_sequence,
                                   CommCommandResultCode result,
                                   CommSystemState state);
bool CommService_SendServoDiagnosticResponse(
    uint8_t request_sequence,
    CommCommandResultCode result,
    uint16_t pulse_us,
    uint16_t steering_adc_raw,
    uint8_t status_flags);

void CommService_OnUartRxEvent(UART_HandleTypeDef *uart, uint16_t size);
void CommService_OnUartTxComplete(UART_HandleTypeDef *uart);
void CommService_OnUartError(UART_HandleTypeDef *uart);

void CommService_GetStats(CommServiceStats *service_stats,
                          CommParserStats *parser_stats,
                          UartTransportStats *transport_stats);

#ifdef __cplusplus
}
#endif

#endif
