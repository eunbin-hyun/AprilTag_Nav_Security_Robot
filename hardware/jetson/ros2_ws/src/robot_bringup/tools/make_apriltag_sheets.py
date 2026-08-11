#!/usr/bin/env python3
"""AprilTag(tag36h11) A4 인쇄 원본 생성. 프린터 축소를 견디게 만든다.

왜 v2 인가 — v1(160 mm)이 실인쇄에서 틀어졌다
--------------------------------------------
v1 은 검은 사각형 160 mm + 흰 여백 20 mm = 내용 200 mm 였다. A4 가 210 mm 이니
좌우 여백이 5 mm 뿐인데, 프린터의 **인쇄 불가 영역이 보통 사방 3~5 mm** 다.
그래서 앱에서 배율 100 % 를 골라도 드라이버가 "인쇄 가능 영역에 맞춤"을 다시
걸었고, 그것도 **축마다 다른 비율**로 걸렸다.

    2026-08-03 실측: 155.0 x 152.0 mm (설계 160 정사각)
    → 가로 96.9 % / 세로 95.0 %, 종횡비 +2.0 % 왜곡
    → 여백 5 mm 가정 시 예상값 154.6 x 152.4 mm 와 0.5 mm 이내 일치

종횡비 왜곡이 진짜 문제다. solvePnP 는 태그를 정사각형으로 가정하므로, 실제가
직사각형이면 **없는 기울기를 만들어낸다**. 크기 오차는 설정값으로 보정되지만
종횡비는 보정할 수 없다.

v2 가 바꾼 것
-------------
1. 내용을 175 mm 로 줄여 **사방 여백 17.5 mm** 를 확보한다. 인쇄 불가 영역과
   겹치지 않으므로 맞춤이 걸릴 이유가 없다.
2. **100 mm 검증 눈금자를 가로·세로에 넣는다.** v1 은 태그를 재서 역산해야
   했고, 그래서 한 변만 재고 종횡비 왜곡을 놓쳤다. 눈금자를 두 축에 두면
   축별 배율이 바로 나온다.
3. 생성을 스크립트로 남긴다. v1 은 즉석으로 만들어 재현할 수 없었다.

해상도: 254 dpi. `254 / 25.4 = 10` 이므로 **1 px = 정확히 0.1 mm** 다.
300 dpi 는 1 px = 0.0847 mm 라 정수 픽셀로 mm 를 못 맞춘다.

    한 칸        175 px = 17.5 mm
    검은 사각형   1400 px = 140.0 mm   ← tag_size_m 에 넣는 값
    흰 여백 포함  1750 px = 175.0 mm
    A4           2100 x 2970 px = 210.0 x 297.0 mm

사용:
    python3 make_apriltag_sheets.py --out ../apriltags
    python3 make_apriltag_sheets.py --ids 1 2 --cell-px 200   # 160 mm 로
"""
import argparse
import pathlib
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

DPI = 254                      # 1 px = 0.1 mm
PX_PER_MM = DPI / 25.4         # = 10.0
A4_W_PX, A4_H_PX = 2100, 2970
GRID = 10                      # tag36h11 전체 격자 (검은 8칸 + 흰 테두리 1칸씩)
BLACK_CELLS = 8
RULER_MM = 100.0               # 검증 눈금자 길이

FONT_PATHS = (
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
)


def mm(v: float) -> int:
    """mm -> px (254 dpi 에서 정확히 10배)."""
    return int(round(v * PX_PER_MM))


def load_font(size_px: int):
    for p in FONT_PATHS:
        if pathlib.Path(p).exists():
            return ImageFont.truetype(p, size_px)
    return ImageFont.load_default()


def read_logical_grid(raw_path: pathlib.Path) -> list:
    """원본 PNG -> 10x10 논리 격자 (0=검정, 255=흰색).

    원본 크기가 무엇이든 각 칸 중심을 표본으로 뽑는다. 배율로 resize 하면
    칸 경계에서 반픽셀이 섞일 수 있는데, 중심 표본은 그럴 일이 없다.
    """
    im = Image.open(raw_path).convert('L')
    w, h = im.size
    if w != h:
        raise SystemExit(f'{raw_path.name}: 정사각형이 아니다 ({w}x{h})')
    step = w / GRID
    px = im.load()
    grid = []
    for r in range(GRID):
        row = []
        for c in range(GRID):
            x = int((c + 0.5) * step)
            y = int((r + 0.5) * step)
            row.append(0 if px[x, y] < 128 else 255)
        grid.append(row)
    return grid


