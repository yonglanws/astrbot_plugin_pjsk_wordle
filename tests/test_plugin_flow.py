"""插件主流程端到端测试（mock astrbot 框架，无需 AstrBot 运行环境）。

覆盖：开局广播、全局监听猜测、错误猜测计数、制胜猜测计分、
结算文案格式、题库服务器切换与持久化、退出与自动模式。
"""

import json
import os
import sys
import types
from pathlib import Path
from urllib.parse import quote

import pytest

PLUGIN_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(PLUGIN_ROOT))


# ---------------- astrbot 框架 mock ----------------


def _install_astrbot_mocks():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api_event = types.ModuleType("astrbot.api.event")
    api_star = types.ModuleType("astrbot.api.star")
    api_comp = types.ModuleType("astrbot.api.message_components")
    core = types.ModuleType("astrbot.core")
    core_utils = types.ModuleType("astrbot.core.utils")
    core_sw = types.ModuleType("astrbot.core.utils.session_waiter")

    import logging

    api.logger = logging.getLogger("astrbot_mock")

    class AstrBotConfig(dict):
        def save_config(self):
            pass

    api.AstrBotConfig = AstrBotConfig

    class _Filter:
        EventMessageType = types.SimpleNamespace(ALL="ALL", GROUP_MESSAGE="G", PRIVATE_MESSAGE="P")
        PlatformAdapterType = types.SimpleNamespace(ALL="ALL")

        @staticmethod
        def command(name, alias=None, priority=0):
            def deco(func):
                func._astrbot_command = {"name": name, "alias": alias or set()}
                return func

            return deco

        @staticmethod
        def event_message_type(t, priority=0):
            def deco(func):
                func._astrbot_listener = True
                return func

            return deco

        @staticmethod
        def permission_type(t):
            def deco(func):
                return func

            return deco

    api_event.filter = _Filter()
    api_event.AstrMessageEvent = object

    class Star:
        def __init__(self, context):
            self.context = context

        async def send(self, *a, **k):
            pass

    def register(name, author, desc, version, repo=None):
        def deco(cls):
            cls._astrbot_meta = (name, author, desc, version, repo)
            return cls

        return deco

    api_star.Context = object
    api_star.Star = Star
    api_star.register = register

    _star_tools = types.ModuleType("astrbot.api.star.tools")

    class StarTools:
        _root = None

        @staticmethod
        def get_data_dir(name):
            p = Path(StarTools._root) / name
            p.mkdir(parents=True, exist_ok=True)
            return p

    api_star.StarTools = StarTools

    class Comp:
        class At:
            def __init__(self, qq="", name=""):
                self.qq = qq
                self.name = name

        class Plain:
            def __init__(self, text=""):
                self.text = text

        class Image:
            def __init__(self, file=""):
                self.file = file

            @classmethod
            def fromFileSystem(cls, file):
                return cls(file=file)

    api_comp.Plain = Comp.Plain
    api_comp.Image = Comp.Image
    api_comp.At = Comp.At

    class SessionController:
        def stop(self):
            pass

        def keep(self, timeout=60, reset_timeout=False):
            pass

    class SessionFilter:
        def filter(self, event):
            return ""

    def session_waiter(timeout=60, record_history_chains=True):
        def deco(func):
            func._session_waiter = timeout
            return func

        return deco

    core_sw.session_waiter = session_waiter
    core_sw.SessionController = SessionController
    core_sw.SessionFilter = SessionFilter

    for name, mod in {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": api_event,
        "astrbot.api.star": api_star,
        "astrbot.api.message_components": api_comp,
        "astrbot.api.star.tools": _star_tools,
        "astrbot.core": core,
        "astrbot.core.utils": core_utils,
        "astrbot.core.utils.session_waiter": core_sw,
    }.items():
        sys.modules.setdefault(name, mod)

    return StarTools


StarTools = _install_astrbot_mocks()

import importlib  # noqa: E402

# 以包形式加载插件（main.py 使用相对导入，与 AstrBot 加载方式一致）
_PKG = "astrbot_plugin_pjsk_wordle"
if _PKG not in sys.modules:
    _pkg = types.ModuleType(_PKG)
    _pkg.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[_PKG] = _pkg
_main = importlib.import_module(f"{_PKG}.main")
from services.data_service import SERVER_JP, SERVER_SC  # noqa: E402

