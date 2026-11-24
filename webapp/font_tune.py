"""思源宋体适配：把小字号整体放大一档。

宋体和黑体不是换名字就行：
- 宋体笔画细、字面小，同样 12px 会比黑体显得更小更浅
- 手机上尤其明显，小字容易糊

这里只动 font-size 的数值，不动别的属性：
  11px → 12px   11.5px → 12.5px   12px → 13px
  12.3px → 13px  12.5px → 13.5px  12.8px → 13.5px
  13px → 13.5px（正文附近的不再放大，避免整体撑开）
大标题（14px 以上）保持不动，否则版式会被撑坏。
"""

import re
from pathlib import Path

P = Path(__file__).resolve().parent / "index.html"

# 只放大 11 ~ 12.8 这一段
MAP = {
    "11px": "12px",
    "11.5px": "12.5px",
    "12px": "13px",
    "12.3px": "13px",
    "12.5px": "13.5px",
    "12.8px": "13.5px",
}


def main() -> None:
    s = P.read_text(encoding="utf-8")

    # 只在 <style> 块里替换，避免动到 JS 里的字符串
    style_re = re.compile(r"(<style>)(.*?)(</style>)", re.S)

    changed = [0]

    def fix(m: re.Match) -> str:
        css = m.group(2)

        def one(mm: re.Match) -> str:
            val = mm.group(1) + "px"
            new = MAP.get(val)
            if new:
                changed[0] += 1
                return "font-size:" + new
            return mm.group(0)

        css2 = re.sub(r"font-size:([\d.]+)px", one, css)
        return m.group(1) + css2 + m.group(3)

    out = style_re.sub(fix, s)

    if out != s:
        P.write_text(out, encoding="utf-8")
    print(f"调整了 {changed[0]} 处字号")


if __name__ == "__main__":
    main()
