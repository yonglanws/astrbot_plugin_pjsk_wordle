"""棋盘布局回归测试：确保常见文本在既定列宽下不会被截断。"""

import os
import sys

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import render_service as rs

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_PATH = os.path.join(PLUGIN_ROOT, "resources", "font.ttf")


def _font(size):
    return ImageFont.truetype(FONT_PATH, size)


def test_cell_text_fits_columns():
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    cell_font = _font(rs.BOARD_CELL_FONT_SIZE)
    date_font = _font(rs.BOARD_DATE_FONT_SIZE)
    widths = rs.BOARD_COL_WIDTHS
    pad = rs.BOARD_CELL_PADDING

    # (列, 文本, 是否带箭头, 字体)
    cases = [
        (0, "ハッピーチートデー", False, cell_font),
        (0, "blender", False, cell_font),
        (0, "不了。", False, cell_font),
        (1, "2024-03-28", True, date_font),
        (1, "2020-02-29", True, date_font),
        (2, "是", False, cell_font),
        (2, "否", False, cell_font),
        (3, "Vivid BAD SQUAD", False, cell_font),
        (3, "MORE MORE JUMP!", False, cell_font),
        (3, "虚拟歌手", False, cell_font),
        (3, "25时，Nightcord见。", False, cell_font),
        (5, "200", True, cell_font),
        (5, "33", True, cell_font),
        (6, "27", True, cell_font),
        (7, "有", False, cell_font),
        (7, "无", False, cell_font),
    ]
    for col, text, has_arrow, font in cases:
        max_w = widths[col] - pad
        if has_arrow:
            max_w -= draw.textlength("↓", font=font) + 8
        text_w = draw.textlength(text, font=font)
        assert text_w <= max_w, f"列{col} 文本'{text}'({text_w:.0f}px) 超出可用宽度 {max_w}px"


def test_header_text_fits_columns():
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    header_font = _font(26)
    headers = ["曲名", "上线时间", "是否为书下曲", "乐曲分类", "作者", "BPM", "MASTER", "APPEND"]
    for col, text in enumerate(headers):
        text_w = draw.textlength(text, font=header_font)
        max_w = rs.BOARD_COL_WIDTHS[col] - rs.BOARD_CELL_PADDING
        assert text_w <= max_w, f"表头列{col} '{text}'({text_w:.0f}px) 超出 {max_w}px"


def test_board_dims_stable():
    total = sum(rs.BOARD_COL_WIDTHS) + rs.BOARD_GAP * (len(rs.BOARD_COL_WIDTHS) - 1)
    width = total + rs.BOARD_MARGIN_X * 2
    assert 1400 <= width <= 1850
