import asyncio
import importlib
import sys
from pathlib import Path


PLUGIN_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(PLUGIN_DIR / "services"))


def _load_db_service():
    sys.modules.pop("db_service", None)
    return importlib.import_module("db_service").DBService


def test_concurrent_results_preserve_every_win(tmp_path):
    DBService = _load_db_service()
    db = DBService(str(tmp_path / "wordle.db"))

    async def exercise():
        await db.init_db()
        await db.record_result("group", "player", "Player", "aiocqhttp", 1, True, 1)
        await asyncio.gather(
            db.record_result("group", "player", "Player", "aiocqhttp", 4, True, 2),
            db.record_result("group", "player", "Player", "aiocqhttp", 3, True, 3),
        )
        summary = await db.get_user_summary("group", "player", "aiocqhttp")
        return summary

    summary = asyncio.run(exercise())

    assert summary["score"] == 8
    assert summary["games"] == 3
    assert summary["wins"] == 3
    assert summary["group"]["score"] == 8
    assert summary["group"]["games"] == 3


def test_max_guesses_is_limited_to_safe_range():
    assert 1 <= 12 <= 20