PjskWordlePlugin = _main.PjskWordlePlugin
_get_normalized_session_id = _main._get_normalized_session_id


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
    }
    base.update(kwargs)
    return base


# ---------------- 假事件与假上下文 ----------------


class FakeEvent:
    def __init__(
        self,
        text,
        sender_id="10001",
        sender_name="测试玩家",
        group_id="20001",
        platform="aiocqhttp",
        self_id="10001",
        at_bot=True,
    ):
        self.message_str = text
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._group_id = group_id
        self._platform = platform
        self.unified_msg_origin = f"{platform}:GroupMessage:{group_id}"
        message_chain = []
        if at_bot:
            # 使用 mock 的 At 组件（与 main.py isinstance 检查一致）
            at = sys.modules["astrbot.api.message_components"].At(qq=self_id)
            message_chain.append(at)
        self.message_obj = types.SimpleNamespace(self_id=self_id, message=message_chain)
        self.is_admin = False
        self.sent = []  # 记录发送的消息

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_group_id(self):
        return self._group_id

    def get_platform_name(self):
        return self._platform

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)

    def image_result(self, path):
        return ("image", path)

    def make_result(self):
        return FakeResult()

    async def send(self, result):
        self.sent.append(result)


class FakeResult:
    """模拟 AstrBot 的 MessageEventResult（含 use_markdown 标记）。"""

    def __init__(self):
        self.chain = []
        self.use_markdown_ = None

    def use_markdown(self, use=True):
        self.use_markdown_ = use


class FakeContext:
    pass


def make_songs():
    return [
        make_song(),
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
            title="烈火",
            cn="烈火",
            date="2024-04-30",
            bpm=132,
            master=27,
            append=False,
            category="Vivid BAD SQUAD",
            artist="niki",
        ),
        make_song(
            id=4,
            title="ロキ",
            cn="ROKI",
            date="2021-02-20",
            bpm=150,
            master=28,
            append=False,
            category="虚拟歌手",
            artist="みきとP",
            aliases=["洛基"],
        ),
    ]


import pytest_asyncio  # noqa: E402


@pytest_asyncio.fixture
async def plugin(tmp_path, monkeypatch):
    StarTools._root = tmp_path
    # 固定随机答案为 1 号（blender）
    import random

    monkeypatch.setattr(random, "choice", lambda seq: seq[0])

    # 阻止后台初始化任务联网拉取题库
    from services.data_service import DataService

    async def _noop_start(self):
        pass

    monkeypatch.setattr(DataService, "start", _noop_start)

    # 渲染走真实 Pillow，输出到临时目录
    config = {
        "default_server": "jp",
        "game_cooldown_seconds": 0,
        "max_guesses": 8,
        "group_whitelist": [],
        "whitelist_reject_message": "",
        "super_users": [],
        "ranking_display_count": 10,
    }

    plugin = PjskWordlePlugin(FakeContext(), config)
    songs = make_songs()
    plugin.data_service.songs[SERVER_JP] = songs
    plugin.data_service.songs[SERVER_SC] = songs  # 测试中两服共用同一题库
    plugin.game_service.update_songs(SERVER_JP, songs)
    plugin.game_service.update_songs(SERVER_SC, songs)
    await plugin.db_service.init_db()
    yield plugin

    await plugin.terminate()


def sent_texts(event):
    """收集事件发送的纯文本（兼容 plain_result 元组与 make_result 消息对象）。"""
    texts = []
    for r in event.sent:
        if isinstance(r, FakeResult):
            texts.extend(getattr(c, "text", "") for c in r.chain)
        elif isinstance(r, tuple) and r[0] == "plain":
            texts.append(r[1])
    return texts


def sent_images(event):
    return [
        c.file
        for r in event.sent
        if r[0] == "chain" and isinstance(r[1], list)
        for c in r[1]
        if hasattr(c, "file")
    ]


