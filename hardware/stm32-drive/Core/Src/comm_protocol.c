#include "comm_protocol.h"

#include <string.h>

static uint16_t CrcUpdate(uint16_t crc, uint8_t byte)
{
  crc ^= (uint16_t)byte << 8;
  for (uint8_t bit = 0U; bit < 8U; bit++)
  {
    if ((crc & 0x8000U) != 0U)
    {
      crc = (uint16_t)((crc << 1) ^ 0x1021U);
    }
    else
    {
      crc <<= 1;
    }
  }
  return crc;
}

static uint16_t ParserIndex(const CommParser *parser, uint16_t offset)
{
  uint16_t index = (uint16_t)(parser->head + offset);

  if (index >= COMM_PROTOCOL_PARSER_BUFFER_SIZE)
  {
    index = (uint16_t)(index - COMM_PROTOCOL_PARSER_BUFFER_SIZE);
  }
  return index;
}

static uint8_t ParserPeek(const CommParser *parser, uint16_t offset)
{
  return parser->buffer[ParserIndex(parser, offset)];
}

static void ParserDrop(CommParser *parser, uint16_t length)
{
  if (length > parser->count)
  {
    length = parser->count;
  }

  parser->head = ParserIndex(parser, length);
  parser->count = (uint16_t)(parser->count - length);
}

static void ParserAppend(CommParser *parser, uint8_t byte)
{
  uint16_t index;

  if (parser->count >= COMM_PROTOCOL_PARSER_BUFFER_SIZE)
  {
    ParserDrop(parser, 1U);
    parser->stats.buffer_overflows++;
    parser->stats.discarded_bytes++;
  }

  index = ParserIndex(parser, parser->count);
  parser->buffer[index] = byte;
  parser->count++;
}

static void MarkIncomplete(CommParser *parser, uint32_t now_ms)
{
  if (!parser->incomplete_timer_active)
  {
    parser->incomplete_started_ms = now_ms;
    parser->incomplete_timer_active = true;
  }
}

static bool ExpireIncompleteCandidate(CommParser *parser, uint32_t now_ms)
{
  if (!parser->incomplete_timer_active ||
      ((uint32_t)(now_ms - parser->incomplete_started_ms) <
       COMM_PROTOCOL_INCOMPLETE_TIMEOUT_MS))
  {
    return false;
  }

  if (parser->count > 0U)
  {
    ParserDrop(parser, 1U);
    parser->stats.discarded_bytes++;
  }
  parser->stats.incomplete_timeouts++;
  parser->incomplete_timer_active = false;
  return true;
}

static CommParseResult ProcessBuffered(CommParser *parser,
                                       uint32_t now_ms,
                                       CommFrame *completed_frame)
{
  CommParseResult last_error = COMM_PARSE_NONE;

  (void)ExpireIncompleteCandidate(parser, now_ms);

  for (;;)
  {
    uint8_t payload_length;
    uint16_t frame_length;
    uint16_t calculated_crc;
    uint16_t received_crc;

    while (parser->count > 0U)
    {
      if (ParserPeek(parser, 0U) != COMM_PROTOCOL_SOF1)
      {
        ParserDrop(parser, 1U);
        parser->stats.discarded_bytes++;
        parser->incomplete_timer_active = false;
        continue;
      }

      if (parser->count == 1U)
      {
        MarkIncomplete(parser, now_ms);
        return last_error;
      }

      if (ParserPeek(parser, 1U) == COMM_PROTOCOL_SOF2)
      {
        break;
      }

      ParserDrop(parser, 1U);
      parser->stats.discarded_bytes++;
      parser->incomplete_timer_active = false;
    }

    if (parser->count == 0U)
    {
      parser->incomplete_timer_active = false;
      return last_error;
    }

    if (parser->count < 6U)
    {
      MarkIncomplete(parser, now_ms);
      return last_error;
    }

    if (ParserPeek(parser, 2U) != COMM_PROTOCOL_VERSION)
    {
      parser->stats.version_errors++;
      ParserDrop(parser, 1U);
      parser->stats.discarded_bytes++;
      parser->incomplete_timer_active = false;
      last_error = COMM_PARSE_VERSION_ERROR;
      continue;
    }

    payload_length = ParserPeek(parser, 5U);
    if (payload_length > COMM_PROTOCOL_MAX_PAYLOAD)
    {
      parser->stats.length_errors++;
      ParserDrop(parser, 1U);
      parser->stats.discarded_bytes++;
      parser->incomplete_timer_active = false;
      last_error = COMM_PARSE_LENGTH_ERROR;
      continue;
    }

    frame_length =
        (uint16_t)(COMM_PROTOCOL_FRAME_OVERHEAD + payload_length);
    if (parser->count < frame_length)
    {
      MarkIncomplete(parser, now_ms);
      return last_error;
    }

    calculated_crc = 0xFFFFU;
    for (uint16_t offset = 2U;
         offset < (uint16_t)(6U + payload_length);
         offset++)
    {
      calculated_crc = CrcUpdate(calculated_crc,
                                 ParserPeek(parser, offset));
    }
    received_crc =
        (uint16_t)ParserPeek(parser, (uint16_t)(6U + payload_length)) |
        ((uint16_t)ParserPeek(parser,
                             (uint16_t)(7U + payload_length)) << 8);

    if (received_crc != calculated_crc)
    {
      parser->stats.crc_errors++;
      ParserDrop(parser, 1U);
      parser->stats.discarded_bytes++;
      parser->incomplete_timer_active = false;
      last_error = COMM_PARSE_CRC_ERROR;
      continue;
    }

    if (completed_frame != NULL)
    {
      completed_frame->version = ParserPeek(parser, 2U);
      completed_frame->message_id = ParserPeek(parser, 3U);
      completed_frame->sequence = ParserPeek(parser, 4U);
      completed_frame->payload_length = payload_length;
      for (uint8_t index = 0U; index < payload_length; index++)
      {
        completed_frame->payload[index] =
            ParserPeek(parser, (uint16_t)(6U + index));
      }
    }

    ParserDrop(parser, frame_length);
    parser->incomplete_timer_active = false;
    parser->stats.valid_frames++;
    return COMM_PARSE_FRAME_READY;
  }
}

