"""Pillow 渲染服务。

负责三类图片：
- Wordle 棋盘（7 列 × 8 行，白底，单元格按 绿/橙/深色 着色，数值列带方向箭头）
- 排行榜（沿用 PJSK 猜卡插件的排行榜视觉样式：浅色渐变背景 + 奖牌 + 名称/ID/分数列）
- 玩法帮助图
"""

import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

try:
    from pilmoji import Pilmoji

    _HAS_PILMOJI = True
except ImportError:  # pragma: no cover
    Pilmoji = None
    _HAS_PILMOJI = False

from .game_service import COLOR_DARK, COLOR_GREEN, COLOR_ORANGE

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_pjsk_wordle")


# ---------- 配色（白色页面背景） ----------
PAGE_BG = (255, 255, 255, 255)
TITLE_COLOR = (15, 23, 42, 255)
COUNTER_COLOR = (13, 148, 136, 255)
BADGE_BG = (207, 244, 238, 255)
BADGE_TEXT = (10, 143, 126, 255)
SUBTITLE_COLOR = (100, 116, 139, 255)
HEADER_BG = (241, 245, 249, 255)
HEADER_TEXT = (51, 65, 85, 255)
EMPTY_FILL = (255, 255, 255, 255)
EMPTY_STROKE = (226, 232, 240, 255)
EMPTY_DASH = (203, 213, 225, 255)
LEGEND_TEXT = (71, 85, 105, 255)
FOOTER_COLOR = (148, 163, 184, 255)

CELL_COLORS = {
    COLOR_GREEN: (21, 154, 108, 255),
    COLOR_ORANGE: (208, 143, 31, 255),
    COLOR_DARK: (46, 53, 67, 255),
}
CELL_TEXT = {
    COLOR_GREEN: (255, 255, 255, 255),
    COLOR_ORANGE: (255, 255, 255, 255),
    COLOR_DARK: (241, 245, 249, 255),
}

ARROW_TEXT = {"up": "↑", "down": "↓"}

# 棋盘布局常量（导出供布局回归测试使用）
BOARD_MARGIN_X = 44
BOARD_GAP = 14
BOARD_COL_WIDTHS = [256, 204, 180, 276, 264, 126, 132, 136]
BOARD_CELL_PADDING = 24
BOARD_DATE_FONT_SIZE = 24
BOARD_CELL_FONT_SIZE = 25

# 排行榜配色（沿用猜卡插件样式）
RANK_BG_START = (230, 240, 255)
RANK_BG_END = (200, 210, 240)
RANK_TITLE_COLOR = (30, 30, 50)
RANK_SHADOW = (180, 180, 190, 128)
RANK_HEADER = (80, 90, 120)
RANK_SCORE = (235, 120, 20)
RANK_ACC = (0, 128, 128)
RANK_SEP = (200, 200, 210, 128)


class _EmojiCanvas:
    """pilmoji 的轻封装：不可用时退回纯 Pillow 文本绘制。"""

    def __init__(self, img: Image.Image):
        self.img = img
        self._pilmoji = Pilmoji(img) if Pilmoji is not None else None

    def __enter__(self):
        if self._pilmoji is not None:
            self._pilmoji.__enter__()
        return self

    def __exit__(self, *args):
        if self._pilmoji is not None:
            return self._pilmoji.__exit__(*args)
        return False

    def text(self, xy, text, font, fill, anchor=None, **kwargs):
        if self._pilmoji is not None:
            return self._pilmoji.text(xy, text, font=font, fill=fill, anchor=anchor, **kwargs)
        ImageDraw.Draw(self.img).text(xy, text, font=font, fill=fill, anchor=anchor)

    def getsize(self, text, font):
        if self._pilmoji is not None:
            try:
                return self._pilmoji.getsize(text, font=font)
            except AttributeError:
                pass
        bbox = font.getbbox(text)
        return (bbox[2] - bbox[0], bbox[3] - bbox[1])


