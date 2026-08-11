#!/usr/bin/env python3
"""조향 실측값 -> 등가 중심 조향각·회전반경 변환기.

`drive_probe --steer-scan` 으로 각 명령값에서 바퀴를 세워 놓고 실측한 각도를
넣으면, 자전거 모델이 쓰는 **등가 중심 조향각**과 예상 회전반경을 낸다.

왜 필요한가: 아커만은 설계상 좌우 바퀴 각도가 다르다. 한쪽만 재면 등가 중심각
에서 최대 3° 이상 벗어난다 (중심각 25°에서 내측 28.7° / 외측 22.1°).

    cot(d_내) = (R - T/2)/L      cot(d_외) = (R + T/2)/L
    cot(d_중) = (cot d_내 + cot d_외)/2 = R/L      <- 엄밀해

각도를 재기 어려우면 **오프셋 방식**을 쓴다. 바퀴 측면에 곧은 자를 대고, 차체
전후 방향으로 baseline 만큼 떨어진 두 점에서 횡방향 편차를 재면
`d = atan(offset/baseline)` 이다. baseline 200 mm, 1 mm 분해능이면 0.29° 다 —
각도기보다 정확하다.

입력은 **한 줄에 한 측정점**, 공백으로 구분한다 (`#` 뒤는 주석):

    명령cdeg   좌바퀴   우바퀴

사용:
    # 각도를 직접 재서 [deg]
    python3 steering_angle_calc.py <<'EOF'
    500     0.0    0.0
    0      -2.3   -2.2
    -768   -5.9   -5.5
    -1300  -8.3   -7.7
    EOF

    # 오프셋 방식 [mm] — baseline 을 주면 각도로 환산한다
    python3 steering_angle_calc.py --baseline-mm 200 < 실측.txt

    # 파일로
    python3 steering_angle_calc.py --file 실측.txt

부호 규약: **좌회전(+) 이 양수**. 우회전이면 두 바퀴 모두 음수로 넣는다.
왼쪽으로 꺾였으면 +, 오른쪽이면 - 로 적으면 된다.
"""
import argparse
import math
import sys

# vehicle_config.h 실측 (2026-07-31)
WHEELBASE_M = 0.135
FRONT_TRACK_M = 0.085
# 트림. drive_probe 는 트림을 적용하지 않으므로 명령값이 곧 전선값이다.
# 실제 조향각 0 이 되는 전선값이 이 값이어야 한다 (실측 2026-08-04: 500).
TRIM_CDEG = 500


def center_angle_deg(left_deg: float, right_deg: float) -> float:
    """좌우 실측 각도 -> 등가 중심 조향각 (코탄젠트 평균, 엄밀해).

    한쪽이 0 에 가까우면 cot 이 발산하므로 그때는 각도 평균으로 물러난다.
    """
    if abs(left_deg) < 0.2 or abs(right_deg) < 0.2:
        return (left_deg + right_deg) / 2.0
    if left_deg * right_deg < 0:
        # 좌우가 반대로 꺾였다 = 아커만이 아니다. 그대로 평균하고 경고는
        # 호출자가 낸다.
        return (left_deg + right_deg) / 2.0
    sign = 1.0 if left_deg > 0 else -1.0
    cl = 1.0 / math.tan(math.radians(abs(left_deg)))
    cr = 1.0 / math.tan(math.radians(abs(right_deg)))
    return sign * math.degrees(math.atan(1.0 / ((cl + cr) / 2.0)))


def radius_m(center_deg: float) -> float:
    if abs(center_deg) < 1e-6:
        return float('inf')
    return WHEELBASE_M / math.tan(math.radians(abs(center_deg)))


def offset_to_deg(offset_mm: float, baseline_mm: float) -> float:
    return math.degrees(math.atan(offset_mm / baseline_mm))


