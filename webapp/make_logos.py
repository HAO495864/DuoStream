"""把两张 LOGO 图压成网页用的小图。

原图 1536x1536、约 2MB，做 42px 的 LOGO 用不上。
LOGO 显示尺寸最大也就 ~46px（高分屏按 3x 算 138px），
所以：
- 主图 256x256 WebP：高分屏也够清晰，体积小
- PNG 兜底 128x128：老浏览器不支持 WebP 时用

顺便把图压成正方形并去掉可能存在的白边，
保证在圆角容器里不会露出难看的直角。
"""

from pathlib import Path

from PIL import Image

SRC = {
    "douyin": r"F:\豆包\GitHub项目下载.png",
    "bili": r"F:\豆包\GitHub项目下载 (1).png",
}
OUT_DIR = Path(__file__).resolve().parent / "static" / "img"


def square(im: Image.Image) -> Image.Image:
    """裁成正方形（居中），再缩到 256。"""
    w, h = im.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    im = im.crop((left, top, left + side, top + side))
    return im.resize((256, 256), Image.LANCZOS)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for key, src in SRC.items():
        p = Path(src)
        if not p.is_file():
            print(f"缺少: {p}")
            continue

        im = Image.open(p)
        print(f"{key}: 原图 {im.size} {im.mode} {im.format} {p.stat().st_size/1024:.0f}KB")

        # 统一转 RGB（WebP/PNG 都不要 alpha，LOGO 是满底的）
        if im.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", im.size, (10, 10, 12))
            conv = im.convert("RGBA")
            bg.paste(conv, mask=conv.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")

        im = square(im)

        webp = OUT_DIR / f"logo-{key}.webp"
        im.save(webp, "WEBP", quality=86, method=6)

        png = OUT_DIR / f"logo-{key}.png"
        im.resize((128, 128), Image.LANCZOS).save(png, "PNG", optimize=True)

        print(f"   -> {webp.name} {webp.stat().st_size/1024:.1f}KB")
        print(f"   -> {png.name}  {png.stat().st_size/1024:.1f}KB")


if __name__ == "__main__":
    main()
