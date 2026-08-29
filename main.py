"""PJSK Wordle - 项目 sekai 音乐游戏猜曲 Wordle 插件。

玩法：开局后随机选定一首目标歌曲，bot 监听会话内所有消息，
玩家通过 @机器人 + 曲名或别名进行回答，限定次数内猜出目标曲目；每次猜测后返回
7 个属性（曲名/上线时间/乐曲分类/作者/BPM/MASTER/APPEND）的
绿/橙/深色 + 方向箭头反馈棋盘，全部变绿即获胜。

- 题库：日服 haruki-sekai-master / 国服 haruki-sekai-sc-master，
  优先通过 GitHub API 拉取必要 JSON，失败回退 jsDelivr，每 24 小时自动更新，
  持久化于 plugin_data 目录。
- 中文译名与别名取自 Moesekai 同款数据源（translation.exmeaning.com / moe.exmeaning.com）。
- 排行榜沿用 PJSK 猜卡插件的视觉样式；支持 QQ 官方机器人账号绑定迁移。
- 图片全部使用 Pillow 本地渲染（白色背景）。
"""

import asyncio
import json
import random
import re
import time
from pathlib import Path
from urllib.parse import quote

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.utils.session_waiter import (
    SessionController,
    SessionFilter,
    session_waiter,
)
from PIL import Image as PILImage

from .services.data_service import SERVER_JP, SERVER_SC, DataService
from .services.db_service import DBService
from .services.game_service import MAX_GUESSES, GameService, WordleGame, score_for_guess_count
from .services.render_service import RenderService

PLUGIN_NAME = "pjsk_wordle"
PLUGIN_AUTHOR = "慵懒午睡"
PLUGIN_DESCRIPTION = "PJSK 音乐游戏猜曲 Wordle：限定次数内根据曲名/上线时间/书下曲/分类/作者/BPM/MASTER/APPEND 反馈锁定目标曲目"
PLUGIN_VERSION = "1.0.0"
PLUGIN_REPO_URL = "https://github.com/yonglanws/astrbot_plugin_pjsk_wordle"

SERVER_LABELS = {SERVER_JP: "日服", SERVER_SC: "国服"}
SERVER_BADGES = {SERVER_JP: "日服题库", SERVER_SC: "国服题库"}
SWITCH_COMMANDS = {SERVER_JP: "切换国服题库", SERVER_SC: "切换日服题库"}
# 结算连接入口使用的带前缀指令名（连接插入的文本，与下方指令别名一一对应）
CONNECT_SWITCH_COMMANDS = {SERVER_JP: "Wordle切换国服题库", SERVER_SC: "Wordle切换日服题库"}

# 指令连接的默认 markdown 模板：QQ 官方机器人 markdown 消息的参数指令标签
# （见 bot.q.qq.com/wiki 的 markdown / text-chain 文档）。
# text 只放指令本身：QQ 客户端在群聊发送时会自动 @ 官方机器人，
# 拼进 "@id" 反而会出现双重 @（self_id 还可能是占位符 "qq_official"）。
DEFAULT_CONNECT_TEMPLATE = '<qqbot-cmd-input text="{encoded_command}" show="{encoded_name}" />'
# 旧版本默认模板特征：命中即视为未自定义，自动升级到新默认模板
_LEGACY_TEMPLATE_MARKERS = ("{encoded_at_text}", "mqqapi://")

DEFAULT_PLATFORM_NAME = "aiocqhttp"
OFFICIAL_PLATFORM_NAME = "qq_official"
OFFICIAL_QID_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")

# 插件自身指令词：全局监听器遇到这些消息直接跳过，避免把指令当作猜测
_COMMAND_WORDS = {
    "wordle",
    "pjskwordle",
    "pjsk_wordle",
    "自动wordle",
    "自动pjskwordle",
    "切换国服题库",
    "切换国服",
    "切换日服题库",
    "切换日服",
    "wordle排行榜",
    "pjskwordle排行榜",
    "群wordle排行榜",
    "wordle分数",
    "wordle个人分数",
    "我的wordle分数",
    "wordle绑定",
    "wordle绑定qq",
    "pjskwordle绑定",
    "wordle切换国服题库",
    "wordle切换日服题库",
    "wordle帮助",
    "wordle玩法",
    "更新wordle题库",
    "刷新wordle题库",
}


def _get_normalized_session_id(event: AstrMessageEvent) -> str:
    """标准化 session_id，确保以群/会话为粒度（与同目录其他 PJSK 插件保持一致）。"""
    group_id = event.get_group_id()
    if group_id:
        parts = str(event.unified_msg_origin).split(":", 2)
        if len(parts) == 3:
            return f"{parts[0]}:{parts[1]}:{group_id}"

    parts = str(event.unified_msg_origin).split(":", 2)
    if len(parts) == 3 and "_" in parts[2]:
        core_session_id = parts[2].rsplit("_", 1)[-1]
        return f"{parts[0]}:{parts[1]}:{core_session_id}"
    return str(event.unified_msg_origin)