void CommProtocol_ParserInit(CommParser *parser)
{
  if (parser != NULL)
  {
    memset(parser, 0, sizeof(*parser));
  }
}

CommParseResult CommProtocol_ParserPushTimed(CommParser *parser,
                                             uint8_t byte,
                                             uint32_t now_ms,
                                             CommFrame *completed_frame)
{
  if (parser == NULL)
  {
    return COMM_PARSE_NONE;
  }

  ParserAppend(parser, byte);
  return ProcessBuffered(parser, now_ms, completed_frame);
}

CommParseResult CommProtocol_ParserPush(CommParser *parser,
                                        uint8_t byte,
                                        CommFrame *completed_frame)
{
  return CommProtocol_ParserPushTimed(parser, byte, 0U, completed_frame);
}

CommParseResult CommProtocol_ParserPoll(CommParser *parser,
                                        uint32_t now_ms,
                                        CommFrame *completed_frame)
{
  if (parser == NULL)
  {
    return COMM_PARSE_NONE;
  }

  return ProcessBuffered(parser, now_ms, completed_frame);
}

uint16_t CommProtocol_Crc16CcittFalse(const uint8_t *data, size_t length)
{
  uint16_t crc = 0xFFFFU;

  if ((data == NULL) && (length > 0U))
  {
    return crc;
  }

  for (size_t index = 0U; index < length; index++)
  {
    crc = CrcUpdate(crc, data[index]);
  }

  return crc;
}

size_t CommProtocol_EncodeFrame(uint8_t message_id,
                                uint8_t sequence,
                                const uint8_t *payload,
                                uint8_t payload_length,
                                uint8_t *output,
                                size_t output_capacity)
{
  size_t frame_length;
  uint16_t crc;

  if ((output == NULL) ||
      ((payload == NULL) && (payload_length > 0U)) ||
      (payload_length > COMM_PROTOCOL_MAX_PAYLOAD))
  {
    return 0U;
  }

  frame_length = COMM_PROTOCOL_FRAME_OVERHEAD + payload_length;
  if (output_capacity < frame_length)
  {
    return 0U;
  }

  output[0] = COMM_PROTOCOL_SOF1;
  output[1] = COMM_PROTOCOL_SOF2;
  output[2] = COMM_PROTOCOL_VERSION;
  output[3] = message_id;
  output[4] = sequence;
  output[5] = payload_length;

  if (payload_length > 0U)
  {
    memcpy(&output[6], payload, payload_length);
  }

  crc = CommProtocol_Crc16CcittFalse(&output[2],
                                     (size_t)payload_length + 4U);
  output[6U + payload_length] = (uint8_t)(crc & 0xFFU);
  output[7U + payload_length] = (uint8_t)(crc >> 8);

  return frame_length;
}
