"""把思源宋体裁成只含页面用到的字符的子集。

为什么不用整个字体文件：
  NotoSerifSC-VF.ttf 有 25MB。手机上通过局域网加载要等十几秒，
  而且每个访客都要下一次。页面实际只用到 400 多个字符，
  裁完只有几十 KB，加载快、流量省。

用法：
  python make_font_subset.py            # 重新生成子集
改了页面文案之后要重跑一次，否则新字会回退到系统字体。

产物：
  static/fonts/NotoSerifSC-subset.woff2   （给网页用）
  static/fonts/NotoSerifSC-subset.ttf     （兜底，老浏览器）
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont

HERE = Path(__file__).resolve().parent
INDEX = HERE / "index.html"
OUT_DIR = HERE / "static" / "fonts"

# 思源宋体的几个可能来源，按优先级找
CANDIDATES = [
    Path(r"C:\Windows\Fonts\NotoSerifSC-VF.ttf"),
    Path(r"C:\Windows\Fonts\SourceHanSerifSC-VF.otf"),
    Path(r"C:\Windows\Fonts\SourceHanSerifSC-Regular.otf"),
    Path(r"C:\Windows\Fonts\NotoSerifSC-Regular.otf"),
]

# 除了页面里的字，再带上这些，防止动态内容缺字
EXTRA = (
    "0123456789"
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    " .,:;!?'\"()[]{}<>/\\|-_+=*&%#@^~`$"
    "·—…、。，；：？！“”‘’（）《》【】〔〕"
    "０１２３４５６７８９％"
    "／＼　"
    "→←↑↓"
    "％℃"
)


def find_font() -> Path:
    for p in CANDIDATES:
        if p.is_file():
            return p
    raise SystemExit(
        "找不到思源宋体。装一个再跑：\n"
        "  https://fonts.google.com/noto/specimen/Noto+Serif+SC\n"
        "或把字体文件放到 C:\\Windows\\Fonts\\NotoSerifSC-VF.ttf"
    )


def collect_chars() -> str:
    """从 index.html 收集所有可能显示出来的字符。"""
    html = INDEX.read_text(encoding="utf-8")

    chars: set[str] = set()

    # 1) 可见文本
    body = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    body = re.sub(r"<style.*?</style>", "", body, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", body)
    chars.update(text)

    # 2) 脚本里会被动态插进 DOM 的中文（提示语、错误文案都在这）
    for script in re.findall(r"<script>(.*?)</script>", html, re.S):
        for m in re.findall(r'"([^"]*)"', script):
            chars.update(m)
        for m in re.findall(r"'([^']*)'", script):
            chars.update(m)

    # 3) 后端会返回、前端直接显示的字（步骤名、状态词）
    for extra in (
        "准备中解析链接下载中合并音视频提取音频修正容器写入元数据整理文件完成失败已存在排队",
        "条目文件已收队列中成功跳过提交耗时复制链接已复制失败复制失败",
        "服务在线已断开始终连接中未登录已登录设置已应用先输关键词先粘一个链接进来",
        "台服务设了访问码请向主机要需要访问码需要登录搜索先在这台电脑上扫码登录双击登录抖音把再回来搜",
        "开始下载已加入队列解析链接已在主机上打开目录清除了个已结束的任务",
        "没有符合筛选条件的任务还没有把链接粘到上面点开始下载",
        "加载中没有拿到热搜数据没搜到结果下载目录里还没有视频先选一个视频已选转写完成段字数",
        "转写失败已提交加载模型识别中提取音频音频提取失败文件损坏或没有音轨没识别到语音内容",
        "找不加载模型提交中转换中只换容器通常几秒编码不兼容正在转码较慢按片长耗时完成重试",
        "这台设备大概率放不了容器或编码不被手机浏览器支持可以把它换个封装只搬流不重新编码很快画质不掉",
        "转成手机能放的播不了换容器试试真出错就给可操作的提示编码未知这个文件在这里播不了",
        "解码在你这台设备上刷新文件手机可播放不了看设备播放语音转写本地抖音热搜刷新",
        "搜抖音作品抖音作品图文合集主页直播搜索下载转写播放B站其他站点新建",
        "提高速最推中慢带小鲸鱼",
        "很抱歉抱歉我无法满足错误异常请稍候",
        "第条第个共项",
        "抖音视频下载器多设备网页版下载在这台电脑上跑手机只是遥控器",
        "支不支持暂不",
        "已隐藏显示全部展开收起关闭确定取消是",
        "元天小时分钟秒",
    ):
        chars.update(extra)

    # 4) 基本字符
    chars.update(EXTRA)

    # 去掉控制字符
    return "".join(sorted(c for c in chars if c.isprintable() or c == " "))


def build(src: Path, chars: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for fmt, out_name in (("woff2", "NotoSerifSC-subset.woff2"),
                          ("ttf", "NotoSerifSC-subset.ttf")):
        font = TTFont(str(src), fontNumber=0)
        opts = subset.Options()

        # 保留 OpenType 特性，中文排版要靠这些（标点挤压、竖排等）
        opts.layout_features = ["*"]
        opts.name_IDs = ["*"]
        opts.name_legacy = True
        opts.notdef_outline = True
        opts.recalc_bounds = True
        opts.recalc_timestamp = False
        opts.drop_tables = []
        opts.passthrough_tables = False
        # 保留 hinting 会显著变大，屏幕上不需要
        opts.hinting = False
        opts.desubroutinize = True

        subsetter = subset.Subsetter(options=opts)
        subsetter.populate(text=chars)
        subsetter.subset(font)

        out = OUT_DIR / out_name
        if fmt == "woff2":
            font.flavor = "woff2"
        font.save(str(out))
        font.close()

        kb = out.stat().st_size / 1024
        print(f"  {out_name:34} {kb:8.1f} KB")


def main() -> None:
    src = find_font()
    print(f"源字体: {src}  ({src.stat().st_size / 1048576:.1f} MB)")
    chars = collect_chars()
    print(f"子集字符数: {len(chars)}")
    print("生成:")
    build(src, chars)
    print("完成")


if __name__ == "__main__":
    main()
