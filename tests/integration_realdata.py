"""真实题库端到端集成测试（需要联网）。

流程：GitHub API 拉取日服/国服必要 JSON（失败自动回退 jsDelivr）→
加载别名/BPM/翻译 → 构建派生题库 → 模拟一局完整 Wordle（8 次猜测）→
渲染各阶段棋盘与排行榜。

直接运行：python tests/integration_realdata.py
"""

import asyncio
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.data_service import SERVER_JP, SERVER_SC, DataService
from services.game_service import (
    GameService,
    WordleGame,
    score_for_guess_count,
)
from services.render_service import RenderService

PLUGIN_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INTEGRATION_DIR = PLUGIN_ROOT / "tests" / "_integration"


async def main():
    INTEGRATION_DIR.mkdir(parents=True, exist_ok=True)
    data_dir = INTEGRATION_DIR / "plugin_data"

    # 1. 拉取真实题库（GitHub API 优先 → jsDelivr 兜底）
    svc = DataService(data_dir)
    await svc.refresh_if_stale(force=True)

    for server in (SERVER_JP, SERVER_SC):
        count = svc.get_song_count(server)
        version = svc.get_version(server)
        assert count > 300, f"{server} 题库歌曲数异常: {count}"
        print(f"[OK] {server} 题库: {count} 首, 版本 {version}")

    # 2. 校验派生数据质量
    for server in (SERVER_JP, SERVER_SC):
        songs = svc.get_songs(server)
        missing_master = [s for s in songs if s["master"] is None]
        missing_date = [s for s in songs if not s["date"]]
        missing_bpm = [s for s in songs if s["bpm"] is None]
        missing_cn = [s for s in songs if not s["cn"]]
        cats = {}
        for s in songs:
            cats[s["category"]] = cats.get(s["category"], 0) + 1
        print(
            f"[{server}] 缺MASTER:{len(missing_master)} 缺日期:{len(missing_date)} "
            f"缺BPM:{len(missing_bpm)} 缺中文名:{len(missing_cn)}"
        )
        print(f"[{server}] 分类分布: {cats}")
        assert len(missing_master) / len(songs) < 0.02, "MASTER 缺失率过高"
        assert len(missing_date) / len(songs) < 0.02, "日期缺失率过高"
        assert len(missing_bpm) / len(songs) < 0.10, "BPM 覆盖率过低"
        assert len(missing_cn) / len(songs) < 0.10, "中文名覆盖率过低"

    # 3. 抽查 blender 的属性正确性
    jp_blender = next((s for s in svc.get_songs(SERVER_JP) if s["title"] == "blender"), None)
    assert jp_blender, "日服题库中找不到 blender"
    assert jp_blender["date"] == "2024-03-28", jp_blender
    assert jp_blender["master"] == 27, jp_blender
    assert jp_blender["category"] == "Vivid BAD SQUAD", jp_blender
    assert abs(jp_blender["bpm"] - 103) < 1, jp_blender
    print(f"[OK] blender: {jp_blender}")

    # 4. 别名匹配抽查（别名库真实数据）
    from services.game_service import SongMatcher

    matcher = SongMatcher(svc.get_songs(SERVER_JP))
    for query, expect_title in [("将手", "テオ"), ("洛基", "ロキ")]:
        hit = matcher.find(query)
        assert hit and hit["title"] == expect_title, (
            f"别名 {query} 应命中 {expect_title}, 实际 {hit and hit['title']}"
        )
        print(f"[OK] 别名 '{query}' -> {hit['title']} ({hit['cn']})")

    # 5. 模拟一局完整游戏（目标 blender，模仿参考图的猜测序列）
    game_service = GameService()
    game_service.update_songs(SERVER_JP, svc.get_songs(SERVER_JP))
    answer = jp_blender
    game = WordleGame(answer, SERVER_JP)

    guess_names = ["Flyway", "彗星ノ銀河", "ハッピーチートデー", "烈火", "blender"]
    renderer = RenderService(PLUGIN_ROOT / "resources", INTEGRATION_DIR)
    version = svc.get_version(SERVER_JP)
    for i, name in enumerate(guess_names, 1):
        song = matcher.find(name)
        assert song, f"猜测 {name} 无法在题库中匹配"
        result = game.guess(song, "10001", "测试玩家")
        finished = None
        if game.is_finished():
            finished = {
                "win": game.won,
                "guess_count": game.guess_count,
                "max": game.max_guesses,
                "answer": answer["cn"],
                "reason": game.end_reason,
            }
        path = renderer.render_board(game.rows, 8, "日服题库", version, finished=finished)
        assert path, f"第 {i} 次猜测后渲染失败"
        print(f"[OK] 猜测 {i}: {name} -> {result['result']} (棋盘: {os.path.basename(path)})")

    assert game.won and game.guess_count == 5
    assert score_for_guess_count(5) == 2
    print(f"[OK] 第 {game.guess_count} 次猜对，得 {score_for_guess_count(game.guess_count)} 分")

    # 6. 渲染排行榜
    rank_rows = [
        {
            "rank": 1,
            "user_id": "10001",
            "user_name": "测试玩家",
            "display_name": "测试玩家",
            "score": 12,
            "wins": 4,
            "best": 2,
            "is_unbound_official": False,
        },
        {
            "rank": 2,
            "user_id": "10002",
            "user_name": "世界计划bury",
            "display_name": "世界计划bury",
            "score": 9,
            "wins": 3,
            "best": 3,
            "is_unbound_official": False,
        },
    ]
    rank_path = renderer.render_ranking(rank_rows, title="PJSK Wordle 排行榜")
    assert rank_path, "排行榜渲染失败"
    print(f"[OK] 排行榜: {os.path.basename(rank_path)}")

    await svc.terminate()
    print("\n=== 集成测试全部通过 ===")


if __name__ == "__main__":
    if INTEGRATION_DIR.exists():
        shutil.rmtree(INTEGRATION_DIR, ignore_errors=True)
    asyncio.run(main())
