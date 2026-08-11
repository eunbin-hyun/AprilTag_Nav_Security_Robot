#include "uart_transport.h"

#include <string.h>

static uint16_t RingNext(uint16_t index, uint16_t size)
{
  index++;
  if (index >= size)
  {
    index = 0U;
  }
  return index;
}

static uint16_t RingFree(uint16_t head, uint16_t tail, uint16_t size)
{
  if (head >= tail)
  {
    return (uint16_t)(size - (head - tail) - 1U);
  }
  return (uint16_t)(tail - head - 1U);
}

static bool StartReceive(UartTransport *transport)
{
  transport->rx_dma_position = 0U;

  if (HAL_UARTEx_ReceiveToIdle_DMA(transport->uart,
                                   transport->rx_dma_buffer,
                                   UART_TRANSPORT_RX_DMA_SIZE) != HAL_OK)
  {
    return false;
  }

  return true;
}

static void PushRxByteFromIsr(UartTransport *transport, uint8_t byte)
{
  uint16_t next = RingNext(transport->rx_head, UART_TRANSPORT_RX_RING_SIZE);

  if (next == transport->rx_tail)
  {
    transport->stats.rx_overflows++;
    return;
  }

  transport->rx_ring[transport->rx_head] = byte;
  __DMB();
  transport->rx_head = next;
}

static void CopyDmaRangeFromIsr(UartTransport *transport,
                                uint16_t begin,
                                uint16_t end)
{
  for (uint16_t index = begin; index < end; index++)
  {
    PushRxByteFromIsr(transport, transport->rx_dma_buffer[index]);
    transport->stats.rx_bytes++;
  }
}

static void TryStartTransmit(UartTransport *transport)
{
  uint16_t head;
  uint16_t tail;
  uint16_t length;

  if ((transport->uart == NULL) || transport->tx_busy)
  {
    return;
  }

  head = transport->tx_head;
  tail = transport->tx_tail;
  if (head == tail)
  {
    return;
  }

  if (head > tail)
  {
    length = (uint16_t)(head - tail);
  }
  else
  {
    length = (uint16_t)(UART_TRANSPORT_TX_RING_SIZE - tail);
  }

  transport->tx_dma_length = length;
  transport->tx_busy = true;

  if (HAL_UART_Transmit_DMA(transport->uart,
                            &transport->tx_ring[tail],
                            length) != HAL_OK)
  {
    transport->tx_busy = false;
    transport->tx_dma_length = 0U;
    transport->stats.tx_start_failures++;
  }
}

bool UartTransport_Init(UartTransport *transport, UART_HandleTypeDef *uart)
{
  if ((transport == NULL) || (uart == NULL))
  {
    return false;
  }

  memset(transport, 0, sizeof(*transport));
  transport->uart = uart;

  if (!StartReceive(transport))
  {
    transport->rx_restart_pending = true;
    return false;
  }

  return true;
}

void UartTransport_Process(UartTransport *transport)
{
  if ((transport == NULL) || (transport->uart == NULL))
  {
    return;
  }

  if (transport->rx_restart_pending)
  {
    if (StartReceive(transport))
    {
      transport->rx_restart_pending = false;
    }
    else
    {
      transport->stats.rx_restart_failures++;
    }
  }

  TryStartTransmit(transport);
}

size_t UartTransport_Read(UartTransport *transport, uint8_t *data, size_t capacity)
{
  size_t count = 0U;

  if ((transport == NULL) || (data == NULL))
  {
    return 0U;
  }

  while ((count < capacity) && (transport->rx_tail != transport->rx_head))
  {
    data[count] = transport->rx_ring[transport->rx_tail];
    transport->rx_tail = RingNext(transport->rx_tail,
                                  UART_TRANSPORT_RX_RING_SIZE);
    count++;
  }

  return count;
}

bool UartTransport_Write(UartTransport *transport,
                         const uint8_t *data,
                         size_t length)
{
  uint32_t primask;
  uint16_t head;
  uint16_t free_bytes;

  if ((transport == NULL) || ((data == NULL) && (length > 0U)))
  {
    return false;
  }

  if (length == 0U)
  {
    return true;
  }

  if (length >= UART_TRANSPORT_TX_RING_SIZE)
  {
    transport->stats.tx_overflows++;
    return false;
  }

  primask = __get_PRIMASK();
  __disable_irq();

  head = transport->tx_head;
  free_bytes = RingFree(head,
                        transport->tx_tail,
                        UART_TRANSPORT_TX_RING_SIZE);

  if (length > free_bytes)
  {
    transport->stats.tx_overflows++;
    if (primask == 0U)
    {
      __enable_irq();
    }
    return false;
  }

  for (size_t index = 0U; index < length; index++)
  {
    transport->tx_ring[head] = data[index];
    head = RingNext(head, UART_TRANSPORT_TX_RING_SIZE);
  }

  __DMB();
  transport->tx_head = head;

  if (primask == 0U)
  {
    __enable_irq();
  }

  return true;
}

bool UartTransport_UsesHandle(const UartTransport *transport,
                              const UART_HandleTypeDef *uart)
{
  return (transport != NULL) && (transport->uart == uart);
}

void UartTransport_OnRxEvent(UartTransport *transport,
                             UART_HandleTypeDef *uart,
                             uint16_t dma_position)
{
  uint16_t old_position;

  if (!UartTransport_UsesHandle(transport, uart))
  {
    return;
  }

  if (dma_position > UART_TRANSPORT_RX_DMA_SIZE)
  {
    transport->stats.uart_errors++;
    transport->rx_restart_pending = true;
    return;
  }

  old_position = transport->rx_dma_position;

  if (dma_position > old_position)
  {
    CopyDmaRangeFromIsr(transport, old_position, dma_position);
  }
  else if (dma_position < old_position)
  {
    CopyDmaRangeFromIsr(transport,
                        old_position,
                        UART_TRANSPORT_RX_DMA_SIZE);
    CopyDmaRangeFromIsr(transport, 0U, dma_position);
  }

  if (dma_position == UART_TRANSPORT_RX_DMA_SIZE)
  {
    transport->rx_dma_position = 0U;
  }
  else
  {
    transport->rx_dma_position = dma_position;
  }
}

void UartTransport_OnTxComplete(UartTransport *transport,
                                UART_HandleTypeDef *uart)
{
  uint16_t tail;
  uint16_t completed;

  if (!UartTransport_UsesHandle(transport, uart))
  {
    return;
  }

  completed = transport->tx_dma_length;
  tail = transport->tx_tail;
  for (uint16_t index = 0U; index < completed; index++)
  {
    tail = RingNext(tail, UART_TRANSPORT_TX_RING_SIZE);
  }

  transport->tx_tail = tail;
  transport->tx_dma_length = 0U;
  transport->tx_busy = false;
  transport->stats.tx_bytes += completed;
}

void UartTransport_OnError(UartTransport *transport,
                           UART_HandleTypeDef *uart)
{
  if (!UartTransport_UsesHandle(transport, uart))
  {
    return;
  }

  transport->stats.uart_errors++;
  transport->rx_restart_pending = true;
}

void UartTransport_GetStats(const UartTransport *transport,
                            UartTransportStats *stats)
{
  uint32_t primask;

  if ((transport == NULL) || (stats == NULL))
  {
    return;
  }

  primask = __get_PRIMASK();
  __disable_irq();
  memcpy(stats, (const void *)&transport->stats, sizeof(*stats));
  if (primask == 0U)
  {
    __enable_irq();
  }
}