def radius_from_chord(chord_mm: float, turned_deg: float) -> float:
    """현(출발-도착 직선거리) + 회전각 -> 회전반경 [m].

        현 = 2R sin(theta/2)   ->   R = 현 / (2 sin(theta/2))

    theta=180 이면 현이 곧 지름이고, sin 이 최대라 기울기가 0 이다 —
    각도 판정이 5도 틀려도 반경 오차가 0.1% 뿐이다. 90도에서는 같은 5도가
    4.7% 로 실린다 (45배). 그래서 가능하면 180도로 재는 것이 좋다.
    """
    if not 0 < turned_deg < 360:
        raise ValueError('turned_deg 는 0~360 사이여야 한다')
    return (chord_mm / 1000.0) / (2.0 * math.sin(math.radians(turned_deg / 2)))


def do_chord_mode(text):
    """'cdeg 현mm 회전각deg' 줄들을 반경으로 환산해 표로 낸다."""
    rows = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split('#')[0].strip()
        if not line:
            continue
        p = line.replace(',', ' ').split()
        if len(p) != 3:
            sys.exit(f'{n}행 형식 오류: {raw!r}\n'
                     '  한 줄에 "명령cdeg 현mm 회전각deg" 세 값이어야 한다')
        try:
            rows.append((int(float(p[0])), float(p[1]), float(p[2])))
        except ValueError:
            sys.exit(f'{n}행 숫자 아님: {raw!r}')
    if not rows:
        sys.exit('입력이 비어 있다.')

    print('\n  명령    현      회전각   실측 R    각도±5도 시 R 범위      민감도')
    print('  cdeg    mm       deg       m         m ~ m')
    print('  ' + '-' * 66)
    for cdeg, chord, th in rows:
        R = radius_from_chord(chord, th)
        lo = radius_from_chord(chord, min(359.0, th + 5))
        hi = radius_from_chord(chord, max(1.0, th - 5))
        band = abs(hi - lo) / R
        mark = '' if band < 0.02 else ('  ⚠ 각도 오차가 크게 실린다'
                                       if band > 0.05 else '  △')
        print(f'  {cdeg:>6} {chord:>7.0f} {th:>9.1f} {R:>9.3f}   '
              f'{lo:.3f} ~ {hi:.3f}  {band:>6.1%}{mark}')
    print('\n  R = 현 / (2 sin(회전각/2)).  회전각 180 이면 현이 곧 지름이고')
    print('  각도 오차가 거의 안 실린다 (5도 틀려도 0.1%). 90도는 4.7% 다.')
    return 0


def solve_arc(arc_m: float, chord_mm: float):
    """호길이 + 현 -> (반경 m, 실제 회전각 deg). **yaw 를 쓰지 않는다.**

        s = R·θ                 (엔코더 호길이)
        c = 2R·sin(θ/2)         (출발-도착 직선거리)

    두 식을 나누면 R 이 소거된다:

        c/s = 2·sin(θ/2) / θ

    우변은 θ ∈ (0, 2π) 에서 1 → 0 으로 단조감소하므로 이분법으로 θ 를 구하고,
    R = s/θ 로 반경을 얻는다. 조향 트림 때문에 STM32 의 COMMAND_ESTIMATE yaw
    가 부풀려져 있어도 이 계산은 영향받지 않는다 — 그게 이 방법의 요점이다.
    """
    s = arc_m
    c = chord_mm / 1000.0
    if s <= 0:
        raise ValueError('호길이가 0 이하다')
    ratio = c / s
    if not 0.0 < ratio < 1.0:
        raise ValueError(
            f'현/호길이 = {ratio:.3f} 가 (0,1) 밖이다. 현이 호길이보다 길 수는 '
            '없다 — 두 값의 단위(m vs mm)나 기준점을 확인할 것')

    def f(th):
        return 2.0 * math.sin(th / 2.0) / th

    lo, hi = 1e-6, 2.0 * math.pi - 1e-6
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if f(mid) > ratio:
            lo = mid
        else:
            hi = mid
    th = (lo + hi) / 2.0
    return s / th, math.degrees(th)


def amplification(turned_deg: float) -> float:
    """현/호길이 비의 상대오차가 반경 오차로 증폭되는 배수.

        f(θ) = 2 sin(θ/2)/θ = c/s
        |dR/R| = |dθ/θ| = |f / (θ · f'(θ))| · |d(c/s)/(c/s)|

    180°에서 1.0 배로 최소이고, 각도가 작아지면 급격히 커진다
    (120° 2.5배 / 90° 4.7배 / 60° 10.7배). 90° 이상을 확보할 것.
    """
    a = math.radians(turned_deg)
    f = 2.0 * math.sin(a / 2) / a
    fp = (math.cos(a / 2) * a - 2.0 * math.sin(a / 2)) / a ** 2
    if abs(fp) < 1e-12:
        return float('inf')
    return abs(f / (a * fp))


