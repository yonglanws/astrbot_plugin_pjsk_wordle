"""数据服务单元测试：派生题库构建（日服/国服两种结构）与持久化。"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.data_service import SERVER_JP, SERVER_SC, DataService

# ---------- 造数据 ----------

JP_MUSICS = [
    {
        "id": 404,
        "title": "blender",
        "creatorArtistId": 234,
        "publishedAt": 1711631100000,  # 2024-03-28 (UTC+9)
        "composer": "x",
    },
    {
        "id": 1,
        "title": "Tell Your World",
        "creatorArtistId": 1,
        "publishedAt": 1560148031000,
    },
]
JP_DIFFS = [
    {"id": 1, "musicId": 404, "musicDifficulty": "master", "playLevel": 27},
    {"id": 2, "musicId": 404, "musicDifficulty": "append", "playLevel": 30},
    {"id": 3, "musicId": 1, "musicDifficulty": "master", "playLevel": 26},
]
JP_VOCALS = [
    # blender: sekai 版为 VBS 角色（characterId 9-12）
    {
        "id": 1,
        "musicId": 404,
        "musicVocalType": "original_song",
        "characters": [{"characterId": 21}],
    },
    {
        "id": 2,
        "musicId": 404,
        "musicVocalType": "sekai",
        "characters": [{"characterId": 9}, {"characterId": 21}],
    },
    # Tell Your World: 只有虚拟歌手
    {
        "id": 3,
        "musicId": 1,
        "musicVocalType": "original_song",
        "characters": [{"characterId": 21}],
    },
]
JP_ARTISTS = [
    {"id": 1, "name": "livetune"},
    {"id": 234, "name": "こめだわら×R Sound Design"},
]

SC_MUSICS = [
    {
        "id": 404,
        "title": "blender",
        "publishedAt": 1717737600000,  # 2024-06-07 (UTC+8)
        "infos": [{"creator": "こめだわら×R Sound Design"}],
        "categories": [{"musicCategoryName": "mv"}],
    }
]
SC_DIFFS = [{"id": 1, "musicId": 404, "musicDifficulty": "master", "playLevel": 26}]
SC_VOCALS = [
    {
        "id": 1,
        "musicId": 404,
        "musicVocalType": "sekai",
        "characters": [{"characterId": 17}],  # n25
    }
]


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path


def _write_raw(data_dir: Path, server: str, parsed: dict):
    sdir = data_dir / server
    sdir.mkdir(parents=True, exist_ok=True)
    for name, payload in parsed.items():
        (sdir / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class TestBuildDerived:
    def test_jp_derived(self, data_dir):
        svc = DataService(data_dir)
        svc._load_extras_memory()
        parsed = {
            "musics.json": JP_MUSICS,
            "musicDifficulties.json": JP_DIFFS,
            "musicVocals.json": JP_VOCALS,
            "musicArtists.json": JP_ARTISTS,
        }
        songs = svc._build_derived(SERVER_JP, parsed)
        by_id = {s["id"]: s for s in songs}

        blender = by_id[404]
        assert blender["title"] == "blender"
        assert blender["artist"] == "こめだわら×R Sound Design"
        assert blender["category"] == "Vivid BAD SQUAD"
        assert blender["date"] == "2024-03-28"
        assert blender["master"] == 27
        assert blender["append"] is True

        tyw = by_id[1]
        assert tyw["category"] == "虚拟歌手"
        assert tyw["artist"] == "livetune"
        assert tyw["append"] is False

    def test_category_prefers_sekai_vocals(self, data_dir):
        """命ばっかり 案例：VS ver. 含 Leo/need 角色，但 セカイver. 是 25時，必须判 25時。"""
        svc = DataService(data_dir)
        musics = [
            {"id": 352, "title": "命ばっかり", "creatorArtistId": 1, "publishedAt": 1660000000000}
        ]
        vocals = [
            {  # 旧规则会先撞上这里的 Leo/need 角色 3
                "id": 858,
                "musicId": 352,
                "musicVocalType": "virtual_singer",
                "characters": [{"characterId": 3}, {"characterId": 15}],
            },
            {
                "id": 859,
                "musicId": 352,
                "musicVocalType": "sekai",
                "characters": [
                    {"characterId": 25},
                    {"characterId": 17},
                    {"characterId": 18},
                    {"characterId": 19},
                    {"characterId": 20},
                ],
            },
            {
                "id": 894,
                "musicId": 352,
                "musicVocalType": "another_vocal",
                "characters": [{"characterId": 20}],
            },
        ]
        parsed = {
            "musics.json": musics,
            "musicDifficulties.json": [
                {"musicId": 352, "musicDifficulty": "master", "playLevel": 26}
            ],
            "musicVocals.json": vocals,
            "musicArtists.json": [{"id": 1, "name": "まふまふ"}],
        }
        songs = svc._build_derived(SERVER_JP, parsed)
        assert songs[0]["category"] == "25時、ナイトコードで。"

    def test_sc_derived(self, data_dir):
        svc = DataService(data_dir)
        parsed = {
            "musics.json": SC_MUSICS,
            "musicDifficulties.json": SC_DIFFS,
            "musicVocals.json": SC_VOCALS,
        }
        songs = svc._build_derived(SERVER_SC, parsed)
        assert len(songs) == 1
        song = songs[0]
        assert song["artist"] == "こめだわら×R Sound Design"  # 来自内嵌 infos.creator
        assert song["category"] == "25时，Nightcord见。"
        assert song["date"] == "2024-06-07"
        assert song["master"] == 26
        assert song["append"] is False

    def test_cn_translation_applied(self, data_dir):
        svc = DataService(data_dir)
        svc._store_extra("translation", {"title": {"ロキ": "ROKI"}})
        musics = [
            {
                "id": 2,
                "title": "ロキ",
                "creatorArtistId": 1,
                "publishedAt": 1560148031000,
            }
        ]
        parsed = {
            "musics.json": musics,
            "musicDifficulties.json": [],
            "musicVocals.json": [],
            "musicArtists.json": [{"id": 1, "name": "みきとP"}],
        }
        songs = svc._build_derived(SERVER_JP, parsed)
        assert songs[0]["cn"] == "ROKI"

    def test_alias_and_bpm_attached(self, data_dir):
        svc = DataService(data_dir)
        svc._store_extra(
            "alias",
            {"musics": [{"music_id": 404, "title": "blender", "aliases": ["布伦德"]}]},
        )
        svc._store_extra("bpm", {"songs": [{"music_id": 404, "title": "blender", "bpm": 103.0}]})
        parsed = {
            "musics.json": JP_MUSICS,
            "musicDifficulties.json": JP_DIFFS,
            "musicVocals.json": JP_VOCALS,
            "musicArtists.json": JP_ARTISTS,
        }
        songs = svc._build_derived(SERVER_JP, parsed)
        by_id = {s["id"]: s for s in songs}
        assert by_id[404]["aliases"] == ["布伦德"]
        assert by_id[404]["bpm"] == 103.0

    def test_bpm_fallback_by_title(self, data_dir):
        """国服歌曲 id 不在 BPM 表中时，按标题回退匹配。"""
        svc = DataService(data_dir)
        svc._store_extra("bpm", {"songs": [{"music_id": 999, "title": "blender", "bpm": 103.0}]})
        parsed = {
            "musics.json": SC_MUSICS,
            "musicDifficulties.json": SC_DIFFS,
            "musicVocals.json": SC_VOCALS,
        }
        songs = svc._build_derived(SERVER_SC, parsed)
        assert songs[0]["bpm"] == 103.0


class TestPersistence:
    def test_store_and_load_derived(self, data_dir):
        svc = DataService(data_dir)
        parsed = {
            "musics.json": SC_MUSICS,
            "musicDifficulties.json": SC_DIFFS,
            "musicVocals.json": SC_VOCALS,
        }
        svc._store_server(SERVER_SC, parsed, blob_sha="abc1234", via="github")
        assert svc.get_song_count(SERVER_SC) == 1
        assert svc.get_version(SERVER_SC) == "abc1234"
        assert svc.is_ready(SERVER_SC)

        # 新实例从磁盘恢复（生产环境由 start() 完成 meta + derived 加载）
        svc2 = DataService(data_dir)
        svc2._load_meta()
        assert svc2._load_derived(SERVER_SC)
        assert svc2.get_song_count(SERVER_SC) == 1
        assert svc2.get_version(SERVER_SC) == "abc1234"  # 来自 meta.json

    def test_load_extras_memory_restores(self, data_dir):
        svc = DataService(data_dir)
        svc._store_extra("alias", {"musics": [{"music_id": 1, "title": "A", "aliases": ["x"]}]})
        # 手动写盘（_store_extra 已写）
        svc2 = DataService(data_dir)
        svc2._load_extras_memory()
        assert svc2.alias_by_id[1] == ["x"]

    def test_data_version_takes_priority(self, data_dir):
        """题库版本优先使用 current_version.json 中的 dataVersion（如 6.8.0.12）。"""
        svc = DataService(data_dir)
        parsed = {
            "musics.json": SC_MUSICS,
            "musicDifficulties.json": SC_DIFFS,
            "musicVocals.json": SC_VOCALS,
            "current_version.json": {
                "appVersion": "6.8.0",
                "dataVersion": "6.8.0.12",
                "assetVersion": "6.8.0.10",
            },
        }
        svc._store_server(SERVER_SC, parsed, blob_sha="abc1234", via="github")
        assert svc.get_version(SERVER_SC) == "6.8.0.12"
        assert svc.meta["servers"][SERVER_SC]["data_version"] == "6.8.0.12"
        assert svc.meta["servers"][SERVER_SC]["blob_sha"] == "abc1234"

    def test_version_fallback_date(self, data_dir):
        svc = DataService(data_dir)
        parsed = {
            "musics.json": SC_MUSICS,
            "musicDifficulties.json": SC_DIFFS,
            "musicVocals.json": SC_VOCALS,
        }
        svc._store_server(SERVER_SC, parsed, blob_sha=None, via="jsdelivr")
        # 无 sha 时使用日期版本（8 位数字）
        assert svc.get_version(SERVER_SC).isdigit()
