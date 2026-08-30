import sys
from pathlib import Path
import pytest

PLUGIN_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

from services.game_service import SongMatcher, WordleGame


# 模拟典型的 PJSK 歌曲题库
SAMPLE_SONGS = [
    {
        "id": 1,
        "title": "Tell Your World",
        "cn": "告诉你的世界",
        "aliases": ["tyw", "初音开机曲", "告诉世界", "Tell Your World"],
        "date": "2020-09-30",
        "bpm": 150,
        "master": 26,
        "append": False,
        "category": "vocaloid",
        "artist": "kz",
        "newly_written": False,
    },
    {
        "id": 2,
        "title": "シャンティ",
        "cn": "SHANTI",
        "aliases": ["香缇", "shanti", "SHANTI"],
        "date": "2023-05-18",
        "bpm": 133,
        "master": 30,
        "append": True,
        "category": "vocaloid",
        "artist": "wotaku",
        "newly_written": False,
    },
    {
        "id": 3,
        "title": "群青讃歌",
        "cn": "群青赞歌",
        "aliases": ["群青", "一周年曲", "gunjo sanka"],
        "date": "2021-09-30",
        "bpm": 165,
        "master": 29,
        "append": False,
        "category": "theme_song",
        "artist": "Eve",
        "newly_written": True,
    },
    {
        "id": 4,
        "title": "初音ミクの消失",
        "cn": "初音未来的消失",
        "aliases": ["消失", "The Disappearance of Hatsune Miku", "shoushitu", "shoushi"],
        "date": "2020-09-30",
        "bpm": 240,
        "master": 33,
        "append": True,
        "category": "vocaloid",
        "artist": "cosMo@暴走P",
        "newly_written": False,
    },
    {
        "id": 5,
        "title": "初音ミクの激唱",
        "cn": "初音未来的激唱",
        "aliases": ["激唱", "The Intense Voice of Hatsune Miku", "jichang"],
        "date": "2021-03-30",
        "bpm": 200,
        "master": 33,
        "append": True,
        "category": "vocaloid",
        "artist": "cosMo@暴走P",
        "newly_written": False,
    },
    {
        "id": 6,
        "title": "トンデモワンダーズ",
        "cn": "离奇物语",
        "aliases": ["tdm", "离奇", "万达", "Tondemo Wonders", "离奇万达"],
        "date": "2021-06-19",
        "bpm": 195,
        "master": 32,
        "append": False,
        "category": "unit",
        "artist": "sasakure.UK",
        "newly_written": True,
    },
    {
        "id": 7,
        "title": "Potatoになっていく",
        "cn": "变成马铃薯",
        "aliases": ["土豆", "马铃薯", "Becoming Potatoes", "potato", "potatoni"],
        "date": "2020-09-30",
        "bpm": 160,
        "master": 28,
        "append": False,
        "category": "unit",
        "artist": "Neru",
        "newly_written": True,
    },
]


class TestSongMatcher:
    @pytest.fixture
    def matcher(self):
        return SongMatcher(SAMPLE_SONGS, always_match=True)

    def test_exact_match_original_title(self, matcher):
        res = matcher.find("Tell Your World")
        assert res is not None
        assert res["id"] == 1

        res2 = matcher.find("群青讃歌")
        assert res2 is not None
        assert res2["id"] == 3

    def test_exact_match_cn_title(self, matcher):
        res = matcher.find("告诉你的世界")
        assert res is not None
        assert res["id"] == 1

        res2 = matcher.find("变成马铃薯")
        assert res2 is not None
        assert res2["id"] == 7

    def test_exact_match_alias(self, matcher):
        res = matcher.find("tyw")
        assert res is not None
        assert res["id"] == 1

        res2 = matcher.find("土豆")
        assert res2 is not None
        assert res2["id"] == 7

    def test_tell_your_word_matches_tell_your_world(self, matcher):
        # 核心场景：玩家输入 "Tell your word"，不能匹配到 "shanti"，必须准确匹配到 "Tell Your World"
        res = matcher.find("Tell your word")
        assert res is not None
        assert res["id"] == 1
        assert res["title"] == "Tell Your World"

    def test_prefix_matches_unique_song(self, matcher):
        res = matcher.find("Tell Your")
        assert res is not None
        assert res["id"] == 1

    def test_alias_shanti_not_misidentified(self, matcher):
        # 搜索 shanti 应精准匹配到 Shanti，不会被其它混淆
        res = matcher.find("shanti")
        assert res is not None
        assert res["id"] == 2

    def test_tell_your_wrold_typo(self, matcher):
        res = matcher.find("Tell your wrold")
        assert res is not None
        assert res["id"] == 1

    def test_chinese_typo_fuzzy_match(self, matcher):
        # 错别字 / 漏字 / 繁简体测试
        res = matcher.find("初音未来消失")  # 漏了“的”
        assert res is not None
        assert res["id"] == 4

        res2 = matcher.find("告诉你的世")  # 少一个字
        assert res2 is not None
        assert res2["id"] == 1

    def test_japanese_typo_fuzzy_match(self, matcher):
        res = matcher.find("初音ミクの激")  # 少字
        assert res is not None
        assert res["id"] == 5

    def test_alias_fuzzy_match(self, matcher):
        res = matcher.find("离奇万事")  # 别名 "离奇万达" 的错字
        assert res is not None
        assert res["id"] == 6

    def test_shanti_match(self, matcher):
        res = matcher.find("shanti")
        assert res is not None
        assert res["id"] == 2

        res2 = matcher.find("香提")  # 错字匹配 香缇
        assert res2 is not None
        assert res2["id"] == 2


class TestWordleGameDuplicateGuess:
    def test_duplicate_guess_not_counted(self):
        answer = SAMPLE_SONGS[0]  # Tell Your World
        game = WordleGame(answer, server="jp", max_guesses=8)

        # 第一次猜测：土豆
        res1 = game.guess(SAMPLE_SONGS[6], player_id="123", player_name="TestUser")
        assert res1["result"] == "ongoing"
        assert game.guess_count == 1
        assert SAMPLE_SONGS[6]["id"] in game.guess_ids

        # 重复猜测检查
        assert game.is_already_guessed(SAMPLE_SONGS[6]["id"]) is True
        assert game.is_already_guessed(SAMPLE_SONGS[0]["id"]) is False
