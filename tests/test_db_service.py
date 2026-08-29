"""数据库服务单元测试：战绩记录、排行榜查询、官方账号绑定迁移。"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.db_service import (
    DEFAULT_PLATFORM_NAME,
    OFFICIAL_PLATFORM_NAME,
    DBService,
)


@pytest.fixture
def db(tmp_path):
    db = DBService(str(tmp_path / "test.db"))

    async def _init():
        await db.init_db()

    asyncio.run(_init())
    return db


def run(coro):
    return asyncio.run(coro)


class TestRecordResult:
    def test_record_and_ranking(self, db):
        run(db.record_result("grp1", "10001", "Alice", DEFAULT_PLATFORM_NAME, 4, True, 2))
        run(db.record_result("grp1", "10002", "Bob", DEFAULT_PLATFORM_NAME, 2, True, 5))
        run(db.record_result("grp2", "10001", "Alice", DEFAULT_PLATFORM_NAME, 3, True, 3))

        global_rows = run(db.get_global_ranking(10))
        assert len(global_rows) == 2
        assert global_rows[0]["user_id"] == "10001"  # 7 分
        assert global_rows[0]["score"] == 7
        assert global_rows[0]["wins"] == 2
        assert global_rows[0]["best_guesses"] == 2

        group_rows = run(db.get_group_ranking("grp1", 10))
        assert len(group_rows) == 2
        assert group_rows[0]["user_id"] == "10001"
        assert group_rows[0]["score"] == 4

        summary = run(db.get_user_summary("grp2", "10001", DEFAULT_PLATFORM_NAME))
        assert summary["score"] == 7
        assert summary["global_rank"] == 1
        assert summary["group"]["score"] == 3

    def test_best_guesses_updates(self, db):
        run(db.record_result("g", "u1", "A", DEFAULT_PLATFORM_NAME, 2, True, 6))
        run(db.record_result("g", "u1", "A", DEFAULT_PLATFORM_NAME, 4, True, 2))
        summary = run(db.get_user_summary("g", "u1", DEFAULT_PLATFORM_NAME))
        assert summary["best_guesses"] == 2


class TestBinding:
    def test_bind_and_resolve(self, db):
        # 官方机先攒分
        run(db.record_result("g", "a" * 32, "OfficialBot", OFFICIAL_PLATFORM_NAME, 4, True, 2))
        # 绑定到普通 QQ
        ok = run(db.bind_official_account("a" * 32, "10086"))
        assert ok

        resolved = run(db.resolve_user_id(OFFICIAL_PLATFORM_NAME, "a" * 32))
        assert resolved == "10086"

        summary = run(db.get_user_summary("g", "10086", DEFAULT_PLATFORM_NAME))
        assert summary is not None
        assert summary["score"] == 4  # 分数迁移
        assert summary["wins"] == 1

    def test_double_bind_rejected(self, db):
        assert run(db.bind_official_account("b" * 32, "10086"))
        assert not run(db.bind_official_account("b" * 32, "20001"))

    def test_invalid_target_rejected(self, db):
        assert not run(db.bind_official_account("c" * 32, "abc"))
        assert not run(db.bind_official_account("c" * 32, "123"))  # 太短
        assert not run(db.bind_official_account("", "10086"))

    def test_unbound_official_flag_in_ranking(self, db):
        run(db.record_result("g", "d" * 32, "OfficialBot", OFFICIAL_PLATFORM_NAME, 4, True, 2))
        rows = run(db.get_global_ranking(10))
        assert len(rows) == 1
        assert rows[0]["is_unbound_official"] == 1

        # 绑定后徽章消失
        run(db.bind_official_account("d" * 32, "10086"))
        rows = run(db.get_global_ranking(10))
        assert len(rows) == 1
        assert rows[0]["is_unbound_official"] == 0
        assert rows[0]["user_id"] == "10086"

    def test_merge_group_scores(self, db):
        # 官方机在群 g1 得分，普通号在群 g2 得分，绑定后两组分数并存
        run(db.record_result("g1", "e" * 32, "Bot", OFFICIAL_PLATFORM_NAME, 4, True, 1))
        run(db.record_result("g2", "10086", "User", DEFAULT_PLATFORM_NAME, 2, True, 6))
        run(db.bind_official_account("e" * 32, "10086"))
        g1 = run(db.get_group_ranking("g1", 10))
        g2 = run(db.get_group_ranking("g2", 10))
        assert g1[0]["user_id"] == "10086" and g1[0]["score"] == 4
        assert g2[0]["user_id"] == "10086" and g2[0]["score"] == 2
