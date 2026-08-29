"""游戏核心逻辑单元测试：反馈颜色/箭头、计分、名称匹配。"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.game_service import (
    ARROW_DOWN,
    ARROW_UP,
    COLOR_DARK,
    COLOR_GREEN,
    COLOR_ORANGE,
    GameService,
    SongMatcher,
    WordleGame,
    normalize_text,
    score_for_guess_count,
)


def make_song(**kwargs):
    base = {
        "id": 1,
        "title": "blender",
        "cn": "blender",
        "aliases": ["布伦德"],
        "category": "Vivid BAD SQUAD",
        "artist": "こめだわら×R Sound Design",
        "date": "2024-03-28",
        "bpm": 103.0,
        "master": 27,
        "append": False,
        "newly_written": False,
    }
    base.update(kwargs)
    return base


ANSWER = make_song()


class TestNormalize:
    def test_fullwidth_and_case(self):
        assert normalize_text("ＲＯＫＩ") == "roki"
        assert normalize_text("Tell Your World") == "tellyourworld"

    def test_spaces_removed(self):
        assert normalize_text("  ロ キ  ") == "ロキ"

    def test_empty(self):
        assert normalize_text("") == ""
        assert normalize_text(None) == ""


class TestScoring:
    @pytest.mark.parametrize(
        "count,score",
        [(1, 4), (2, 4), (3, 3), (4, 3), (5, 2), (6, 2), (7, 1), (8, 1), (9, 0)],
    )
    def test_tiers(self, count, score):
        assert score_for_guess_count(count) == score


class TestCompare:
    def test_all_green_on_answer(self):
        row = GameService().compare(ANSWER, ANSWER)
        assert len(row) == 8
        assert all(cell["color"] == COLOR_GREEN for cell in row)
        assert all(cell["arrow"] is None for cell in row)

    def test_name_column(self):
        guess = make_song(id=2, title="Flyway", cn="Flyway", date="2023-01-21", bpm=190, master=29)
        row = GameService().compare(guess, ANSWER)
        assert row[0]["text"] == "Flyway"
        assert row[0]["color"] == COLOR_DARK

    def test_date_orange_and_arrow_up(self):
        # 答案比猜测晚 37 天 → 橙色 + ↑
        guess = make_song(id=2, date="2024-02-20")
        row = GameService().compare(guess, ANSWER)
        assert row[1]["color"] == COLOR_ORANGE
        assert row[1]["arrow"] == ARROW_UP

    def test_date_dark_and_arrow_down(self):
        # 答案比猜测早 400+ 天 → 深色 + ↓
        guess = make_song(id=2, date="2025-06-01")
        row = GameService().compare(guess, ANSWER)
        assert row[1]["color"] == COLOR_DARK
        assert row[1]["arrow"] == ARROW_DOWN

    def test_date_boundary_orange(self):
        guess = make_song(id=2, date="2023-09-30")  # 恰好 180 天
        row = GameService().compare(guess, ANSWER)
        assert row[1]["color"] == COLOR_ORANGE

    def test_date_beyond_threshold(self):
        guess = make_song(id=2, date="2023-09-29")  # 181 天
        row = GameService().compare(guess, ANSWER)
        assert row[1]["color"] == COLOR_DARK

    def test_newly_written_column(self):
        """书下曲列：是/否，精确匹配为绿。"""
        guess = make_song(id=2, date=ANSWER["date"], bpm=ANSWER["bpm"], master=ANSWER["master"])
        row = GameService().compare(guess, ANSWER)
        assert row[2]["text"] == "否"
        assert row[2]["color"] == COLOR_GREEN  # 双方都不是书下曲 → 一致

        # 猜“是”但答案不是书下曲 → 深色
        guess_yes = make_song(
            id=3,
            newly_written=True,
            date=ANSWER["date"],
            bpm=ANSWER["bpm"],
            master=ANSWER["master"],
        )
        row = GameService().compare(guess_yes, ANSWER)
        assert row[2]["text"] == "是"
        assert row[2]["color"] == COLOR_DARK

        # 答案是书下曲、猜“是” → 绿
        row = GameService().compare(guess_yes, make_song(newly_written=True))
        assert row[2]["text"] == "是"
        assert row[2]["color"] == COLOR_GREEN

    def test_bpm_close_orange(self):
        guess = make_song(id=2, bpm=95)
        row = GameService().compare(guess, ANSWER)
        assert row[5]["color"] == COLOR_ORANGE
        assert row[5]["arrow"] == ARROW_UP

    def test_bpm_exact_green(self):
        guess = make_song(id=2, bpm=103.0)
        row = GameService().compare(guess, ANSWER)
        assert row[5]["color"] == COLOR_GREEN
        assert row[5]["arrow"] is None
        assert row[5]["text"] == "103"

    def test_master_dark_arrow_down(self):
        guess = make_song(id=2, master=30)
        row = GameService().compare(guess, ANSWER)
        assert row[6]["color"] == COLOR_DARK
        assert row[6]["arrow"] == ARROW_DOWN

    def test_category_artist_append(self):
        guess = make_song(
            id=2,
            category="Leo/need",
            artist="niki",
            append=True,
            date=ANSWER["date"],
            bpm=ANSWER["bpm"],
            master=ANSWER["master"],
        )
        row = GameService().compare(guess, ANSWER)
        assert row[3]["color"] == COLOR_DARK
        assert row[3]["text"] == "Leo/need"
        assert row[4]["color"] == COLOR_DARK
        assert row[7]["color"] == COLOR_DARK
        assert row[7]["text"] == "有"

    def test_missing_bpm(self):
        guess = make_song(id=2, bpm=None)
        row = GameService().compare(guess, ANSWER)
        assert row[5]["text"] == "?"
        assert row[5]["color"] == COLOR_DARK


class TestMatcher:
    SONGS = [
        make_song(),
        make_song(id=2, title="ロキ", cn="ROKI", aliases=["洛基", "口丰"]),
        make_song(id=3, title="テオ", cn="将手", aliases=["碲氧"]),
    ]

    def test_match_title(self):
        m = SongMatcher(self.SONGS)
        assert m.find("blender")["id"] == 1

    def test_match_cn(self):
        m = SongMatcher(self.SONGS)
        assert m.find("将手")["id"] == 3
        assert m.find("ROKI")["id"] == 2

    def test_match_alias_fullwidth(self):
        m = SongMatcher(self.SONGS)
        assert m.find("洛基")["id"] == 2

    def test_match_ignores_spaces(self):
        m = SongMatcher(self.SONGS)
        assert m.find("b len der")["id"] == 1

    def test_no_match(self):
        m = SongMatcher(self.SONGS)
        assert m.find("不存在的歌") is None
        assert m.find("") is None

    def test_fuzzy_one_char_diff(self):
        """别名容错：恰好差一个字（如把“25时的情热”打成“25时的情熟”）也能命中。"""
        songs = [
            make_song(id=9, title="25時の情熱", cn="25时的热情", aliases=["25时的情热", "情热"])
        ]
        m = SongMatcher(songs)
        assert m.find("25时的情熟")["id"] == 9  # 熟/热 一字之差（替换）
        assert m.find("25时的情")["id"] == 9  # 漏一个字（删除）
        assert m.find("25时了的情热")["id"] == 9  # 多一个字（插入）
        assert m.find("25时的情热")["id"] == 9  # 精确优先

    def test_fuzzy_distance_ranking(self):
        """多个模糊候选时，编辑距离更小者优先。"""
        songs = [
            make_song(id=9, title="25時の情熱", cn="25时的热情", aliases=["25时的情热"]),
            make_song(id=10, title="ニジェ", cn="ニジェ", aliases=["25时的情热曲"]),
        ]
        m = SongMatcher(songs)
        # “25时的情熟” 距离 9 号为 1、10 号为 2 → 命中 9 号
        assert m.find("25时的情熟")["id"] == 9

    def test_fuzzy_long_input_two_chars(self):
        """长输入（≥8）允许两个字符的差错。"""
        songs = [make_song(id=7, title="Helloworld", cn="你好世界", aliases=[])]
        m = SongMatcher(songs)
        assert m.find("helooworlx")["id"] == 7  # 距离 2
        # 长度 <8 时两个差错不命中
        m2 = SongMatcher(self.SONGS)
        assert m2.find("blendxy") is None  # 距离 2，但长度 7 只容 1

    def test_fuzzy_containment_unique(self):
        """包含匹配：只记得半个名字，且唯一命中一首歌时可用。"""
        songs = [
            make_song(id=6, title="Nothing but Life", cn="只为生命", aliases=["nothing but life"])
        ]
        m = SongMatcher(songs)
        assert m.find("nothing but li")["id"] == 6
        assert m.find("but life是") is None  # 不构成包含关系（含额外字符）时不命中

    def test_fuzzy_containment_ambiguous(self):
        """包含匹配命中多首歌时宁可不作答。"""
        songs = [
            make_song(id=11, title="LoveSong", cn="恋曲", aliases=["love song"]),
            make_song(id=12, title="LoveStory", cn="恋事", aliases=["love story"]),
        ]
        m = SongMatcher(songs)
        assert m.find("love") is None  # "love" 同时是两个别名的子串

    def test_fuzzy_min_length_guard(self):
        """过短的输入不参与模糊层。"""
        short = [make_song(id=5, title="テオ", cn="将手", aliases=["热"])]
        m2 = SongMatcher(short)
        assert m2.find("熟") is None  # 长度 <4 不做容错


class TestWordleGame:
    def test_win_flow(self):
        game = WordleGame(ANSWER, "jp")
        other = make_song(id=2)
        r1 = game.guess(other, "u1", "p1")
        assert r1["result"] == "ongoing"
        assert game.guess_count == 1
        r2 = game.guess(ANSWER, "u2", "p2")
        assert r2["result"] == "win"
        assert game.finished and game.won
        assert game.winner_id == "u2"

    def test_fail_after_max(self):
        game = WordleGame(ANSWER, "jp", max_guesses=2)
        game.guess(make_song(id=2))
        r = game.guess(make_song(id=3))
        assert r["result"] == "fail"
        assert game.finished and not game.won

    def test_guess_after_finish_noop(self):
        game = WordleGame(ANSWER, "jp")
        game.guess(ANSWER)
        r = game.guess(make_song(id=2))
        assert r["result"] == "finished"
        assert game.guess_count == 1

    def test_forfeit(self):
        game = WordleGame(ANSWER, "jp")
        game.guess(make_song(id=2))
        game.forfeit("timeout")
        assert game.finished and not game.won
        assert game.end_reason == "timeout"
