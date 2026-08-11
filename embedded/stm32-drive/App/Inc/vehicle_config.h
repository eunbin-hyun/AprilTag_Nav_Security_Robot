#ifndef APP_VEHICLE_CONFIG_H
#define APP_VEHICLE_CONFIG_H

/*
 * Enable each final-vehicle calibration only after it is measured on the
 * assembled OrinCar. The servo command calibration and odometry geometry below
 * are enabled; steering feedback remains disabled. The production UART speed
 * limit and motor output ceiling use the measured no-load 95% PWM operating
 * point from the assembled vehicle.
 */

#define VEHICLE_STEERING_SENSOR_CALIBRATED 0U
#define VEHICLE_STEERING_LEFT_RAW          0U
#define VEHICLE_STEERING_CENTER_RAW        0U
#define VEHICLE_STEERING_RIGHT_RAW         0U
#define VEHICLE_STEERING_LEFT_ANGLE_DEG    0.0f
#define VEHICLE_STEERING_RIGHT_ANGLE_DEG   0.0f

/*
 * 1: allow speed control with calibrated servo command angles when no physical
 *    steering feedback sensor is mounted. Telemetry keeps feedback invalid,
 *    while odometry may explicitly use the calibrated center-angle command as
 *    an estimated input when vehicle geometry is calibrated.
 * 0: require calibrated, valid steering feedback before speed control.
 */
#define VEHICLE_ALLOW_STEERING_COMMAND_ESTIMATE 1U

/*
 * Seven-point center-equivalent steering LUT derived from both measured front
 * wheel angles. Positive steering is left/CCW. Intermediate command angles are
 * piecewise-linearly interpolated; commands outside the measured range clamp
 * to the nearest endpoint. All pulse points include the same +16 us trim from
 * the original 1215 us center so that the verified straight pulse is 1231 us.
 */
#define VEHICLE_STEERING_SERVO_CALIBRATED 1U
#define VEHICLE_SERVO_LUT_0_ANGLE_DEG      19.55f
#define VEHICLE_SERVO_LUT_0_PULSE_US       766U
#define VEHICLE_SERVO_LUT_1_ANGLE_DEG      14.18f
#define VEHICLE_SERVO_LUT_1_PULSE_US       921U
#define VEHICLE_SERVO_LUT_2_ANGLE_DEG      10.27f
#define VEHICLE_SERVO_LUT_2_PULSE_US       1076U
#define VEHICLE_SERVO_LUT_3_ANGLE_DEG      0.0f
#define VEHICLE_SERVO_LUT_3_PULSE_US       1231U
#define VEHICLE_SERVO_LUT_4_ANGLE_DEG      -7.09f
#define VEHICLE_SERVO_LUT_4_PULSE_US       1386U
#define VEHICLE_SERVO_LUT_5_ANGLE_DEG      -17.56f
#define VEHICLE_SERVO_LUT_5_PULSE_US       1541U
#define VEHICLE_SERVO_LUT_6_ANGLE_DEG      -28.69f
#define VEHICLE_SERVO_LUT_6_PULSE_US       1696U

/*
 * Assembled OrinCar measurements:
 * - wheelbase: 13.5 cm
 * - front steering-pivot center distance: 8.5 cm
 */
#define VEHICLE_ODOMETRY_GEOMETRY_CALIBRATED 1U
#define VEHICLE_WHEELBASE_M                   0.135f
#define VEHICLE_FRONT_STEERING_TRACK_M        0.085f

/* 0: left front wheel sensor, 1: right front wheel sensor. */
#define VEHICLE_STEERING_SENSOR_SIDE 0U

/*
 * BNO085 mounting and odometry heading correction.
 * Mount the module rigidly with its sensor X axis forward, Y axis left, and
 * Z axis up. Positive Z angular rate must therefore mean a left/CCW turn.
 * Change VEHICLE_IMU_YAW_SIGN to -1.0f if the final physical mounting reverses
 * that sign. The blend weight is an initial commissioning value and must be
 * validated on the assembled vehicle.
 */
#define VEHICLE_IMU_REPORT_INTERVAL_US       10000UL
#define VEHICLE_IMU_STALE_TIMEOUT_MS         100UL
#define VEHICLE_IMU_DISCONNECT_TIMEOUT_MS    500UL
/* Keep accuracy in telemetry, but do not block fusion when BNO085 reports g0. */
#define VEHICLE_IMU_ENFORCE_ACCURACY_GATE     0U
#define VEHICLE_IMU_MIN_FUSION_ACCURACY      1U
#define VEHICLE_IMU_MAX_YAW_RATE_RAD_S       10.0f
#define VEHICLE_IMU_YAW_SIGN                 1.0f
#define VEHICLE_IMU_FUSION_MIN_SPEED_MPS     0.02f

#define VEHICLE_ODOMETRY_HEADING_MODEL_ONLY     0U
#define VEHICLE_ODOMETRY_HEADING_COMPLEMENTARY 1U
#define VEHICLE_ODOMETRY_HEADING_IMU_ONLY      2U

#ifndef VEHICLE_ODOMETRY_HEADING_MODE
#define VEHICLE_ODOMETRY_HEADING_MODE \
    VEHICLE_ODOMETRY_HEADING_MODEL_ONLY
#endif

#ifndef VEHICLE_IMU_HEADING_CORRECTION_WEIGHT
#define VEHICLE_IMU_HEADING_CORRECTION_WEIGHT 0.75f
#endif

/*
 * Full calibrated steering-command range. Positive steering is left/CCW.
 * The limits are asymmetric because the assembled linkage reaches different
 * center-equivalent angles at the center-trimmed 766 us and 1696 us endpoints.
 */
/*
 * The previous 100 mm/s bench cap is replaced by the measured sustained
 * no-load speed at 95% PWM: 1565.3 mm/s, rounded down to 1565 mm/s.
 * Ground-loaded maximum speed remains to be measured.
 */
#define VEHICLE_MAX_ABS_SPEED_MM_S     1565
#define VEHICLE_MIN_STEERING_CDEG      (-2869)
#define VEHICLE_MAX_STEERING_CDEG      1955

/*
 * Local HC-SR04 safety interlock.
 * 0: keep range telemetry, but do not stop motion for range/health results.
 * 1: enforce the ultrasonic caution/stop/stale safety state machine.
 */
#define VEHICLE_ENFORCE_ULTRASONIC_SAFETY 0U

/*
 * The current vehicle has no rear range sensor.  Allow reverse commands while
 * retaining the existing ultrasonic, E-stop, communication, and stale-state
 * stop conditions.  Set this to 0 if rear obstacle protection becomes a
 * mandatory interlock.
 */
#define VEHICLE_ALLOW_REVERSE_WITHOUT_REAR_SENSOR 1U

/*
 * 1: on-board ST-LINK VCP (USART1 / COM port) for bench calibration.
 * 0: Jetson connector (UART5 / PC12 TX, PD2 RX) for final integration.
 */
#define VEHICLE_COMM_USE_STLINK_VCP 0U

#endif