def _truncate(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> str:
    """超宽文本截断，尾部追加省略号。多行文本先折叠为单行（textlength 不支持多行）。"""
    if not text:
        return text
    text = " ".join(str(text).split())
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


class RenderService:
    def __init__(self, resources_dir: Path, output_dir: Path):
        self.resources_dir = Path(resources_dir)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.font_path = self.resources_dir / "font.ttf"
        self._fonts: dict[str, ImageFont.FreeTypeFont] = {}
        self._load_fonts()

    # ---------- 字体 ----------

    def _load_fonts(self):
        sizes = {
            "title": 56,
            "counter": 44,
            "badge": 27,
            "subtitle": 26,
            "header": 26,
            "cell": 25,
            "cell_date": 24,
            "legend": 27,
            "result": 32,
            "footer": 21,
            "rank_title": 46,
            "rank_header": 27,
            "rank_body": 25,
            "rank_id": 16,
            "rank_medal": 34,
        }
        try:
            for name, size in sizes.items():
                self._fonts[name] = ImageFont.truetype(str(self.font_path), size)
        except OSError as e:
            logger.error(f"[PJSK Wordle] 字体加载失败({self.font_path}): {e}，使用默认字体")
            default = ImageFont.load_default()
            for name in sizes:
                self._fonts[name] = default

    def font(self, name: str) -> ImageFont.FreeTypeFont:
        return self._fonts[name]

    # ---------- 棋盘 ----------

    def render_board(
        self,
        rows: list[list[dict]],
        max_rows: int,
        server_label: str,
        version: str,
    ) -> str | None:
        """渲染 Wordle 棋盘。rows: 已提交的猜测行（每行 7 格 {text,color,arrow}）。"""
        try:
            margin_x = BOARD_MARGIN_X
            gap = BOARD_GAP
            col_widths = list(BOARD_COL_WIDTHS)
            board_w = sum(col_widths) + gap * (len(col_widths) - 1)
            width = board_w + margin_x * 2

            header_y = 186
            header_h = 56
            row_h = 94
            row_gap = 12
            rows_top = header_y + header_h + 14
            board_h = max_rows * row_h + (max_rows - 1) * row_gap
            legend_y = rows_top + board_h + 34
            height = legend_y + 88  # 底部角标略往下

            img = Image.new("RGBA", (width, height), PAGE_BG)
            draw = ImageDraw.Draw(img)

            # --- 标题与计数 ---
            title_font = self.font("title")
            draw.text(
                (margin_x, 40),
                "PJSK WORDLE",
                font=title_font,
                fill=TITLE_COLOR,
                stroke_width=2,
                stroke_fill=TITLE_COLOR,
            )
            guess_count = len(rows)
            counter_font = self.font("counter")
            draw.text(
                (width - margin_x, 52),
                f"{guess_count} / {max_rows}",
                font=counter_font,
                fill=COUNTER_COLOR,
                anchor="ra",
            )

            # --- 题库徽章与副标题 ---
            badge_font = self.font("badge")
            badge_text = server_label
            bw = draw.textlength(badge_text, font=badge_font)
            badge_x, badge_y, badge_h = margin_x, 128, 46
            draw.rounded_rectangle(
                [badge_x, badge_y, badge_x + bw + 40, badge_y + badge_h],
                radius=badge_h // 2,
                fill=BADGE_BG,
            )
            draw.text(
                (badge_x + (bw + 40) / 2, badge_y + badge_h / 2),
                badge_text,
                font=badge_font,
                fill=BADGE_TEXT,
                anchor="mm",
            )
            subtitle_font = self.font("subtitle")
            draw.text(
                (badge_x + bw + 58, badge_y + badge_h / 2),
                "根据每次猜测的属性锁定目标曲目",
                font=subtitle_font,
                fill=SUBTITLE_COLOR,
                anchor="lm",
            )

            # --- 表头 ---
            header_font = self.font("header")
            headers = [
                "曲名",
                "上线时间",
                "是否为书下曲",
                "乐曲分类",
                "作者",
                "BPM",
                "MASTER",
                "APPEND",
            ]
            x = margin_x
            col_x = []
            for i, col_w in enumerate(col_widths):
                col_x.append(x)
                draw.rounded_rectangle(
                    [x, header_y, x + col_w, header_y + header_h],
                    radius=12,
                    fill=HEADER_BG,
                )
                draw.text(
                    (x + col_w / 2, header_y + header_h / 2),
                    headers[i],
                    font=header_font,
                    fill=HEADER_TEXT,
                    anchor="mm",
                )
                x += col_w + gap

            # --- 猜测行与空行 ---
            cell_font = self.font("cell")
            date_font = self.font("cell_date")
            for r in range(max_rows):
                y = rows_top + r * (row_h + row_gap)
                if r < len(rows):
                    self._draw_guess_row(
                        draw, rows[r], col_x, col_widths, y, row_h, cell_font, date_font
                    )
                else:
                    self._draw_empty_row(draw, col_x, col_widths, y, row_h)

            # --- 图例 ---
            legend_font = self.font("legend")
            lx = margin_x
            square = 26
            items = [
                (CELL_COLORS[COLOR_GREEN], "完全一致"),
                (CELL_COLORS[COLOR_ORANGE], "数值相近"),
                (CELL_COLORS[COLOR_DARK], "不匹配"),
            ]
            ly = legend_y + 4
            for color, label in items:
                draw.rounded_rectangle([lx, ly, lx + square, ly + square], radius=7, fill=color)
                draw.text(
                    (lx + square + 12, ly + square / 2),
                    label,
                    font=legend_font,
                    fill=LEGEND_TEXT,
                    anchor="lm",
                )
                lx += square + 12 + draw.textlength(label, font=legend_font) + 42
            lx += 14
            arrow_font = self.font("legend")
            for arrow, label in [("↑", "答案更晚 / 更高"), ("↓", "答案更早 / 更低")]:
                draw.text(
                    (lx, ly + square / 2),
                    arrow,
                    font=arrow_font,
                    fill=LEGEND_TEXT,
                    anchor="lm",
                )
                aw = draw.textlength(arrow, font=arrow_font)
                draw.text(
                    (lx + aw + 10, ly + square / 2),
                    label,
                    font=legend_font,
                    fill=LEGEND_TEXT,
                    anchor="lm",
                )
                lx += aw + 10 + draw.textlength(label, font=legend_font) + 42

            # --- 底部：结果行（左） + 角标（右） ---
            footer_font = self.font("footer")
            footer_text = f"玩法和界面借鉴自 宵崎奏Bot(watagashi-uni) | 题库版本:{version}"
            draw.text(
                (width - margin_x, height - 19),
                footer_text,
                font=footer_font,
                fill=FOOTER_COLOR,
                anchor="rm",
            )

            return self._save(img, "board")

        except Exception as e:
            logger.error(f"[PJSK Wordle] 渲染棋盘失败: {e}", exc_info=True)
            return None

    def _draw_guess_row(
        self,
        draw: ImageDraw.ImageDraw,
        row: list[dict],
        col_x: list[int],
        col_widths: list[int],
        y: int,
        row_h: int,
        cell_font,
        date_font,
    ):
        for i, cell in enumerate(row):
            x = col_x[i]
            w = col_widths[i]
            color = cell.get("color", COLOR_DARK)
            fill = CELL_COLORS.get(color, CELL_COLORS[COLOR_DARK])
            text_color = CELL_TEXT.get(color, CELL_TEXT[COLOR_DARK])
            draw.rounded_rectangle([x, y, x + w, y + row_h], radius=16, fill=fill)

            font = date_font if i == 1 else cell_font  # 日期列用略小字号保证完整显示
            text = cell.get("text", "")
            arrow = cell.get("arrow")
            max_text_w = w - 24
            if arrow:
                arrow_w = draw.textlength(ARROW_TEXT[arrow], font=font)
                max_text_w -= arrow_w + 8
            text = _truncate(draw, text, font, max_text_w)

            text_w = draw.textlength(text, font=font)
            total_w = text_w
            if arrow:
                total_w += 8 + arrow_w
            start_x = x + (w - total_w) / 2
            cy = y + row_h / 2
            draw.text((start_x, cy), text, font=font, fill=text_color, anchor="lm")
            if arrow:
                draw.text(
                    (start_x + text_w + 8, cy),
                    ARROW_TEXT[arrow],
                    font=font,
                    fill=text_color,
                    anchor="lm",
                )

    def _draw_empty_row(
        self,
        draw: ImageDraw.ImageDraw,
        col_x: list[int],
        col_widths: list[int],
        y: int,
        row_h: int,
    ):
        for i, w in enumerate(col_widths):
            x = col_x[i]
            draw.rounded_rectangle(
                [x, y, x + w, y + row_h],
                radius=16,
                fill=EMPTY_FILL,
                outline=EMPTY_STROKE,
                width=2,
            )
            draw.text(
                (x + w / 2, y + row_h / 2),
                "—",
                font=self.font("cell"),
                fill=EMPTY_DASH,
                anchor="mm",
            )

    # ---------- 排行榜（PJSK 猜卡样式） ----------

    def render_ranking(
        self,
        rows: list[dict],
        title: str = "PJSK Wordle 排行榜",
        show_id: bool = True,
    ) -> str | None:
        """渲染排行榜。rows: [{rank, user_id, user_name, display_name, score, games, wins, best}]"""
        try:
            if not rows:
                return None
            width = 850
            base_height = 250
            item_height = 70
            height = base_height + len(rows) * item_height

            img = Image.new("RGB", (width, height), RANK_BG_START)
            draw_bg = ImageDraw.Draw(img)
            for yy in range(height):
                ratio = yy / max(1, height - 1)
                color = tuple(
                    int(RANK_BG_START[c] + (RANK_BG_END[c] - RANK_BG_START[c]) * ratio)
                    for c in range(3)
                )
                draw_bg.line([(0, yy), (width, yy)], fill=color)

            img = img.convert("RGBA")
            overlay = Image.new("RGBA", img.size, (255, 255, 255, 100))
            img = Image.alpha_composite(img, overlay)

            title_font = self.font("rank_title")
            header_font = self.font("rank_header")
            body_font = self.font("rank_body")
            id_font = self.font("rank_id")
            medal_font = self.font("rank_medal")

            with _EmojiCanvas(img) as canvas:
                center_x, title_y = width // 2, 80
                canvas.text(
                    (center_x + 2, title_y + 2),
                    title,
                    font=title_font,
                    fill=RANK_SHADOW,
                    anchor="mm",
                )
                canvas.text(
                    (center_x, title_y),
                    title,
                    font=title_font,
                    fill=RANK_TITLE_COLOR,
                    anchor="mm",
                )

                headers = ["排名", "玩家", "总分", "胜场", "最佳次数"]
                col_positions = [40, 150, 470, 590, 712]
                title_h = canvas.getsize(title, font=title_font)[1]
                current_y = title_y + int(title_h / 2) + 45
                for header, hx in zip(headers, col_positions, strict=False):
                    canvas.text((hx, current_y), header, font=header_font, fill=RANK_HEADER)

                current_y += 55
                rank_icons = ["🥇", "🥈", "🥉"]
                for i, row in enumerate(rows):
                    rank = row.get("rank", i + 1)
                    display_name = row.get("display_name") or row.get("user_name") or "未知"
                    score = str(row.get("score", 0))
                    wins = str(row.get("wins", 0) or 0)
                    best = str(row.get("best") or "—")

                    rank_num_align_x = 130
                    canvas.text(
                        (rank_num_align_x, current_y),
                        str(rank),
                        font=body_font,
                        fill=RANK_TITLE_COLOR,
                        anchor="ra",
                    )
                    if i < 3:
                        canvas.text(
                            (col_positions[0], current_y - 30),
                            rank_icons[i],
                            font=medal_font,
                            fill=RANK_TITLE_COLOR,
                        )

                    name_x = col_positions[1]
                    if row.get("is_unbound_official"):
                        badge_text = "未绑定QQ"
                        badge_w = canvas.getsize(badge_text, font=id_font)[0] + 16
                        badge_y = current_y + 3
                        ImageDraw.Draw(img).rounded_rectangle(
                            [name_x, badge_y, name_x + badge_w, badge_y + 26],
                            radius=8,
                            fill=(115, 125, 150, 230),
                        )
                        canvas.text(
                            (name_x + 8, badge_y + 4),
                            badge_text,
                            font=id_font,
                            fill=(255, 255, 255, 255),
                        )
                        name_x += badge_w + 10

                    max_name_width = col_positions[2] - name_x - 20
                    d = ImageDraw.Draw(img)
                    display_name = _truncate(d, display_name, body_font, max_name_width)
                    canvas.text(
                        (name_x, current_y),
                        display_name,
                        font=body_font,
                        fill=RANK_TITLE_COLOR,
                    )

                    if show_id:
                        id_text = f"{row.get('user_name') or ''} ID: {row.get('user_id', '')}"
                        id_text = _truncate(
                            d,
                            id_text,
                            id_font,
                            col_positions[2] - col_positions[1] - 20,
                        )
                        canvas.text(
                            (col_positions[1], current_y + 32),
                            id_text,
                            font=id_font,
                            fill=RANK_HEADER,
                        )

                    canvas.text(
                        (col_positions[2], current_y),
                        score,
                        font=body_font,
                        fill=RANK_SCORE,
                    )
                    canvas.text(
                        (col_positions[3], current_y),
                        wins,
                        font=body_font,
                        fill=RANK_ACC,
                    )
                    canvas.text(
                        (col_positions[4], current_y),
                        best,
                        font=body_font,
                        fill=RANK_TITLE_COLOR,
                    )

                    if i < len(rows) - 1:
                        ImageDraw.Draw(img).line(
                            [(30, current_y + 60), (width - 30, current_y + 60)],
                            fill=RANK_SEP,
                            width=1,
                        )
                    current_y += item_height

                footer = f"Generated on {time.strftime('%Y-%m-%d %H:%M:%S')}"
                canvas.text(
                    (center_x, height - 25),
                    footer,
                    font=id_font,
                    fill=RANK_HEADER,
                    anchor="ms",
                )

            return self._save(img, "ranking")

        except Exception as e:
            logger.error(f"[PJSK Wordle] 渲染排行榜失败: {e}", exc_info=True)
            return None

    # ---------- 帮助图 ----------

    def render_help(self) -> str | None:
        try:
            width = 980
            lines = [
                ("title", "PJSK Wordle 玩法帮助"),
                ("gap", ""),
                ("section", "【玩法】"),
                ("text", "开始后从题库中随机选定一首目标歌曲，"),
                ("text", "@机器人 + 曲名或别名进行回答，在限定次数内猜出目标曲目。"),
                ("text", "每次猜测后会返回 8 个属性的对阵反馈："),
                ("text", "  🟩 完全一致　🟧 数值相近　⬛ 不匹配"),
                ("text", "  ↑ 答案更晚 / 更高　↓ 答案更早 / 更低"),
                ("text", "全部 8 格变绿即获胜；越快猜对得分越高"),
                ("text", "（按限定次数四等分，由快到慢得 4/3/2/1 分）。"),
                ("gap", ""),
                ("section", "【指令】"),
                ("text", "wordle / pjskwordle —— 开始一局"),
                ("text", "自动wordle —— 连续自动开局，发送 退出 停止"),
                ("text", "退出 —— 游戏中发送可结束当前对局 / 停止自动模式"),
                ("text", "切换国服题库 / 切换日服题库 —— 切换题库服务器"),
                ("text", "wordle排行榜 / 群wordle排行榜 —— 查看排行榜"),
                ("text", "wordle分数 —— 查看我的战绩"),
                ("text", "wordle绑定 QQ号 —— QQ官方账号绑定"),
                ("text", "wordle帮助 —— 显示本帮助"),
            ]

            line_heights = {"title": 76, "section": 56, "text": 52, "gap": 20}
            height = 60 + sum(line_heights.get(kind, 50) for kind, _ in lines) + 40
            img = Image.new("RGBA", (width, height), PAGE_BG)
            draw = ImageDraw.Draw(img)

            y = 46
            for kind, text in lines:
                if kind == "title":
                    draw.text(
                        (width / 2, y + 18),
                        text,
                        font=self.font("title"),
                        fill=TITLE_COLOR,
                        anchor="mm",
                        stroke_width=1,
                        stroke_fill=TITLE_COLOR,
                    )
                elif kind == "section":
                    draw.text(
                        (60, y + 14),
                        text,
                        font=self.font("result"),
                        fill=COUNTER_COLOR,
                        anchor="lm",
                    )
                elif kind == "text":
                    with _EmojiCanvas(img) as canvas:
                        canvas.text(
                            (60, y + 14),
                            text,
                            font=self.font("legend"),
                            fill=LEGEND_TEXT,
                            anchor="lm",
                        )
                y += line_heights.get(kind, 50)

            return self._save(img, "help")
        except Exception as e:
            logger.error(f"[PJSK Wordle] 渲染帮助图失败: {e}", exc_info=True)
            return None

    # ---------- 输出 ----------

    def _save(self, img: Image.Image, prefix: str) -> str:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / f"{prefix}_{time.time_ns()}.png"
        img.convert("RGB").save(path, "PNG")
        return str(path)

    def cleanup_output_dir(self, max_age_seconds: int = 3600):
        """清理过期的输出图片。"""
        if not self.output_dir.exists():
            return
        now = time.time()
        for f in self.output_dir.iterdir():
            if f.is_file() and f.suffix == ".png" and now - f.stat().st_mtime > max_age_seconds:
                try:
                    f.unlink()
                except OSError:
                    pass