@pytest.mark.asyncio
class TestPluginFlow:
    async def test_full_game_win(self, plugin):
        event = FakeEvent("wordle")
        await plugin.start_wordle(event)
        # 开局：文字介绍 + 空棋盘
        assert any("PJSK Wordle 开始" in t for t in sent_texts(event))
        assert sent_images(event), "开局应发送棋盘图片"
        session_id = _get_normalized_session_id(event)
        assert session_id in plugin.games

        # 错误猜测（监听器路径）
        wrong = FakeEvent("烈火", sender_id="10002", sender_name="B")
        await plugin.on_message(wrong)
        game = plugin.games[session_id]["game"]
        assert game.guess_count == 1
        assert len(sent_images(wrong)) == 1

        # 无效聊天静默忽略
        chat = FakeEvent("今天天气真好", sender_id="10003")
        await plugin.on_message(chat)
        assert not sent_texts(chat) and not sent_images(chat)
        assert plugin.games[session_id]["game"].guess_count == 1

        # 别名猜测（洛基 -> ロキ）
        alias_ev = FakeEvent("洛基", sender_id="10004")
        await plugin.on_message(alias_ev)
        assert plugin.games[session_id]["game"].guess_count == 2

        # 制胜猜测
        win_ev = FakeEvent("blender", sender_id="10001", sender_name="测试玩家")
        await plugin.on_message(win_ev)
        assert session_id not in plugin.games

        result_text = "\n".join(sent_texts(win_ev))
        assert "Wordle 结束" in result_text
        assert "你在第 3 / 8 次猜对了，增加3分" in result_text
        assert "正确答案：blender" in result_text
        assert "本局题库服务器：日服" in result_text
        # 普通 QQ：仅提示切换指令，不显示快捷入口
        assert "你可以使用切换国服题库指令切换国服题库。" in result_text
        assert "快捷入口" not in result_text

        # 积分入库
        summary = await plugin.db_service.get_user_summary(session_id, "10001", "aiocqhttp")
        assert summary and summary["score"] == 3 and summary["wins"] == 1
        assert summary["best_guesses"] == 3

    async def test_win_result_official_platform(self, plugin):
        """QQ 官方机器人平台：结算以 markdown 参数指令标签附连接入口与快捷入口。"""
        event = FakeEvent("wordle", platform="qq_official", self_id="AABBCCDD")
        await plugin.start_wordle(event)
        win_ev = FakeEvent(
            "blender", sender_id="a" * 32, platform="qq_official", self_id="AABBCCDD"
        )
        await plugin.on_message(win_ev)

        result_text = "\n".join(sent_texts(win_ev))
        assert "你在第 1 / 8 次猜对了，增加4分" in result_text
        assert "快捷入口：" in result_text

        def tag(name: str) -> str:
            # text 只含指令（客户端发送时自动 @ 官机），show 为展示名
            return (
                f'<qqbot-cmd-input text="{quote(name, safe="")}" show="{quote(name, safe="")}" />'
            )

        assert tag("Wordle切换国服题库") in result_text
        assert "（连接：" not in result_text  # 不再使用纯文本连接写法
        assert "@AABBCCDD" not in result_text  # 不再把 @id 拼进 text（避免双重 @）
        # 切换指令下方：绑定 / 查分 / 排行榜连接（均带 Wordle 前缀）
        for name in ("Wordle绑定QQ", "Wordle个人分数", "Wordle排行榜"):
            assert tag(name) in result_text
        assert "个人分数（" not in result_text and tag("排行榜") not in result_text
        for name in ("Wordle", "猜歌", "猜曲绘", "猜卡面", "歌词猜曲"):
            assert tag(name) in result_text

    async def test_connect_link_template_fallback(self, plugin):
        """连接模板显式置空时退回纯文本（连接：@官机 指令）形式。"""
        plugin.config["connect_link_template"] = ""
        assert (
            plugin._build_connect_link("切换国服题库", "BOTID") == "（连接：@BOTID 切换国服题库）"
        )
        # 默认模板使用 QQ 官方 markdown 参数指令标签，text 为 URL 编码的指令（不含 @）
        plugin.config["connect_link_template"] = None
        link = plugin._build_connect_link("切换国服题库", "BOTID")
        assert link.startswith("<qqbot-cmd-input ")
        assert quote("切换国服题库", safe="") in link
        assert "BOTID" not in link

    async def test_multiplayer_only_finisher_scores(self, plugin):
        """多人游玩：任何人都可以猜，但只有最后完整答出的人计分。"""
        event = FakeEvent("wordle")
        await plugin.start_wordle(event)
        session_id = _get_normalized_session_id(event)
        for i in range(3):
            ev = FakeEvent("烈火", sender_id=str(20000 + i))
            await plugin.on_message(ev)

        finisher = FakeEvent("blender", sender_id="30001", sender_name="压轴玩家")
        await plugin.on_message(finisher)
        assert session_id not in plugin.games

        summary = await plugin.db_service.get_user_summary(session_id, "30001", "aiocqhttp")
        assert summary and summary["score"] == 3 and summary["wins"] == 1  # 第 4 次猜对 → 3 分
        for i in range(3):
            other = await plugin.db_service.get_user_summary(
                session_id, str(20000 + i), "aiocqhttp"
            )
            assert other is None, "未完成回答的玩家不应得分"

    async def test_fail_after_8(self, plugin):
        event = FakeEvent("wordle")
        await plugin.start_wordle(event)
        session_id = _get_normalized_session_id(event)
        ev = None
        for i in range(8):
            ev = FakeEvent("烈火", sender_id=str(10000 + i))
            await plugin.on_message(ev)
        assert session_id not in plugin.games
        last_texts = "\n".join(sent_texts(ev))
        assert "8 次机会已用完" in last_texts

    async def test_quit_ends_game(self, plugin):
        event = FakeEvent("wordle")
        await plugin.start_wordle(event)
        session_id = _get_normalized_session_id(event)
        quit_ev = FakeEvent("退出")
        await plugin.on_message(quit_ev)
        assert session_id not in plugin.games
        assert "本局已结束" in "\n".join(sent_texts(quit_ev))

        # 无对局时发送退出（无自动模式）：静默
        idle_ev = FakeEvent("退出")
        await plugin.on_message(idle_ev)
        assert not sent_texts(idle_ev)

    async def test_quit_via_listener_stops_auto(self, plugin):
        """自动模式下：游戏中发送 退出 结束本局并停止续局；无对局时发送 退出 停止自动模式。"""
        started = FakeEvent("自动wordle")
        await plugin.start_auto_wordle(started)
        session_id = _get_normalized_session_id(started)
        assert session_id in plugin.auto_sessions
        assert session_id in plugin.games

        quit_ev = FakeEvent("退出")
        await plugin.on_message(quit_ev)
        assert session_id not in plugin.auto_sessions
        assert session_id not in plugin.games

        # 无对局时再发 退出：停止自动模式（本例已停止，静默）
        again = FakeEvent("退出")
        await plugin.on_message(again)
        assert not sent_texts(again)

    async def test_switch_server_and_persist(self, plugin):
        event = FakeEvent("切换国服题库")
        await plugin.switch_to_sc(event)
        session_id = _get_normalized_session_id(event)
        assert plugin._server_for_session(session_id) == "sc"
        # 持久化
        saved = json.loads(plugin.server_prefs_path.read_text(encoding="utf-8"))
        assert saved[session_id] == "sc"
        assert "已切换为国服题库" in "\n".join(sent_texts(event))

        # 游戏进行中不允许切换
        start_ev = FakeEvent("wordle")
        await plugin.start_wordle(start_ev)
        sw_ev = FakeEvent("切换日服题库")
        await plugin.switch_to_jp(sw_ev)
        assert "进行中" in "\n".join(sent_texts(sw_ev))
        assert plugin._server_for_session(session_id) == "sc"

    async def test_auto_mode_lifecycle(self, plugin):
        started = FakeEvent("自动wordle")
        await plugin.start_auto_wordle(started)
        session_id = _get_normalized_session_id(started)
        assert session_id in plugin.auto_sessions
        assert session_id in plugin.games

        # 退出（全局监听）：结束本局且不再续局
        quit_ev = FakeEvent("退出")
        await plugin.on_message(quit_ev)
        assert session_id not in plugin.auto_sessions
        assert session_id not in plugin.games

    async def test_listener_ignores_own_commands(self, plugin):
        event = FakeEvent("wordle")
        await plugin.start_wordle(event)
        session_id = _get_normalized_session_id(event)
        # 指令词不应被当作猜测
        cmd_ev = FakeEvent("wordle排行榜")
        await plugin.on_message(cmd_ev)
        assert plugin.games[session_id]["game"].guess_count == 0

    async def test_other_server_song_hint(self, plugin):
        # 不在题库中的普通聊天：静默忽略
        event = FakeEvent("不存在的歌曲xyz")
        await plugin.start_wordle(FakeEvent("wordle"))
        await plugin.on_message(event)
        assert not sent_texts(event)

    async def test_non_at_message_ignored(self, plugin):
        """未 @ 机器人的消息即使像歌名也不参与回答。"""
        event = FakeEvent("wordle")
        await plugin.start_wordle(event)
        session_id = _get_normalized_session_id(event)

        no_at = FakeEvent("blender", sender_id="40001", at_bot=False)
        await plugin.on_message(no_at)
        assert session_id in plugin.games  # 游戏继续
        assert not sent_texts(no_at) and not sent_images(no_at)
        assert plugin.games[session_id]["game"].guess_count == 0

    async def test_non_at_message_ignored_on_official(self, plugin):
        """QQ 官方群聊：未 @ 机器人的消息同样不参与回答（回归修复）。"""
        event = FakeEvent("wordle", platform="qq_official", self_id="AABBCCDD")
        await plugin.start_wordle(event)
        session_id = _get_normalized_session_id(event)

        no_at = FakeEvent(
            "blender",
            sender_id="a" * 32,
            platform="qq_official",
            self_id="AABBCCDD",
            at_bot=False,
        )
        await plugin.on_message(no_at)
        assert session_id in plugin.games
        assert not sent_texts(no_at) and not sent_images(no_at)
        assert plugin.games[session_id]["game"].guess_count == 0

    async def test_official_start_message_has_answer_link(self, plugin):
        """官方平台开局消息末尾并排"回答 / 退出"两个连接。"""
        from urllib.parse import quote

        event = FakeEvent("Wordle", platform="qq_official", self_id="AABBCCDD")
        await plugin.start_wordle(event)
        intro_text = "\n".join(sent_texts(event))
        assert "@本机器人 + 曲名或别名进行回答" in intro_text
        assert "退出 可结束本局" not in intro_text  # 退出说明文字已删除
        answer_link = (
            f'<qqbot-cmd-input text="{quote(" ", safe="")}" show="{quote("点击回答", safe="")}" />'
        )
        quit_link = f'<qqbot-cmd-input text="{quote("退出", safe="")}" show="{quote("退出本局", safe="")}" />'
        assert answer_link in intro_text
        assert quit_link in intro_text
        assert intro_text.index(answer_link) < intro_text.index(quit_link)

    async def test_answer_display_chinese_only(self, plugin):
        """结算中的正确答案只显示中文译名，不带日文原名。"""
        assert plugin._answer_display({"cn": "彗星的银河", "title": "彗星ノ銀河"}) == "彗星的银河"
        assert plugin._answer_display({"cn": "blender", "title": "blender"}) == "blender"
        assert plugin._answer_display({"title": "ロキ"}) == "ロキ"  # 无中文时回退原名