def render_tag(grid: list, cell_px: int) -> Image.Image:
    """논리 격자 -> 확대 이미지. NEAREST 만 쓴다 (보간하면 경계가 흐려진다)."""
    small = Image.new('L', (GRID, GRID))
    small.putdata([v for row in grid for v in row])
    return small.resize((GRID * cell_px, GRID * cell_px), Image.NEAREST)


def draw_ruler(d: ImageDraw.ImageDraw, x0: int, y0: int, length_px: int,
               vertical: bool, font, label: str, ticks_only: bool = False):
    """검증용 자. **채운 막대**로 그린다.

    선 + 눈금으로 그렸더니 선 굵기가 길이에 더해져 100.4 mm 로 재졌다. 자를
    재는 사람이 어디를 기준으로 삼아야 하는지 알 수 없으면 그 자는 쓸모가
    없다. 막대를 정확히 length_px 만큼 채우면 **바깥 끝 ~ 바깥 끝**이
    답이고 기준이 하나뿐이다.

    막대 위(가로) 또는 옆(세로)에 10 mm 간격 눈금을 얇게 얹어 눈대중 확인을
    돕는다. 재는 기준은 어디까지나 막대 전체 길이다.
    """
    bar = mm(3.0)
    notch = mm(2.0)
    if vertical:
        d.rectangle([x0, y0, x0 + bar - 1, y0 + length_px - 1], fill=0)
        for i in range(1, 10):
            y = y0 + round(length_px * i / 10)
            d.rectangle([x0 + bar, y, x0 + bar + notch - 1, y], fill=0)
        if not ticks_only:
            d.text((x0 + bar + notch + mm(2), y0 + length_px // 2), label,
                   fill=0, font=font, anchor='lm')
    else:
        d.rectangle([x0, y0, x0 + length_px - 1, y0 + bar - 1], fill=0)
        for i in range(1, 10):
            x = x0 + round(length_px * i / 10)
            d.rectangle([x, y0 - notch, x, y0 - 1], fill=0)
        d.text((x0 + length_px // 2, y0 + bar + mm(2)), label,
               fill=0, font=font, anchor='ma')


def build_sheet(tag_id: int, grid: list, cell_px: int) -> Image.Image:
    black_mm = BLACK_CELLS * cell_px / PX_PER_MM
    total_px = GRID * cell_px
    if total_px > A4_W_PX - mm(20.0):
        raise SystemExit(
            f'내용 {total_px / PX_PER_MM:.1f} mm 는 A4 에 여백 10 mm 를 '
            '남기지 못한다. --cell-px 를 줄여라')

    sheet = Image.new('L', (A4_W_PX, A4_H_PX), 255)
    tag = render_tag(grid, cell_px)

    # 태그는 가로 중앙, 위에서 조금 띄운다. 아래를 눈금자·설명에 쓴다.
    tx = (A4_W_PX - total_px) // 2
    ty = mm(14.0)
    sheet.paste(tag, (tx, ty))

    d = ImageDraw.Draw(sheet)
    f_big = load_font(mm(7.0))
    f_mid = load_font(mm(4.6))
    f_sml = load_font(mm(3.6))

    # 세로 눈금자는 **왼쪽 페이지 여백**에 세운다. 태그 아래에 두면 100 mm
    # 가 그대로 필요해서 설명이 들어갈 자리가 없다 (첫 시도에서 240 px 넘쳤다).
    # 검은 사각형과 23 mm 이상 떨어지므로 검출에 영향이 없다.
    # ⚠ 라벨을 붙이지 않는다. 첫 판에서 라벨 글자가 x 87~340 까지 뻗어
    #   태그의 흰 여백(quiet zone, x 175~350)을 침범했다. 그 영역은 검출에
    #   쓰이므로 아무것도 넣으면 안 된다. 설명은 아래 본문에서 가리킨다.
    ruler_px = mm(RULER_MM)
    draw_ruler(d, mm(11.0), ty, ruler_px, True, f_sml, '', ticks_only=True)

    below = ty + total_px + mm(9.0)
    d.text((tx, below), f'tag36h11  ID {tag_id}', fill=0, font=f_big)
    d.text((tx, below + mm(10.5)),
           f'검은 사각형 설계값 {black_mm:.1f} mm '
           f'(한 칸 {cell_px / PX_PER_MM:.1f} mm)',
           fill=0, font=f_mid)

    hy = below + mm(26.0)
    draw_ruler(d, tx, hy, ruler_px, False, f_sml, f'가로 검증자 {RULER_MM:.0f} mm (막대 전체 길이)')

    # 인쇄 후 해야 할 일. 종이에 적혀 있어야 실제로 한다.
    notes = (
        f'인쇄 후: 검증자 막대 두 개(아래 가로 / 왼쪽 여백 세로)의 전체 '
        f'길이를 재라. 둘 다 {RULER_MM:.0f} mm 여야 한다.',
        '두 값이 다르면 프린터가 축마다 다른 배율로 줄인 것이다.',
        '종횡비 왜곡은 소프트웨어로 보정할 수 없으니 다시 뽑아라.',
        '검은 사각형 한 변도 가로·세로 재서 tag_size_m 에 넣어라:',
        f'    ros2 launch robot_bringup tag_drive.launch.py '
        f'tag_size_m:={black_mm / 1000.0:.3f}',
        '배율 100 %. "용지에 맞춤"·"인쇄 가능 영역에 맞춤"을 꺼라.',
        '무광 라미네이팅(광택은 반사로 검출 실패). 평평한 판에 부착.',
        '흰 여백은 자르지 마라 — 검출에 필수다.',
    )
    note_y = hy + mm(15.0)
    line_h = mm(5.6)
    for i, line in enumerate(notes):
        d.text((tx, note_y + i * line_h), line, fill=0, font=f_sml)

    # 넘침 방어. 첫 판에서 세로 눈금자와 설명이 A4 밖으로 나갔다.
    bottom = note_y + len(notes) * line_h
    if bottom > A4_H_PX - mm(5.0):
        raise SystemExit(
            f'내용이 A4 아래로 넘친다: {bottom / PX_PER_MM:.1f} mm > '
            f'{(A4_H_PX - mm(5.0)) / PX_PER_MM:.1f} mm')

    # quiet zone 침범 방어. 두 번째 판에서 세로 자 라벨이 태그 흰 여백으로
    # 들어갔다 — 검출에 쓰이는 영역이라 아무 잉크도 있으면 안 된다.
    # 태그 블록 안의 잉크가 검은 사각형(가운데 8칸)과 정확히 일치해야 한다.
    band = np.array(sheet)[ty:ty + total_px, tx:tx + total_px]
    ys, xs = np.where(band < 128)
    got = (xs.min(), xs.max(), ys.min(), ys.max())
    want = (cell_px, cell_px + BLACK_CELLS * cell_px - 1) * 2
    if got != want:
        raise SystemExit(
            f'태그 블록 안에 검은 사각형 외의 잉크가 있다 (quiet zone 침범). '
            f'잉크 범위 {got}, 기대 {want}')

    return sheet


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='.', help='출력 디렉터리')
    ap.add_argument('--raw-dir', default=None,
                    help='tag36h11_id{N}_raw.png 위치 (기본: --out 과 같음)')
    ap.add_argument('--ids', type=int, nargs='+', default=[1, 2, 3, 4])
    ap.add_argument('--cell-px', type=int, default=175,
                    help='한 칸 픽셀. 254 dpi 에서 175 = 17.5 mm '
                         '(검은 사각형 140 mm)')
    ap.add_argument('--suffix', default='v2', help='파일명 꼬리표')
    args = ap.parse_args(argv)

    out = pathlib.Path(args.out)
    raw_dir = pathlib.Path(args.raw_dir) if args.raw_dir else out
    out.mkdir(parents=True, exist_ok=True)
    black_mm = BLACK_CELLS * args.cell_px / PX_PER_MM

    for tag_id in args.ids:
        raw = raw_dir / f'tag36h11_id{tag_id}_raw.png'
        if not raw.exists():
            raise SystemExit(f'원본이 없다: {raw}')
        grid = read_logical_grid(raw)
        sheet = build_sheet(tag_id, grid, args.cell_px)
        stem = f'tag36h11_id{tag_id}_{black_mm:.0f}mm_A4_{args.suffix}'
        sheet.save(out / f'{stem}.png', dpi=(DPI, DPI))
        sheet.save(out / f'{stem}.pdf', 'PDF', resolution=DPI)
        print(f'{stem}.pdf  검은 사각형 {black_mm:.1f} mm  '
              f'내용 {GRID * args.cell_px / PX_PER_MM:.1f} mm  '
              f'여백 {(A4_W_PX - GRID * args.cell_px) / 2 / PX_PER_MM:.1f} mm')
    return 0


if __name__ == '__main__':
    sys.exit(main())
