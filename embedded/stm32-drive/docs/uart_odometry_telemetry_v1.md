# UART V1 Odometry Telemetry

`TELEMETRY_ODOMETRY`는 기존 UART Protocol V1에 추가된 메시지다. Frame,
CRC, sequence 및 byte order 규칙은 `uart_protocol_v1.md`와 같다.

| ID | 방향 | 이름 | Payload |
|---:|---|---|---:|
| `0x82` | STM32 -> Jetson/PC | `TELEMETRY_ODOMETRY` | 28 bytes |

20ms 주기로 전송하며 multi-byte field는 little-endian이다.

| Offset | Size | Type | Field | Unit |
|---:|---:|---|---|---|
| 0 | 4 | `int32` | `x_mm` | mm |
| 4 | 4 | `int32` | `y_mm` | mm |
| 8 | 4 | `int32` | `yaw_mdeg` | 0.001 degree |
| 12 | 4 | `int32` | `yaw_rate_mdeg_s` | 0.001 degree/s |
| 16 | 2 | `int16` | `measured_wheel_steering_cdeg` | 0.01 degree |
| 18 | 2 | `int16` | `center_steering_cdeg` | 0.01 degree |
| 20 | 2 | `uint16` | `status_flags` | `OdometryStatus` |
| 22 | 2 | `uint16` | `steering_adc_raw` | ADC count, 0..4095 |
| 24 | 4 | `uint32` | `uptime_ms` | ms |

```python
(
    x_mm,
    y_mm,
    yaw_mdeg,
    yaw_rate_mdeg_s,
    measured_wheel_steering_cdeg,
    center_steering_cdeg,
    status_flags,
    steering_adc_raw,
    uptime_ms,
) = struct.unpack("<iiiihhHHI", payload)
```

`status_flags`:

| Bit | 의미 |
|---:|---|
| 0 | 차량 geometry 미설정 |
| 1 | 조향각 미보정 또는 현재 조향각 무효 |
| 2 | 거리·시간·조향 입력값 무효 |