def test_connect_link_legacy_template_migrated(plugin):
    """旧版本默认模板（会把 @id 拼进 text）应自动升级为新默认模板。"""
    from urllib.parse import quote

    for stale in (
        '<qqbot-cmd-input text="{encoded_at_text}" show="{encoded_name}" />',
        "[{name}](mqqapi://container/showcmdcard?cmd={encoded_command})",
    ):
        plugin.config["connect_link_template"] = stale
        link = plugin._build_connect_link("猜歌", "qq_official")
        assert link.startswith("<qqbot-cmd-input ")
        assert quote("猜歌", safe="") in link
        assert "qq_official" not in link and "%40" not in link  # text 不含 @


def test_quick_entries_wordle_last(plugin):
    """快捷入口：旧默认自动补 Wordle 且固定排最后；自定义顺序也会把 Wordle 挪到最后。"""
    legacy = ["猜歌", "猜曲绘", "猜卡面", "歌词猜曲"]
    plugin.config["quick_entries"] = legacy
    assert plugin._get_quick_entries() == ["猜歌", "猜曲绘", "猜卡面", "歌词猜曲", "Wordle"]

    plugin.config["quick_entries"] = ["Wordle", "猜歌", "猜曲绘", "猜卡面", "歌词猜曲"]
    assert plugin._get_quick_entries() == ["猜歌", "猜曲绘", "猜卡面", "歌词猜曲", "Wordle"]

    plugin.config["quick_entries"] = ["猜歌", "Wordle", "猜卡面"]
    assert plugin._get_quick_entries() == ["猜歌", "猜卡面", "Wordle"]

    plugin.config["quick_entries"] = None
    assert plugin._get_quick_entries() == ["猜歌", "猜曲绘", "猜卡面", "歌词猜曲", "Wordle"]


def test_wordle_command_capital_alias():
    """Wordle 指令注册兼容大写 W（Wordle / 自动Wordle）。"""
    assert "Wordle" in PjskWordlePlugin.start_wordle._astrbot_command["alias"]
    assert "自动Wordle" in PjskWordlePlugin.start_auto_wordle._astrbot_command["alias"]