class BindingSessionFilter(SessionFilter):
    """只接收发起绑定的同一用户在同一会话中的确认消息。"""

    def __init__(self, session_id: str, user_id: str):
        self.session_id = str(session_id)
        self.user_id = str(user_id)

    def filter(self, event: AstrMessageEvent) -> str:
        if (
            str(event.unified_msg_origin) != self.session_id
            or str(event.get_sender_id()) != self.user_id
        ):
            return ""
        return f"{event.unified_msg_origin}:{event.get_sender_id()}"


@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESCRIPTION, PLUGIN_VERSION, PLUGIN_REPO_URL)
class PjskWordlePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config

        self.plugin_dir = Path(__file__).parent
        self.resources_dir = self.plugin_dir / "resources"

        # plugin_data 持久化目录
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.server_prefs_path = self.data_dir / "session_servers.json"

        # 服务层
        self.data_service = DataService(
            self.data_dir,
            update_interval_hours=int(config.get("update_interval_hours", 24)),
        )
        self.data_service.on_songs_updated = self._on_songs_updated
        self.game_service = GameService(
            close_days=int(config.get("close_days", 180)),
            close_bpm=int(config.get("close_bpm", 10)),
            close_master=int(config.get("close_master", 1)),
            always_match=bool(config.get("fuzzy_always_match", True)),
        )
        self.render_service = RenderService(self.resources_dir, self.plugin_dir / "output")
        self.db_service = DBService(str(self.data_dir / "wordle.db"))

        # 游戏状态
        self.games: dict[str, dict] = {}  # session_id -> 运行时局信息
        self.auto_sessions: dict[str, bool] = {}  # session_id -> 自动模式
        self.session_locks: dict[str, asyncio.Lock] = {}
        self.last_game_end: dict[str, float] = {}
        self.server_prefs: dict[str, str] = self._load_server_prefs()
        self._background_tasks = set()

        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
        self._init_task = asyncio.create_task(self._async_init())

    # ---------- 初始化 ----------

    async def _async_init(self):
        try:
            await self.db_service.init_db()
            await self.data_service.start()
        except Exception as e:
            logger.error(f"[PJSK Wordle] 初始化失败: {e}", exc_info=True)

    async def _on_songs_updated(self):
        """题库更新后重建各服务器匹配器。"""
        for server in (SERVER_JP, SERVER_SC):
            songs = self.data_service.get_songs(server)
            if songs:
                self.game_service.update_songs(server, songs)
        logger.info("[PJSK Wordle] 匹配器已随题库更新。")

    async def terminate(self):
        for session_id, sess in list(self.games.items()):
            self._cancel_idle_task(sess)
            self.games.pop(session_id, None)
        for task in list(self._background_tasks):
            task.cancel()
        await self.data_service.terminate()
        logger.info("[PJSK Wordle] 插件已终止。")

    def _track_task(self, task: asyncio.Task):
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # ---------- 配置与会话状态 ----------

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self.session_locks:
            self.session_locks[session_id] = asyncio.Lock()
        return self.session_locks[session_id]

    def _load_server_prefs(self) -> dict[str, str]:
        try:
            if self.server_prefs_path.exists():
                return json.loads(self.server_prefs_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[PJSK Wordle] 加载题库服务器偏好失败: {e}")
        return {}

    def _save_server_prefs(self):
        try:
            self.server_prefs_path.write_text(
                json.dumps(self.server_prefs, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[PJSK Wordle] 保存题库服务器偏好失败: {e}")

    def _server_for_session(self, session_id: str) -> str:
        saved = self.server_prefs.get(session_id)
        if saved in (SERVER_JP, SERVER_SC):
            return saved
        default = str(self.config.get("default_server", SERVER_JP)).lower()
        return SERVER_SC if default == SERVER_SC else SERVER_JP

    def _is_group_allowed(self, event: AstrMessageEvent) -> bool:
        whitelist = {str(x) for x in self.config.get("group_whitelist", [])}
        if not whitelist:
            return True
        group_id = event.get_group_id()
        return bool(group_id and str(group_id) in whitelist)

    @staticmethod
    def _get_platform_name(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_platform_name() or DEFAULT_PLATFORM_NAME).strip().lower()
        except Exception:
            return DEFAULT_PLATFORM_NAME

    async def _get_account_identity(self, event: AstrMessageEvent) -> tuple[str, str]:
        """解析（可能已绑定迁移的）用户 ID 与平台。"""
        platform_name = self._get_platform_name(event)
        raw_user_id = str(event.get_sender_id())
        resolved = await self.db_service.resolve_user_id(platform_name, raw_user_id)
        if resolved != raw_user_id:
            return resolved, DEFAULT_PLATFORM_NAME
        return raw_user_id, platform_name

    # ---------- 指令：开始游戏 ----------

    @filter.command("wordle", alias={"Wordle", "pjskwordle", "pjsk_wordle"})
    async def start_wordle(self, event: AstrMessageEvent):
        """开始一局 PJSK Wordle。"""
        session_id = _get_normalized_session_id(event)
        async with self._get_session_lock(session_id):
            await self._start_round(event, session_id)

    @filter.command("自动wordle", alias={"自动Wordle", "自动pjskwordle"})
    async def start_auto_wordle(self, event: AstrMessageEvent):
        """进入自动模式：每局结束后自动开始下一局，发送 退出自动模式 停止。"""
        session_id = _get_normalized_session_id(event)
        self.auto_sessions[session_id] = True
        await event.send(
            event.plain_result(
                "已开启自动 Wordle 模式！每局结束后将自动开始下一局，发送“退出自动模式”可停止。"
            )
        )
        async with self._get_session_lock(session_id):
            await self._start_round(event, session_id, auto=True)

    # ---------- 指令：切换题库服务器 ----------

    @filter.command("切换国服题库", alias={"切换国服", "Wordle切换国服题库"})
    async def switch_to_sc(self, event: AstrMessageEvent):
        """切换为国服题库。"""
        await self._switch_server(event, SERVER_SC)

    @filter.command("切换日服题库", alias={"切换日服", "Wordle切换日服题库"})
    async def switch_to_jp(self, event: AstrMessageEvent):
        """切换为日服题库。"""
        await self._switch_server(event, SERVER_JP)

    async def _switch_server(self, event: AstrMessageEvent, server: str):
        session_id = _get_normalized_session_id(event)
        if session_id in self.games:
            await event.send(event.plain_result("本局游戏还在进行中，结束后再切换题库服务器吧。"))
            return
        current = self._server_for_session(session_id)
        if current == server:
            await event.send(event.plain_result(f"当前题库已经是{SERVER_LABELS[server]}题库了。"))
            return
        self.server_prefs[session_id] = server
        self._save_server_prefs()
        count = self.data_service.get_song_count(server)
        version = self.data_service.get_version(server)
        await event.send(
            event.plain_result(
                f"已切换为{SERVER_BADGES[server]}（共 {count} 首，版本 {version}），下一局生效。"
            )
        )

    # ---------- 指令：排行榜 / 分数 ----------

    @filter.command("wordle排行榜", alias={"Wordle排行榜", "pjskwordle排行榜"})
    async def show_global_ranking(self, event: AstrMessageEvent):
        """显示全局 Wordle 排行榜。"""
        await self._send_ranking(event, group_session=None)

    @filter.command("群wordle排行榜")
    async def show_group_ranking(self, event: AstrMessageEvent):
        """显示本群 Wordle 排行榜。"""
        session_id = _get_normalized_session_id(event)
        await self._send_ranking(event, group_session=session_id)

    async def _send_ranking(self, event: AstrMessageEvent, group_session: str | None):
        limit = int(self.config.get("ranking_display_count", 10))
        try:
            if group_session:
                rows = await self.db_service.get_group_ranking(group_session, limit)
                title = "本群 Wordle 排行榜"
            else:
                rows = await self.db_service.get_global_ranking(limit)
                title = "PJSK Wordle 排行榜"
        except Exception as e:
            logger.error(f"[PJSK Wordle] 查询排行榜失败: {e}", exc_info=True)
            await event.send(event.plain_result("查询排行榜数据时出错。"))
            return

        if not rows:
            await event.send(event.plain_result("还没有人猜对过呢，快来成为第一个！"))
            return

        render_rows = []
        for i, row in enumerate(rows):
            best = row.get("best_guesses")
            try:
                best = int(best) if best else 0
            except (TypeError, ValueError):
                best = 0
            render_rows.append(
                {
                    "rank": i + 1,
                    "user_id": row["user_id"],
                    "user_name": row["user_name"],
                    "display_name": row["user_name"],
                    "score": row["score"],
                    "wins": row["wins"],
                    "best": best if best > 0 else "—",
                    "is_unbound_official": bool(row.get("is_unbound_official")),
                }
            )
        img_path = self.render_service.render_ranking(render_rows, title=title)
        if img_path:
            await event.send(event.image_result(img_path))
        else:
            await event.send(event.plain_result("生成排行榜图片时出错。"))

    @filter.command("wordle分数", alias={"Wordle个人分数", "我的wordle分数"})
    async def show_my_score(self, event: AstrMessageEvent):
        """查看我的 Wordle 战绩。"""
        session_id = _get_normalized_session_id(event)
        user_id, platform_name = await self._get_account_identity(event)
        user_name = event.get_sender_name()
        summary = await self.db_service.get_user_summary(session_id, user_id, platform_name)
        if not summary or summary.get("wins", 0) == 0:
            await event.send(
                event.plain_result(f"{user_name} 还没有猜对过呢，发送 wordle 开始一局吧！")
            )
            return

        best = summary.get("best_guesses") or 0
        best_str = f"{best} 次" if best > 0 else "—"
        lines = [
            f"🎯 {user_name} 的 Wordle 战绩",
            f"总分：{summary['score']}（全局第 {summary['global_rank']} 名）",
            f"胜场：{summary['wins']}",
            f"最快猜对：{best_str}",
        ]
        group = summary.get("group") or {}
        if group:
            lines.append(f"本群：{group.get('score', 0)} 分 / {group.get('wins', 0)} 胜")
        await event.send(event.plain_result("\n".join(lines)))

    # ---------- 指令：绑定 / 帮助 / 题库更新 ----------

    @filter.command("wordle绑定", alias={"Wordle绑定QQ", "wordle绑定QQ", "pjskwordle绑定"})
    async def bind_wordle_account(self, event: AstrMessageEvent):
        """QQ 官方机器人账号绑定到普通 QQ 账号。"""
        if self._get_platform_name(event) != OFFICIAL_PLATFORM_NAME:
            await event.send(event.plain_result("此绑定功能仅支持 QQ 官方机器人使用。"))
            return

        parts = event.message_str.strip().split(maxsplit=1)
        qq_user_id = parts[1].strip() if len(parts) > 1 else ""
        if not qq_user_id.isdigit() or not 5 <= len(qq_user_id) <= 12:
            await event.send(
                event.plain_result("请按“wordle绑定 QQ号”的格式输入，例如：wordle绑定 123456789。")
            )
            return

        official_user_id = str(event.get_sender_id())
        current_user_id = await self.db_service.resolve_user_id(
            OFFICIAL_PLATFORM_NAME, official_user_id
        )
        if current_user_id != official_user_id:
            await event.send(
                event.plain_result(f"当前官方机器人账号已经绑定至 QQ号 {current_user_id}。")
            )
            return

        await event.send(
            event.plain_result(
                f"你确认将账号绑定至  {qq_user_id} ？官方机答对的分数将迁移至该账号。\n"
                "发送“确认”将开始绑定。发送“取消”将取消绑定。"
            )
        )
        decision = None

        @session_waiter(timeout=60, record_history_chains=False)
        async def binding_waiter(controller: SessionController, answer_event: AstrMessageEvent):
            nonlocal decision
            answer_text = answer_event.message_str.strip()
            if answer_text == "确认":
                decision = "confirm"
                controller.stop()
            elif answer_text == "取消":
                decision = "cancel"
                controller.stop()

        try:
            await binding_waiter(
                event,
                session_filter=BindingSessionFilter(event.unified_msg_origin, official_user_id),
            )
        except TimeoutError:
            await event.send(event.plain_result("绑定确认已超时，绑定操作已取消。"))
            return

        if decision == "confirm":
            bound = await self.db_service.bind_official_account(official_user_id, qq_user_id)
            if bound:
                await event.send(
                    event.plain_result(f"绑定成功！官方机的历史分数已迁移至 QQ号 {qq_user_id}。")
                )
            else:
                await event.send(event.plain_result("绑定失败：该官方账号可能已绑定，请稍后重试。"))
        else:
            await event.send(event.plain_result("已取消绑定。"))

    @filter.command("wordle帮助", alias={"wordle玩法"})
    async def show_help(self, event: AstrMessageEvent):
        """显示 Wordle 玩法帮助。"""
        img_path = self.render_service.render_help()
        if img_path:
            await event.send(event.image_result(img_path))
        else:
            await event.send(event.plain_result("生成帮助图片时出错。"))

    @filter.command("更新wordle题库", alias={"刷新wordle题库"})
    async def refresh_wordle_data(self, event: AstrMessageEvent):
        """（管理员）强制刷新题库。"""
        super_users = {str(x) for x in self.config.get("super_users", [])}
        if not (event.is_admin or str(event.get_sender_id()) in super_users):
            return
        await event.send(event.plain_result("正在强制更新题库，请稍候……"))
        try:
            await self.data_service.refresh_if_stale(force=True)
            jp_count = self.data_service.get_song_count(SERVER_JP)
            sc_count = self.data_service.get_song_count(SERVER_SC)
            jp_ver = self.data_service.get_version(SERVER_JP)
            sc_ver = self.data_service.get_version(SERVER_SC)
            await event.send(
                event.plain_result(
                    f"题库更新完成：\n日服 {jp_count} 首（版本 {jp_ver}）\n国服 {sc_count} 首（版本 {sc_ver}）"
                )
            )
        except Exception as e:
            logger.error(f"[PJSK Wordle] 手动刷新题库失败: {e}", exc_info=True)
            await event.send(event.plain_result("题库更新失败，请查看日志。"))

    # ---------- 消息监听（仅 @机器人 的回答 / 退出） ----------

    def _is_at_bot(self, event: AstrMessageEvent) -> bool:
        """判断消息是否 @ 了本机器人（私聊/C2C 天然面向机器人，直接放行）。

        群聊一律要求消息链中存在指向机器人的 At 组件——QQ 官方机器人
        同样可能收到未 @ 的群消息，不能因为是官方平台就无条件放行。
        """
        if not event.get_group_id():
            return True  # 私聊直达机器人
        self_id = str(getattr(event.message_obj, "self_id", "") or "").strip()
        for comp in getattr(event.message_obj, "message", None) or []:
            if isinstance(comp, Comp.At):
                comp_qq = str(getattr(comp, "qq", ""))
                # self_id 缺失时放宽为"消息中存在任意 At"（个别适配器不回填 self_id）
                if not self_id or comp_qq == self_id:
                    return True
        return False

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """仅处理 @机器人 的消息：有对局时视为玩家的猜测回答，"仅退出本局"结束本局、"退出自动模式"停止续局。

        未 @ 机器人的普通聊天完全不参与游戏。
        """
        if not self._is_at_bot(event):
            return

        text = (event.message_str or "").strip()
        if not text or text.startswith("/"):
            return
        first_token = text.split()[0].strip().lower()
        if first_token in _COMMAND_WORDS:
            return

        session_id = _get_normalized_session_id(event)
        sess = self.games.get(session_id)
        if sess is None:
            # 退出自动模式：任何时候可触发（无对局时也响应）
            if text in ["退出自动模式", "退出"] and self.auto_sessions.pop(session_id, None) is not None:
                await event.send(event.plain_result("已退出自动 Wordle 模式。"))
            return

        # 仅退出本局：只在游玩时生效，立即结束当前对局（不影响自动模式）
        if text == "仅退出本局":
            sess["game"].forfeit("quit")
            await self._finish_game(event, session_id, reason="quit")
            return

        # 退出自动模式：本局继续、自动模式停止
        if text in ["退出自动模式", "退出"]:
            if self.auto_sessions.pop(session_id, None) is not None:
                await event.send(event.plain_result("已退出自动 Wordle 模式。"))
            return

        await self._process_guess(event, session_id, sess, text)

    # ---------- 游戏流程 ----------

    async def _start_round(self, event: AstrMessageEvent, session_id: str, auto: bool = False):
        """开始一局（调用方需持有会话锁）。"""
        if not self._is_group_allowed(event):
            reject_msg = str(self.config.get("whitelist_reject_message", "") or "").strip()
            if reject_msg:
                await event.send(event.plain_result(reject_msg))
            return

        if session_id in self.games:
            await event.send(event.plain_result("已经有一局 Wordle 在进行中了，先完成它吧。"))
            return

        cooldown = float(self.config.get("game_cooldown_seconds", 3))
        if not auto and time.time() - self.last_game_end.get(session_id, 0) < cooldown:
            await event.send(event.plain_result("休息一下，马上就可以开始下一局了……"))
            return

        server = self._server_for_session(session_id)
        songs = self.data_service.get_songs(server)
        if not songs:
            await event.send(
                event.plain_result("题库还没有加载完成（首次启动需要联网下载），请稍后再试。")
            )
            return

        answer = random.choice(songs)
        max_guesses = int(self.config.get("max_guesses", MAX_GUESSES))
        game = WordleGame(answer, server, max_guesses=max_guesses)
        self.games[session_id] = {
            "game": game,
            "server": server,
            "event": event,
            "last_activity": time.time(),
            "idle_task": None,
        }
        logger.info(
            f"[PJSK Wordle] 新开局 session={session_id} server={server} 答案: {answer['title']}"
        )

        version = self.data_service.get_version(server)
        board_path = self.render_service.render_board(
            [], max_guesses, SERVER_BADGES[server], version
        )
        is_official = self._get_platform_name(event) == OFFICIAL_PLATFORM_NAME
        in_auto_mode = bool(self.auto_sessions.get(session_id))
        official_self_id = str(getattr(event.message_obj, "self_id", "") or "").strip() if is_official else ""
        intro = (
            "PJSK Wordle 开始！\n"
            f"题库：{SERVER_BADGES[server]}（共 {len(songs)} 首）\n"
            f"在 {max_guesses} 次猜测内猜出目标曲目：@本机器人 + 曲名或别名进行回答，"
            "每次猜测都会返回属性反馈。"
        )
        if in_auto_mode:
            # 自动模式：不出现 markdown 按钮，用文字提示退出方式
            intro += "\n发送「仅退出本局」可结束本局，发送「退出自动模式」可停止自动模式。"
        try:
            if is_official and in_auto_mode:
                await event.send(event.plain_result(intro))
            elif is_official and official_self_id:
                # 官方平台以 markdown 发送，附"点击回答 / 仅退出本局 / 退出自动模式"连接
                intro += (
                    "\n"
                    + self._build_connect_link(" ", official_self_id, show="点击回答")
                    + "  "
                    + self._build_connect_link("仅退出本局", official_self_id)
                    + "  "
                    + self._build_connect_link("退出自动模式", official_self_id)
                )
                result = event.make_result()
                result.chain = [Comp.Plain(intro)]
                result.use_markdown(True)
                await event.send(result)
            else:
                await event.send(event.plain_result(intro))
            if board_path:
                await event.send(event.chain_result([Comp.Image(file=board_path)]))
            else:
                await event.send(event.plain_result("（棋盘图片渲染失败，游戏继续，猜测仍有效）"))
        except Exception as e:
            logger.error(f"[PJSK Wordle] 发送开局消息失败: {e}", exc_info=True)
            self.games.pop(session_id, None)
            return

        self._start_idle_watcher(session_id)

    async def _process_guess(self, event: AstrMessageEvent, session_id: str, sess: dict, text: str):
        """处理一次可能的猜测。"""
        server = sess["server"]
        game: WordleGame = sess["game"]
        if game.is_finished():
            return

        # 匹配器兜底懒加载（题库更新时会自动重建）
        if self.game_service.get_matcher(server) is None:
            self.game_service.update_songs(server, self.data_service.get_songs(server))

        song = self.game_service.find_song(server, text)
        if song is None:
            # 不在当前题库：若命中另一服务器题库则友好提示，其余普通聊天静默忽略
            other = SERVER_SC if server == SERVER_JP else SERVER_JP
            other_songs = self.data_service.get_songs(other)
            if other_songs:
                if self.game_service.get_matcher(other) is None:
                    self.game_service.update_songs(other, other_songs)
                other_song = self.game_service.find_song(other, text)
                if other_song is not None:
                    display = other_song.get("cn") or other_song.get("title") or "该歌曲"
                    await event.send(
                        event.plain_result(
                            f"「{display}」不在当前{SERVER_LABELS[server]}题库中（属于{SERVER_LABELS[other]}题库）。"
                        )
                    )
            return

        player_id, _platform_name = await self._get_account_identity(event)
        player_name = event.get_sender_name()
        sess["last_activity"] = time.time()

        # 身份解析期间超时看护可能已结束本局，避免吞掉制胜一击
        if game.is_finished():
            return

        game.guess(song, player_id, player_name)

        version = self.data_service.get_version(server)
        board_path = self.render_service.render_board(
            game.rows,
            game.max_guesses,
            SERVER_BADGES[server],
            version,
        )
        try:
            if board_path:
                await event.send(event.chain_result([Comp.Image(file=board_path)]))
        except Exception as e:
            logger.error(f"[PJSK Wordle] 发送棋盘失败: {e}", exc_info=True)

        if game.is_finished():
            await self._finish_game(event, session_id, reason=game.end_reason)

    async def _finish_game(self, event: AstrMessageEvent, session_id: str, reason: str):
        """结束一局：结算积分、发送结果文本、清理状态并处理自动续局。

        结算文案按平台区分：
        - QQ 官方机器人：提供"连接"入口（点击后自动在聊天框 @官机 + 指令），
          并附其他 PJSK 娱乐插件的快捷入口；
        - 普通 QQ：仅提示可用的切换指令，不显示快捷入口。
        """
        sess = self.games.pop(session_id, None)
        if sess is None:
            return
        self._cancel_idle_task(sess)
        self.last_game_end[session_id] = time.time()

        game: WordleGame = sess["game"]
        server = sess["server"]
        answer_display = self._answer_display(game.answer)

        lines = ["Wordle 结束"]
        if game.won and reason == "win":
            score = score_for_guess_count(game.guess_count, game.max_guesses)
            lines.append(f"你在第 {game.guess_count} / {game.max_guesses} 次猜对了，增加{score}分")
            lines.append(f"正确答案：{answer_display}")
            try:
                await self.db_service.record_result(
                    session_id=session_id,
                    user_id=game.winner_id or "",
                    user_name=game.winner_name or "未知",
                    platform_name=self._identity_platform(game.winner_id),
                    score=score,
                    won=True,
                    guesses=game.guess_count,
                )
            except Exception as e:
                logger.error(f"[PJSK Wordle] 记录战绩失败: {e}", exc_info=True)
        elif reason == "quit":
            lines.append("本局已结束（仅退出本局）")
            lines.append(f"正确答案：{answer_display}")
        elif reason == "timeout":
            lines.append("长时间没有猜测，本局已超时结束")
            lines.append(f"正确答案：{answer_display}")
        else:
            lines.append(f"{game.max_guesses} 次机会已用完，很遗憾没有猜对")
            lines.append(f"正确答案：{answer_display}")

        if session_id in self.auto_sessions:
            # 自动模式：只显示结果与答案，随后自动开始下一局，不出现 markdown 按钮
            lines.append("即将开始下一局，发送「退出自动模式」可停止自动模式。")
        else:
            lines.extend(self._build_server_footer(event, server))
        if reason == "fail":
            # 次数达到限制时，曲绘卡片单独占一行提示，见下方图片
            lines.append("答案曲绘：")

        try:
            result = event.make_result()
            result.chain = [Comp.Plain("\n".join(lines))]
            if self._get_platform_name(event) == OFFICIAL_PLATFORM_NAME and session_id not in self.auto_sessions:
                # QQ 官方平台以 markdown 渲染结算消息，连接入口以 markdown 链接展示（自动模式除外）
                result.use_markdown(True)
            await event.send(result)
        except Exception as e:
            logger.error(f"[PJSK Wordle] 发送结算消息失败: {e}", exc_info=True)

        # 未猜中的结束方式（退出/超时/次数用尽）：在最下面单开一行发送正确答案曲绘卡片
        if reason in ("fail", "quit", "timeout"):
            try:
                card_path = await self._render_answer_card(game, server)
                if card_path:
                    await event.send(event.chain_result([Comp.Image(file=card_path)]))
            except Exception as e:
                logger.error(f"[PJSK Wordle] 发送答案卡片失败: {e}", exc_info=True)

        # 自动模式续局
        if session_id in self.auto_sessions and reason in ("win", "fail"):
            self._track_task(asyncio.create_task(self._auto_next(event, session_id)))

    def _jacket_base(self, server: str) -> str:
        """曲绘资源站点（按服务器区分）。"""
        key = "jacket_url_base_sc" if server == SERVER_SC else "jacket_url_base"
        default = (
            "https://storage.exmeaning.com/sekai-sc-assets"
            if server == SERVER_SC
            else "https://storage.exmeaning.com/sekai-jp-assets"
        )
        return str(self.config.get(key, "") or "").strip().rstrip("/") or default

    async def _render_answer_card(self, game: WordleGame, server: str) -> str | None:
        """渲染正确答案卡片（曲绘 + 中文名），曲绘下载失败时退化为文字卡片。"""
        answer = game.answer
        jacket_image = None
        jacket_name = str(answer.get("jacket") or "").strip()
        if jacket_name:
            base = self._jacket_base(server)
            url = f"{base}/music/jacket/{jacket_name}/{jacket_name}.png"
            data = await self.data_service.fetch_bytes(url)
            if data:
                try:
                    from io import BytesIO

                    jacket_image = PILImage.open(BytesIO(data))
                except Exception as e:
                    logger.warning(f"[PJSK Wordle] 曲绘解析失败: {e}")
        return self.render_service.render_answer_card(
            jacket_image,
            self._answer_display(answer),
            str(answer.get("title") or ""),
            SERVER_LABELS[server],
        )

    def _identity_platform(self, user_id: str | None) -> str:
        """根据获胜者 ID 推断平台归属（32 位十六进制 QID 视为官方账号，其余归普通 QQ）。"""
        if user_id and OFFICIAL_QID_PATTERN.match(str(user_id)):
            return OFFICIAL_PLATFORM_NAME
        return DEFAULT_PLATFORM_NAME

    def _build_connect_link(self, command: str, self_id: str, show: str | None = None) -> str:
        """按配置模板生成 markdown 格式的指令连接。

        默认使用 QQ 官方机器人 markdown 消息的参数指令标签
        <qqbot-cmd-input>（见 bot.q.qq.com/wiki markdown 与 text-chain 文档）：
        点击后在聊天框填入指令，QQ 客户端发送时会自动 @ 官方机器人。
        show 为展示名（默认与指令一致，可自定义如"点击回答"）。
        可通过 connect_link_template 配置项适配环境，模板占位符：
        {name}=指令显示名 {command}=指令原文 {self_id}=官机 ID
        {at_text}=@官机+指令原文 {encoded_name}/{encoded_command}/{encoded_at_text}=对应 URL 编码。
        模板显式置空则退回纯文本"（连接：@官机 指令）"。
        """
        template = self.config.get("connect_link_template")
        if template is None or any(marker in str(template) for marker in _LEGACY_TEMPLATE_MARKERS):
            # 未配置，或为旧版本默认模板（会把 @id 拼进 text 导致双重 @）时，使用新默认模板
            template = DEFAULT_CONNECT_TEMPLATE
        template = str(template).strip()
        if not template:
            return f"（连接：@{self_id} {command}）"
        display = show or command
        at_text = f"@{self_id} {command}"
        return template.format(
            name=command,
            command=command,
            self_id=self_id,
            at_text=at_text,
            encoded_command=quote(command, safe=""),
            encoded_name=quote(display, safe=""),
            encoded_at_text=quote(at_text, safe=""),
        )

    def _get_quick_entries(self) -> list[str]:
        """读取快捷入口配置；Wordle 固定排在最后，旧默认列表自动补入 Wordle。"""
        wordle = "Wordle"
        default = ["猜歌", "猜曲绘", "猜卡面", "歌词猜曲", wordle]
        legacy = ["猜歌", "猜曲绘", "猜卡面", "歌词猜曲"]
        entries = self.config.get("quick_entries")
        if entries is None:
            return list(default)
        cleaned = [str(x).strip() for x in entries if str(x).strip()]
        # AstrBot 不会用新默认值覆盖已保存的旧配置，对未自定义的旧默认做透明升级
        if cleaned == legacy:
            return list(default)
        if wordle in cleaned:
            # Wordle 统一放最后
            cleaned = [x for x in cleaned if x != wordle] + [wordle]
        return cleaned

    def _build_server_footer(self, event: AstrMessageEvent, server: str) -> list:
        """构建结算消息的题库服务器尾部：QQ 官方机附 markdown 连接入口与快捷入口，普通 QQ 仅提示指令。"""
        other = SERVER_SC if server == SERVER_JP else SERVER_JP
        switch_cmd = SWITCH_COMMANDS[server]  # 当前日服 → 切换国服题库
        connect_switch_cmd = CONNECT_SWITCH_COMMANDS[server]  # 带前缀的连接指令名
        lines = [f"本局题库服务器：{SERVER_LABELS[server]}"]

        if self._get_platform_name(event) == OFFICIAL_PLATFORM_NAME:
            self_id = str(getattr(event.message_obj, "self_id", "") or "").strip()
            if self_id:
                lines.append(self._build_connect_link(connect_switch_cmd, self_id))
                # 切换指令下方：绑定 / 查分 / 排行榜连接
                account_links = ["Wordle绑定QQ", "Wordle个人分数", "Wordle排行榜"]
                lines.append(
                    "  ".join(self._build_connect_link(name, self_id) for name in account_links)
                )
                entries = self._get_quick_entries()
                if entries:
                    lines.append("快捷入口：")
                    lines.append(
                        "  ".join(self._build_connect_link(name, self_id) for name in entries)
                    )
                return lines
        # 普通号（或拿不到官机 self_id 时的兜底）
        lines.append(f"你可以使用{switch_cmd}指令切换{SERVER_LABELS[other]}题库。")
        return lines

    async def _auto_next(self, event: AstrMessageEvent, session_id: str):
        """自动模式：延迟后开始下一局。"""
        delay = float(self.config.get("auto_next_delay_seconds", 6))
        await asyncio.sleep(delay)
        if session_id not in self.auto_sessions or session_id in self.games:
            return
        await event.send(event.plain_result("下一局 Wordle 即将开始……（发送「退出自动模式」可停止自动模式）"))
        async with self._get_session_lock(session_id):
            if session_id not in self.games and session_id in self.auto_sessions:
                await self._start_round(event, session_id, auto=True)

    # ---------- 超时看护 ----------

    def _start_idle_watcher(self, session_id: str):
        sess = self.games.get(session_id)
        if sess is None:
            return
        task = asyncio.create_task(self._idle_watcher(session_id, sess))
        sess["idle_task"] = task
        self._track_task(task)

    def _cancel_idle_task(self, sess: dict):
        task = sess.get("idle_task")
        if task and not task.done():
            task.cancel()

    async def _idle_watcher(self, session_id: str, sess: dict):
        """长时间无猜测自动结束本局。"""
        timeout = float(self.config.get("game_idle_timeout_seconds", 300))
        try:
            while True:
                await asyncio.sleep(10)
                if self.games.get(session_id) is not sess:
                    return
                if time.time() - sess["last_activity"] > timeout:
                    game = sess["game"]
                    if not game.is_finished():
                        game.forfeit("timeout")
                        event = sess["event"]
                        await self._finish_game(event, session_id, reason="timeout")
                    return
        except asyncio.CancelledError:
            return

    # ---------- 其他 ----------

    @staticmethod
    def _answer_display(answer: dict) -> str:
        """答案展示名：只显示中文译名，不附带日文原名。"""
        return answer.get("cn") or answer.get("title") or "未知"

    async def _periodic_cleanup(self):
        while True:
            await asyncio.sleep(3600)
            try:
                self.render_service.cleanup_output_dir()
            except Exception as e:
                logger.warning(f"[PJSK Wordle] 清理输出目录失败: {e}")
