import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PAGE_DIR = ROOT / "output" / "qa_book_20260728_safety"
DEFAULT_OUT_DIR = ROOT / "output" / "qa_book_20260728_safety_contacts"
FONT = ImageFont.truetype(r"C:\Windows\Fonts\malgunbd.ttf", 24)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build four-page contact sheets")
    parser.add_argument("page_dir", nargs="?", type=Path, default=DEFAULT_PAGE_DIR)
    parser.add_argument("out_dir", nargs="?", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    pages = sorted(args.page_dir.glob("page-*.png"))
    if not pages:
        raise SystemExit(f"No page-*.png files found in {args.page_dir}")

    for group_index in range(0, len(pages), 4):
        group = pages[group_index : group_index + 4]
        opened = [Image.open(path).convert("RGB") for path in group]
        page_w, page_h = opened[0].size
        gutter = 34
        label_h = 44
        canvas = Image.new(
            "RGB",
            (page_w * 2 + gutter * 3, (page_h + label_h) * 2 + gutter * 3),
            "#AAB2BA",
        )
        draw = ImageDraw.Draw(canvas)
        for local_index, (path, page) in enumerate(zip(group, opened)):
            col = local_index % 2
            row = local_index // 2
            x = gutter + col * (page_w + gutter)
            y = gutter + row * (page_h + label_h + gutter)
            canvas.paste(page, (x, y + label_h))
            draw.text((x, y + 6), path.stem, font=FONT, fill="#152536")
        first = group_index + 1
        last = group_index + len(group)
        canvas.save(
            args.out_dir / f"pages-{first:02d}-{last:02d}.png",
            dpi=(140, 140),
        )


if __name__ == "__main__":
    main()
