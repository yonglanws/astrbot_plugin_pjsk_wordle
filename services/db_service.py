"""积分与账号绑定数据库服务（aiosqlite 异步实现）。

与其他 PJSK 娱乐插件保持一致的 QQ 官方机器人账号绑定体系：
- 普通 QQ 平台: aiocqhttp
- QQ 官方机器人平台: qq_official（QID 为 32 位十六进制）
- 通过 account_bindings 表把官方 QID 绑定到普通 QQ 号，分数自动合并迁移。

本插件表结构：
- user_stats: 总分 / 游戏局数 / 胜场 / 最佳(最少)次数 / 群内分数(JSON)
- account_bindings: 官方账号绑定关系
"""

import json
from datetime import datetime

import aiosqlite

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_pjsk_wordle")

DEFAULT_PLATFORM_NAME = "aiocqhttp"
OFFICIAL_PLATFORM_NAME = "qq_official"


class DBService:
    DEFAULT_PLATFORM_NAME = DEFAULT_PLATFORM_NAME
    OFFICIAL_PLATFORM_NAME = OFFICIAL_PLATFORM_NAME

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _get_conn(self) -> aiosqlite.Connection:
        return aiosqlite.connect(self.db_path)

    # ---------- 初始化 ----------

    async def init_db(self):
        async with self._get_conn() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_stats (
                    user_id TEXT PRIMARY KEY,
                    user_name TEXT,
                    platform_name TEXT NOT NULL DEFAULT 'aiocqhttp',
                    score INTEGER DEFAULT 0,
                    games INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    sum_guesses INTEGER DEFAULT 0,
                    best_guesses INTEGER DEFAULT 0,
                    group_scores TEXT DEFAULT '{}'
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS account_bindings (
                    official_platform TEXT NOT NULL,
                    official_user_id TEXT NOT NULL,
                    qq_user_id TEXT NOT NULL,
                    bound_at TEXT NOT NULL,
                    PRIMARY KEY (official_platform, official_user_id)
                )
                """
            )
            # 旧版本兼容：32 位十六进制 id 视为官方账号
            async with conn.execute(
                "SELECT user_id FROM user_stats WHERE platform_name = ?",
                (self.DEFAULT_PLATFORM_NAME,),
            ) as cursor:
                for (legacy_id,) in await cursor.fetchall():
                    if len(str(legacy_id)) == 32 and all(
                        ch in "0123456789abcdefABCDEF" for ch in str(legacy_id)
                    ):
                        await conn.execute(
                            "UPDATE user_stats SET platform_name = ? WHERE user_id = ?",
                            (self.OFFICIAL_PLATFORM_NAME, legacy_id),
                        )
            await conn.commit()

    # ---------- 身份解析 ----------

    async def resolve_user_id(self, platform_name: str, user_id: str) -> str:
        """把已绑定的官方机器人 QID 解析为普通 QQ 号。"""
        if str(platform_name or DEFAULT_PLATFORM_NAME).strip().lower() != OFFICIAL_PLATFORM_NAME:
            return str(user_id)
        async with (
            self._get_conn() as conn,
            conn.execute(
                "SELECT qq_user_id FROM account_bindings "
                "WHERE official_platform = ? AND official_user_id = ?",
                (OFFICIAL_PLATFORM_NAME, str(user_id)),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        return str(row[0]) if row else str(user_id)

    async def bind_official_account(self, official_user_id: str, qq_user_id: str) -> bool:
        """绑定官方账号，并把其在本插件内的历史战绩合并迁移到目标 QQ 号。"""
        source_id = str(official_user_id).strip()
        target_id = str(qq_user_id).strip()
        if (
            not source_id
            or not target_id.isdigit()
            or not 5 <= len(target_id) <= 12
            or source_id == target_id
        ):
            return False

        async with self._get_conn() as conn:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                async with conn.execute(
                    "SELECT 1 FROM account_bindings WHERE official_platform = ? AND official_user_id = ?",
                    (OFFICIAL_PLATFORM_NAME, source_id),
                ) as cursor:
                    if await cursor.fetchone():
                        await conn.rollback()
                        return False

                columns = "user_name, score, games, wins, sum_guesses, best_guesses, group_scores"
                async with conn.execute(
                    f"SELECT {columns} FROM user_stats WHERE user_id = ?", (source_id,)
                ) as cursor:
                    source_row = await cursor.fetchone()
                async with conn.execute(
                    f"SELECT {columns} FROM user_stats WHERE user_id = ?", (target_id,)
                ) as cursor:
                    target_row = await cursor.fetchone()

                if source_row:
                    if target_row:
                        merged = self._merge_stats(source_row, target_row)
                        await conn.execute(
                            "UPDATE user_stats SET user_name = ?, score = ?, games = ?, wins = ?, "
                            "sum_guesses = ?, best_guesses = ?, group_scores = ? WHERE user_id = ?",
                            (*merged, target_id),
                        )
                    else:
                        await conn.execute(
                            "UPDATE user_stats SET user_id = ?, platform_name = ? WHERE user_id = ?",
                            (target_id, DEFAULT_PLATFORM_NAME, source_id),
                        )

                await conn.execute(
                    "INSERT INTO account_bindings (official_platform, official_user_id, qq_user_id, bound_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        OFFICIAL_PLATFORM_NAME,
                        source_id,
                        target_id,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
                if source_row and target_row:
                    await conn.execute("DELETE FROM user_stats WHERE user_id = ?", (source_id,))
                await conn.commit()
                return True
            except Exception as exc:
                await conn.rollback()
                logger.error(f"[PJSK Wordle] 绑定官方账号失败: {exc}", exc_info=True)
                return False

    @staticmethod
    def _merge_stats(source_row: tuple, target_row: tuple) -> tuple:
        """合并两份战绩：分数/局数/胜场/总次数累加，最佳次数取最小非零。"""
        (
            s_name,
            s_score,
            s_games,
            s_wins,
            s_sum,
            s_best,
            s_groups,
        ) = source_row
        (
            t_name,
            t_score,
            t_games,
            t_wins,
            t_sum,
            t_best,
            t_groups,
        ) = target_row

        def _int(v):
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return 0

        best = min((v for v in (_int(s_best), _int(t_best)) if v > 0), default=0)
        groups = _merge_group_scores(s_groups, t_groups)
        return (
            t_name or s_name,
            _int(s_score) + _int(t_score),
            _int(s_games) + _int(t_games),
            _int(s_wins) + _int(t_wins),
            _int(s_sum) + _int(t_sum),
            best,
            groups,
        )

    # ---------- 战绩更新 ----------

    async def record_result(
        self,
        session_id: str,
        user_id: str,
        user_name: str,
        platform_name: str,
        score: int,
        won: bool,
        guesses: int,
    ):
        """记录一局结束后的玩家战绩（仅获胜者会被调用，won=True）。"""
        async with self._get_conn() as conn:
            conn.row_factory = aiosqlite.Row
            try:
                await conn.execute("BEGIN IMMEDIATE")
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "SELECT * FROM user_stats WHERE user_id = ? AND platform_name = ?",
                        (user_id, platform_name),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        await cursor.execute(
                            "INSERT INTO user_stats (user_id, user_name, platform_name) VALUES (?, ?, ?)",
                            (user_id, user_name, platform_name),
                        )
                        row = {
                            "score": 0,
                            "games": 0,
                            "wins": 0,
                            "sum_guesses": 0,
                            "best_guesses": 0,
                            "group_scores": "{}",
                        }

                    def _int(key):
                        try:
                            return int(row[key] or 0)
                        except (KeyError, TypeError, ValueError):
                            return 0

                    safe_score = max(0, int(score or 0))
                    games = _int("games") + 1
                    wins = _int("wins") + (1 if won else 0)
                    sum_guesses = _int("sum_guesses") + (int(guesses) if won else 0)
                    best = _int("best_guesses")
                    if won and (best == 0 or 0 < int(guesses) < best):
                        best = int(guesses)

                    try:
                        groups = json.loads(row["group_scores"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        groups = {}
                    stat = groups.get(session_id, {})
                    if not isinstance(stat, dict):
                        stat = {}
                    groups[session_id] = {
                        "score": int(stat.get("score", 0) or 0) + safe_score,
                        "games": int(stat.get("games", 0) or 0) + 1,
                        "wins": int(stat.get("wins", 0) or 0) + (1 if won else 0),
                    }

                    await cursor.execute(
                        """
                        UPDATE user_stats SET user_name = ?, score = ?, games = ?, wins = ?,
                                              sum_guesses = ?, best_guesses = ?, group_scores = ?
                        WHERE user_id = ? AND platform_name = ?
                        """,
                        (
                            user_name,
                            _int("score") + safe_score,
                            games,
                            wins,
                            sum_guesses,
                            best,
                            json.dumps(groups, ensure_ascii=False),
                            user_id,
                            platform_name,
                        ),
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    # ---------- 排行榜查询 ----------

    async def get_global_ranking(self, limit: int = 10) -> list[dict]:
        async with self._get_conn() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                """
                SELECT s.user_id, s.user_name, s.score, s.games, s.wins, s.best_guesses, s.platform_name,
                       CASE WHEN s.platform_name = ? AND b.official_user_id IS NULL
                            THEN 1 ELSE 0 END AS is_unbound_official
                FROM user_stats AS s
                LEFT JOIN account_bindings AS b
                       ON b.official_platform = s.platform_name AND b.official_user_id = s.user_id
                WHERE s.score > 0 AND (b.official_user_id IS NULL OR s.platform_name != ?)
                ORDER BY s.score DESC, s.wins DESC
                LIMIT ?
                """,
                (OFFICIAL_PLATFORM_NAME, OFFICIAL_PLATFORM_NAME, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_group_ranking(self, session_id: str, limit: int = 10) -> list[dict]:
        async with self._get_conn() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                """
                SELECT s.user_id, s.user_name, s.score, s.games, s.wins, s.best_guesses, s.group_scores,
                       s.platform_name,
                       CASE WHEN s.platform_name = ? AND b.official_user_id IS NULL
                            THEN 1 ELSE 0 END AS is_unbound_official
                FROM user_stats AS s
                LEFT JOIN account_bindings AS b
                       ON b.official_platform = s.platform_name AND b.official_user_id = s.user_id
                WHERE s.score > 0 AND (b.official_user_id IS NULL OR s.platform_name != ?)
                """,
                (OFFICIAL_PLATFORM_NAME, OFFICIAL_PLATFORM_NAME),
            ) as cursor:
                rows = await cursor.fetchall()

        result: list[dict] = []
        for row in rows:
            try:
                groups = json.loads(row["group_scores"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            stat = groups.get(session_id)
            if not isinstance(stat, dict) or int(stat.get("score", 0) or 0) <= 0:
                continue
            result.append(
                {
                    "user_id": row["user_id"],
                    "user_name": row["user_name"],
                    "score": int(stat.get("score", 0) or 0),
                    "games": int(stat.get("games", 0) or 0),
                    "wins": int(stat.get("wins", 0) or 0),
                    "best_guesses": row["best_guesses"],
                    "is_unbound_official": bool(row["is_unbound_official"]),
                }
            )
        result.sort(key=lambda r: (-r["score"], -r["wins"]))
        return result[:limit]

    async def get_user_summary(
        self, session_id: str, user_id: str, platform_name: str
    ) -> dict | None:
        """查询用户总分 / 全局排名 / 本群数据。"""
        async with self._get_conn() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT * FROM user_stats WHERE user_id = ? AND platform_name = ?",
                (user_id, platform_name),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None

            async with conn.execute(
                "SELECT COUNT(1) + 1 FROM user_stats WHERE score > ?",
                (row["score"] or 0,),
            ) as cursor:
                rank_row = await cursor.fetchone()

            group_stat = {}
            try:
                group_stat = json.loads(row["group_scores"] or "{}").get(session_id, {})
            except (TypeError, json.JSONDecodeError):
                group_stat = {}
            if not isinstance(group_stat, dict):
                group_stat = {}

            return {
                "user_id": row["user_id"],
                "user_name": row["user_name"],
                "score": int(row["score"] or 0),
                "games": int(row["games"] or 0),
                "wins": int(row["wins"] or 0),
                "best_guesses": int(row["best_guesses"] or 0),
                "global_rank": int(rank_row[0]) if rank_row else 1,
                "group": group_stat,
            }


def _merge_group_scores(source_value: str, target_value: str) -> str:
    try:
        source = json.loads(source_value or "{}")
    except (TypeError, json.JSONDecodeError):
        source = {}
    try:
        target = json.loads(target_value or "{}")
    except (TypeError, json.JSONDecodeError):
        target = {}
    if not isinstance(source, dict):
        source = {}
    if not isinstance(target, dict):
        target = {}
    for session_id, stat in source.items():
        if not isinstance(stat, dict):
            continue
        dst = target.get(session_id, {})
        if not isinstance(dst, dict):
            dst = {}

        def _int(v):
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return 0

        target[session_id] = {
            "score": _int(dst.get("score")) + _int(stat.get("score")),
            "games": _int(dst.get("games")) + _int(stat.get("games")),
            "wins": _int(dst.get("wins")) + _int(stat.get("wins")),
        }
    return json.dumps(target, ensure_ascii=False)
