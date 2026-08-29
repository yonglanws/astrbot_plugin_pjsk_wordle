"""渲染服务冒烟测试：棋盘（白底/配色/箭头）、排行榜、帮助图生成。"""

import os
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.game_service import GameService
from services.render_service import RenderService

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
RESOURCES = PLUGIN_ROOT / "resources"
OUTPUT = Path(os.environ.get("WORDLE_TEST_OUTPUT", PLUGIN_ROOT / "tests" / "_render_out"))


@pytest.fixture(scope="module")
def renderer():
    return RenderService(RESOURCES, OUTPUT)


def make_song(**kwargs):
    base = {
        "id": 1,
        "title": "blender",
        "cn": "blender",
        "category": "Vivid BAD SQUAD",
        "artist": "こめだわら×R Sound Design",
        "date": "2024-03-28",
        "bpm": 103.0,
        "master": 27,
        "append": False,
    }
    base.update(kwargs)
    return base


def test_board_empty(renderer):
    path = renderer.render_board([], 8, "日服题库", "a1b2c3d")
    assert path and Path(path).exists()
    img = Image.open(path)
    assert img.size[0] >= 1000
    # 白色背景
    assert img.convert("RGB").getpixel((5, 5)) == (255, 255, 255)


def test_board_midgame(renderer):
    svc = GameService()
    answer = make_song()
    guesses = [
        make_song(
            id=2,
            title="Flyway",
            cn="Flyway",
            date="2023-01-21",
            bpm=190,
            master=29,
            append=True,
            category="Leo/need",
            artist="halyosy",
        ),
        make_song(
            id=3,
            title="彗星ノ銀河",
            cn="彗星ノ銀河",
            date="2024-08-19",
            bpm=85,
            master=26,
            category="虚拟歌手",
            artist="マツシタレオ",
        ),
        make_song(
            id=4,
            title="ハッピーチートデー",
            cn="ハッピーチートデー",
            date="2024-02-10",
            bpm=124,
            master=26,
            category="虚拟歌手",
            artist="れるりり",
        ),
        make_song(
            id=5,
            title="烈火",
            cn="烈火",
            date="2024-04-30",
            bpm=132,
            master=27,
            category="Vivid BAD SQUAD",
            artist="niki",
        ),
    ]
    rows = [svc.compare(g, answer) for g in guesses]
    path = renderer.render_board(rows, 8, "日服题库", "a1b2c3d")
    assert path and Path(path).exists()


def test_board_final_row_no_result_line(renderer):
    """结束后棋盘不再绘制左下角结果文字，获胜行保持全绿。"""
    svc = GameService()
    answer = make_song()
    rows = [
        svc.compare(make_song(id=2, date="2023-01-21", bpm=190, master=29), answer),
        svc.compare(answer, answer),
    ]
    path = renderer.render_board(rows, 8, "日服题库", "a1b2c3d")
    assert path and Path(path).exists()
    img = Image.open(path).convert("RGB")
    width, height = img.size
    # 第二行（最后一次猜测，全绿行）的纵向区间：rows_top=256，行高 94，行距 12
    row_top, row_bottom = 256 + 1 * (94 + 12), 256 + 1 * (94 + 12) + 94
    found_green = any(
        img.getpixel((x, y)) == (21, 154, 108)
        for y in range(row_top, row_bottom)
        for x in range(100, width - 100, 25)
    )
    assert found_green, "获胜行应存在绿色单元格"


def test_board_fail_rows(renderer):
    svc = GameService()
    answer = make_song()
    rows = [
        svc.compare(make_song(id=i, date="2020-01-01", bpm=200, master=33), answer)
        for i in range(2, 10)
    ]
    path = renderer.render_board(rows, 8, "国服题库", "d4e5f6a")
    assert path and Path(path).exists()


def test_ranking(renderer):
    rows = [
        {
            "rank": 1,
            "user_id": "10001",
            "user_name": "旅人Elysia",
            "display_name": "旅人Elysia",
            "score": 38,
            "wins": 12,
            "best": 2,
            "is_unbound_official": False,
        },
        {
            "rank": 2,
            "user_id": "10002",
            "user_name": "三十吨苹果",
            "display_name": "三十吨苹果",
            "score": 27,
            "wins": 9,
            "best": 3,
            "is_unbound_official": False,
        },
        {
            "rank": 3,
            "user_id": "abcdef0123456789abcdef0123456789",
            "user_name": "官方机小明",
            "display_name": "官方机小明",
            "score": 15,
            "wins": 5,
            "best": 4,
            "is_unbound_official": True,
        },
    ]
    path = renderer.render_ranking(rows, title="PJSK Wordle 排行榜")
    assert path and Path(path).exists()


def test_help(renderer):
    path = renderer.render_help()
    assert path and Path(path).exists()