def do_chord_arc_mode(text):
    """'cdeg 호길이m 현mm' 줄들을 반경으로 환산한다 (yaw 무관)."""
    rows = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split('#')[0].strip()
        if not line:
            continue
        p = line.replace(',', ' ').split()
        if len(p) != 3:
            sys.exit(f'{n}행 형식 오류: {raw!r}\n'
                     '  한 줄에 "명령cdeg 호길이m 현mm" 세 값이어야 한다')
        try:
            rows.append((int(float(p[0])), float(p[1]), float(p[2])))
        except ValueError:
            sys.exit(f'{n}행 숫자 아님: {raw!r}')
    if not rows:
        sys.exit('입력이 비어 있다.')

    print('\n  호길이(엔코더) + 현(줄자) 로 반경과 실제 회전각을 푼다.')
    print('  yaw 를 쓰지 않으므로 트림에 의한 모델 yaw 왜곡과 무관하다.')
    print('  ★ 회전각을 180°로 맞출 필요 없다 — 어느 각도든 정확히 풀린다.')
    print('    다만 각도가 작으면 측정오차가 크게 증폭된다 (아래 증폭 열).\n')
    print('  명령    호길이   현      실측 R   실제회전각  등가각   오차증폭')
    print('  cdeg     m       mm        m         deg       deg     (현1%→R)')
    print('  ' + '-' * 68)
    out = []
    for cdeg, arc, chord in rows:
        try:
            R, th = solve_arc(arc, chord)
        except ValueError as e:
            print(f'  {cdeg:>6}  {arc:>6.3f} {chord:>7.0f}   ⚠ {e}')
            continue
        eq = math.degrees(math.atan(WHEELBASE_M / R))
        amp = amplification(th)
        mark = ('' if amp <= 3.0 else
                '  ⚠ 각도가 작다' if amp <= 6.0 else
                '  ⚠⚠ 신뢰 곤란')
        print(f'  {cdeg:>6}  {arc:>6.3f} {chord:>7.0f} {R:>8.3f} '
              f'{th:>10.1f} {eq:>8.2f}  {amp:>6.2f}x{mark}')
        out.append((cdeg, R, eq))

    if out:
        print('\n  "자전거모델 등가각" = atan(L/R). 실제 조향각이 아니라 '
              '**그 반경을 내는 등가값**이다.')
        print('  안티아커만·스크럽·LUT 오차가 전부 이 한 값에 포함되어 있다 —')
        print('  turn_steering 을 이 표로 대체하면 모델 오차가 사라진다.\n')
        print('  코드에 넣을 표 (전선cdeg -> 반경m):')
        print('    RADIUS_TABLE = [')
        for cdeg, R, _eq in sorted(out, key=lambda r: r[0]):
            print(f'        ({cdeg:>6}, {R:.3f}),')
        print('    ]')
    return 0


