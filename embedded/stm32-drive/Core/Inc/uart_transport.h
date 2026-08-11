#ifndef UART_TRANSPORT_H
#define UART_TRANSPORT_H

#ifdef __cplusplus
extern "C" {
#endif

#include "stm32f4xx_hal.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define UART_TRANSPORT_RX_DMA_SIZE   256U
#define UART_TRANSPORT_RX_RING_SIZE  512U
#define UART_TRANSPORT_TX_RING_SIZE  512U

typedef struct
{
  uint32_t rx_bytes;
  uint32_t tx_bytes;
  uint32_t rx_overflows;
  uint32_t tx_overflows;
  uint32_t uart_errors;
  uint32_t rx_restart_failures;
  uint32_t tx_start_failures;
} UartTransportStats;

typedef struct
{
  UART_HandleTypeDef *uart;

  uint8_t rx_dma_buffer[UART_TRANSPORT_RX_DMA_SIZE];
  uint8_t rx_ring[UART_TRANSPORT_RX_RING_SIZE];
  volatile uint16_t rx_head;
  volatile uint16_t rx_tail;
  volatile uint16_t rx_dma_position;
  volatile bool rx_restart_pending;

  uint8_t tx_ring[UART_TRANSPORT_TX_RING_SIZE];
  volatile uint16_t tx_head;
  volatile uint16_t tx_tail;
  volatile uint16_t tx_dma_length;
  volatile bool tx_busy;

  volatile UartTransportStats stats;
} UartTransport;

bool UartTransport_Init(UartTransport *transport, UART_HandleTypeDef *uart);
void UartTransport_Process(UartTransport *transport);

size_t UartTransport_Read(UartTransport *transport, uint8_t *data, size_t capacity);
bool UartTransport_Write(UartTransport *transport, const uint8_t *data, size_t length);

bool UartTransport_UsesHandle(const UartTransport *transport,
                              const UART_HandleTypeDef *uart);
void UartTransport_OnRxEvent(UartTransport *transport,
                             UART_HandleTypeDef *uart,
                             uint16_t dma_position);
void UartTransport_OnTxComplete(UartTransport *transport,
                                UART_HandleTypeDef *uart);
void UartTransport_OnError(UartTransport *transport,
                           UART_HandleTypeDef *uart);

void UartTransport_GetStats(const UartTransport *transport,
                            UartTransportStats *stats);

#ifdef __cplusplus
}
#endif

#endif