def parse_lines(text, baseline_mm=None):
    """'cdeg left right' 줄 목록을 (cdeg, left_deg, right_deg) 로."""
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split('#')[0].strip()
        if not line:
            continue
        parts = line.replace(',', ' ').split()
        if len(parts) != 3:
            sys.exit(f'{n}행 형식 오류: {raw!r}\n'
                     '  한 줄에 "명령cdeg 좌 우" 세 값이어야 한다')
        try:
            cdeg, va, vb = int(float(parts[0])), float(parts[1]), \
                float(parts[2])
        except ValueError:
            sys.exit(f'{n}행 숫자 아님: {raw!r}')
        if baseline_mm:
            va = offset_to_deg(va, baseline_mm)
            vb = offset_to_deg(vb, baseline_mm)
        out.append((cdeg, va, vb))
    if not out:
        sys.exit('입력이 비어 있다. stdin 이나 --file 로 측정표를 주십시오.')
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--file', default=None,
                    help='측정표 파일 (없으면 stdin)')
    ap.add_argument('--baseline-mm', type=float, default=None,
                    help='주면 좌/우 값을 오프셋[mm]으로 보고 각도로 환산한다')
    ap.add_argument('--trim-cdeg', type=int, default=TRIM_CDEG,
                    help=f'조향 중립 트림 [cdeg] (기본 {TRIM_CDEG})')
    ap.add_argument('--chord-arc', action='store_true',
                    help='입력을 "명령cdeg 호길이m 현mm" 로 읽어 회전반경을 '
                         '푼다 (yaw 무관). drive_probe --arc 의 후처리')
    a = ap.parse_args(argv)

    if a.file:
        with open(a.file, encoding='utf-8') as f:
            text = f.read()
    else:
        if sys.stdin.isatty():
            ap.error('측정표를 stdin 으로 주거나 --file 을 쓰십시오 '
                     '(--help 에 예시)')
        text = sys.stdin.read()
    if a.chord_arc:
        return do_chord_arc_mode(text)
    rows = parse_lines(text, a.baseline_mm)

    print(f'\n휠베이스 {WHEELBASE_M} m · 앞트랙 {FRONT_TRACK_M} m · '
          f'트림 {a.trim_cdeg:+d} cdeg')
    print('명령cdeg 는 drive_probe 에 넣은 값 = 전선값 (트림 미적용 도구)\n')
    print('  명령    의도각    좌실측   우실측   등가중심   반경      게인')
    print('  cdeg     deg       deg      deg       deg       m         %')
    print('  ' + '-' * 66)
    warn = []
    for cdeg, l, r in rows:
        # 의도한 각도: 전선값에서 트림을 뺀 것이 제어층이 원한 각
        intended = (cdeg - a.trim_cdeg) / 100.0
        c = center_angle_deg(l, r)
        R = radius_m(c)
        rs = '   inf' if math.isinf(R) else f'{R:6.3f}'
        if abs(intended) < 0.05:
            gain = '     —'
        else:
            gain = f'{c / intended:6.0%}'
        print(f'  {cdeg:>6} {intended:>8.2f} {l:>9.2f} {r:>8.2f} '
              f'{c:>10.2f}  {rs}  {gain}')
        if l * r < 0 and abs(l) > 0.2 and abs(r) > 0.2:
            warn.append(f'  ⚠ {cdeg}: 좌우가 반대로 꺾였다 — 링키지 확인')
        elif abs(l) > 0.2 and abs(r) > 0.2 and abs(abs(l) - abs(r)) < 0.15:
            warn.append(f'  ⚠ {cdeg}: 좌우가 거의 같다 — 아커만이 아니라 '
                        '평행 링키지일 수 있다 (큰 각도에서 외측 타이어가 '
                        '끌린다)')
    for w in dict.fromkeys(warn):
        print(w)

    print('\n  의도각 = (명령 - 트림)/100.  게인 100% 면 명령대로 꺾인 것이다.')
    print('  등가중심 = 코탄젠트 평균 (엄밀해). 한쪽만 재면 최대 3° 이상 틀린다.')
    print('  반경은 이 조향각의 **이론값**이다 — 바닥 실측 원 반경과 비교할 것.')

    # 게인이 일정하면 스케일 오차, 각도가 커지며 꺾이다 되돌아오면 사점이다.
    usable = [(c, (cd - a.trim_cdeg) / 100.0)
              for cd, l, r in rows
              for c in [center_angle_deg(l, r)]
              if abs((cd - a.trim_cdeg) / 100.0) > 0.5]
    if len(usable) >= 2:
        gains = [c / i for c, i in usable]
        lo, hi = min(gains), max(gains)
        print()
        if hi - lo < 0.15:
            print(f'  판정: 게인이 {lo:.0%}~{hi:.0%} 로 거의 일정 '
                  '→ **선형 스케일 오차** (LUT cdeg↔us 환산 문제)')
        else:
            print(f'  판정: 게인이 {lo:.0%}~{hi:.0%} 로 크게 변한다 '
                  '→ **비선형·사점** (링키지 over-center 의심)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
