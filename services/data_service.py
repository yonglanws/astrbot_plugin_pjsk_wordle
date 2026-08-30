"""题库数据服务。

数据来源与职责：
- 日服题库: https://github.com/Team-Haruki/haruki-sekai-master   (musics / musicDifficulties / musicVocals / musicArtists / versions-current_version)
- 国服题库: https://github.com/Team-Haruki/haruki-sekai-sc-master (musics / musicDifficulties / musicVocals / versions-current_version)
- 歌曲中文译名: https://translation.exmeaning.com/files/translation/music.json （Moesekai 同款翻译源）
- 歌曲别名:     https://moe.exmeaning.com/data/music_alias/music_aliases.json （Moesekai 同款别名源）
- 歌曲 BPM:     https://moe.exmeaning.com/data/music_bpm/music_bpms.json

拉取策略：GitHub 托管的文件一律优先走 GitHub Contents API；题库版本号取 current_version.json 的 dataVersion（如 6.8.0.12），
仅在 GitHub API 不可用（限流/网络失败/文件过大）时回退 jsDelivr CDN。
非 GitHub 托管的翻译/别名/BPM 直接从其官方源拉取。

所有文件持久化在 plugin_data 持久化目录下，每 24 小时自动更新一次；
同时基于原始数据构建派生题库 derived.json（含中文名/别名/分类/作者/BPM/MASTER/APPEND），
游戏运行时只读派生题库，避免每局重新解析原始大文件。
"""

import asyncio
import base64
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

try:  # 在 AstrBot 内使用其 logger，脱离 AstrBot（单测）时退回标准 logging
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_pjsk_wordle")

SERVER_JP = "jp"
SERVER_SC = "sc"
SERVER_LABELS = {SERVER_JP: "日服", SERVER_SC: "国服"}

# 各服务器需要拉取的必要 JSON 文件（不拉取仓库中的其他任何文件）。
# (仓库内路径, 本地存储文件名)：versions/current_version.json 提供题库版本号 dataVersion（如 6.8.0.12）
_SERVER_FILES = {
    SERVER_JP: [
        ("master/musics.json", "musics.json"),
        ("master/musicDifficulties.json", "musicDifficulties.json"),
        ("master/musicVocals.json", "musicVocals.json"),
        ("master/musicArtists.json", "musicArtists.json"),
        ("master/events.json", "events.json"),
        ("master/eventMusics.json", "eventMusics.json"),
        ("versions/current_version.json", "current_version.json"),
    ],
    SERVER_SC: [
        ("master/musics.json", "musics.json"),
        ("master/musicDifficulties.json", "musicDifficulties.json"),
        ("master/musicVocals.json", "musicVocals.json"),
        ("master/events.json", "events.json"),
        ("master/eventMusics.json", "eventMusics.json"),
        ("versions/current_version.json", "current_version.json"),
    ],
}

_GITHUB_REPOS = {
    SERVER_JP: "Team-Haruki/haruki-sekai-master",
    SERVER_SC: "Team-Haruki/haruki-sekai-sc-master",
}
_GITHUB_BRANCH = "main"

# 派生题库构建规则版本：规则变更时 +1，启动时用本地原始文件离线重建
DERIVED_RULE = 8  # v8: 支持 WorldLink 专属分类识别与跨团合唱精确分类

_EXTRA_SOURCES = {
    "translation": "https://translation.exmeaning.com/files/translation/music.json",
    "alias": "https://moe.exmeaning.com/data/music_alias/music_aliases.json",
    "bpm": "https://moe.exmeaning.com/data/music_bpm/music_bpms.json",
}

_JSDELIVR_TPL = "https://cdn.jsdelivr.net/gh/{repo}@{branch}/{path}"

# 服务器本地时区（上线时间按服务器时区取日期）
_SERVER_TZ = {
    SERVER_JP: timezone(timedelta(hours=9)),
    SERVER_SC: timezone(timedelta(hours=8)),
}

# 角色分组映射：characterId -> 单元
_UNIT_BY_CHAR = {}
for _cid in range(1, 5):
    _UNIT_BY_CHAR[_cid] = "Leo/need"
for _cid in range(5, 9):
    _UNIT_BY_CHAR[_cid] = "MORE MORE JUMP!"
for _cid in range(9, 13):
    _UNIT_BY_CHAR[_cid] = "Vivid BAD SQUAD"
for _cid in range(13, 17):
    _UNIT_BY_CHAR[_cid] = "Wonderlands×Showtime"
for _cid in range(17, 21):
    _UNIT_BY_CHAR[_cid] = "n25"
for _cid in range(21, 27):
    _UNIT_BY_CHAR[_cid] = "virtual_singer"

# 单元显示名（n25 / 虚拟歌手 按服务器本地化，其余两服通用）
_UNIT_DISPLAY = {
    SERVER_JP: {
        "n25": "25時、ナイトコードで。",
        "virtual_singer": "虚拟歌手",
    },
    SERVER_SC: {
        "n25": "25时，Nightcord见。",
        "virtual_singer": "虚拟歌手",
    },
}
_UNIT_DISPLAY_DEFAULT = {
    "n25": "25時、ナイトコードで。",
    "virtual_singer": "虚拟歌手",
}

_UNKNOWN_CATEGORY = "其他"


def _unit_display(unit_key: str, server: str) -> str:
    table = _UNIT_DISPLAY.get(server, _UNIT_DISPLAY_DEFAULT)
    return table.get(unit_key, unit_key)


class DataService:
    """题库下载、缓存、派生与查询。"""

    def __init__(
        self,
        data_dir: Path,
        update_interval_hours: int = 24,
        request_timeout: int = 60,
    ):
        self.data_dir = Path(data_dir)
        self.music_dir = self.data_dir / "musicdata"
        self.music_dir.mkdir(parents=True, exist_ok=True)
        self.update_interval_hours = max(1, int(update_interval_hours))
        self.request_timeout = request_timeout

        self.meta_path = self.music_dir / "meta.json"
        self.meta: dict = {}

        self.songs: dict[str, list[dict]] = {SERVER_JP: [], SERVER_SC: []}
        # 别名/BPM/翻译属于两服共用的补充数据
        self.alias_by_id: dict[int, list[str]] = {}
        self.alias_by_title: dict[str, list[str]] = {}
        self.bpm_by_id: dict[int, float] = {}
        self.bpm_by_title: dict[str, float] = {}
        self.cn_by_title: dict[str, str] = {}

        self._fetch_lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None
        self._update_task: asyncio.Task | None = None
        # 题库重建后的回调（由插件主流程注入，用于刷新匹配器）
        self.on_songs_updated = None

    # ---------- 路径辅助 ----------

    def _server_dir(self, server: str) -> Path:
        return self.music_dir / server

    @property
    def derived_path(self) -> dict[str, Path]:
        return {s: self._server_dir(s) / "derived.json" for s in (SERVER_JP, SERVER_SC)}

    # ---------- 生命周期 ----------

    async def start(self):
        """启动：加载本地缓存，缺失则立即拉取，并启动 24h 周期更新任务。"""
        self.music_dir.mkdir(parents=True, exist_ok=True)
        self._load_meta()
        self._load_extras_memory()

        loaded_any = False
        for server in (SERVER_JP, SERVER_SC):
            if not self._load_derived(server):
                continue
            loaded_any = True
            # 派生规则升级时，用本地缓存的原始文件离线重建（分类修正等无需重新下载）
            info = self.meta.setdefault("servers", {}).get(server, {})
            if info.get("derived_rule") != DERIVED_RULE:
                if not self._rebuild_from_local(server):
                    # 本地原始文件不全，清除时间戳以便周期任务重新拉取
                    info.pop("updated_at", None)
                    self._save_meta()
        if loaded_any:
            logger.info("[PJSK Wordle] 本地题库加载完成。")

        self._update_task = asyncio.create_task(self._update_loop())

    async def terminate(self):
        if self._update_task:
            self._update_task.cancel()
            try:
                await self._update_task
            except (asyncio.CancelledError, Exception):
                pass
            self._update_task = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ---------- 对外查询接口 ----------

    def get_songs(self, server: str) -> list[dict]:
        return self.songs.get(server, [])

    def get_song_count(self, server: str) -> int:
        return len(self.songs.get(server, []))

    def get_version(self, server: str) -> str:
        """题库版本号：优先 GitHub blob sha 前 7 位，否则使用更新日期。"""
        info = self.meta.get("servers", {}).get(server, {})
        version = info.get("version")
        if version:
            return str(version)
        updated = info.get("updated_at")
        if updated:
            return datetime.fromtimestamp(updated).strftime("%Y%m%d")
        return "unknown"

    # ---------- 周期更新 ----------

    async def _update_loop(self):
        """每 30 分钟检查一次，超过更新间隔（默认 24h）的数据源自动重新拉取。"""
        try:
            await self.refresh_if_stale()
        except Exception as e:
            logger.error(f"[PJSK Wordle] 首次题库检查失败: {e}", exc_info=True)
        while True:
            await asyncio.sleep(1800)
            try:
                await self.refresh_if_stale()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[PJSK Wordle] 周期题库更新失败: {e}", exc_info=True)

    async def refresh_if_stale(self, force: bool = False):
        """按 24h 间隔刷新所有数据源；force=True 时无视间隔强制刷新。"""
        async with self._fetch_lock:
            now = time.time()
            interval = self.update_interval_hours * 3600
            stale_servers = []
            for server in (SERVER_JP, SERVER_SC):
                info = self.meta.get("servers", {}).get(server, {})
                if (
                    force
                    or not self.songs.get(server)
                    or now - info.get("updated_at", 0) >= interval
                ):
                    stale_servers.append(server)
            stale_extras = [
                key
                for key in _EXTRA_SOURCES
                if force
                or now - self.meta.get("extras", {}).get(key, {}).get("updated_at", 0) >= interval
            ]

            # 先更新共用补充数据（别名/BPM/翻译），服务器题库构建派生数据时才能带上它们
            if stale_extras:
                try:
                    await self._update_extras(stale_extras)
                except Exception as e:
                    logger.error(
                        f"[PJSK Wordle] 补充数据（别名/BPM/翻译）更新失败: {e}",
                        exc_info=True,
                    )

            for server in stale_servers:
                try:
                    await self._update_server(server)
                except Exception as e:
                    logger.error(
                        f"[PJSK Wordle] {SERVER_LABELS[server]}题库更新失败: {e}",
                        exc_info=True,
                    )

            if stale_servers or stale_extras:
                self._save_meta()

    # ---------- 单服务器题库更新 ----------

    async def _update_server(self, server: str):
        repo = _GITHUB_REPOS[server]
        raw: dict[str, bytes] = {}
        blob_sha = None
        via = "github"

        # 优先 GitHub Contents API（同时拿到 blob sha 作为兜底版本号）
        for remote_path, local_name in _SERVER_FILES[server]:
            data, sha = await self._fetch_github_contents(repo, remote_path)
            raw[local_name] = data
            if sha and blob_sha is None:
                blob_sha = sha[:7]
            await asyncio.sleep(0.3)  # 轻微限速，尊重匿名 API 配额

        parsed = {name: json.loads(raw[name].decode("utf-8")) for name in raw}

        # GitHub 全部成功才走该分支；任何失败都会抛异常进入 jsDelivr 回退
        self._store_server(server, parsed, blob_sha, via)

    def _rebuild_from_local(self, server: str) -> bool:
        """用本地缓存的原始 JSON 离线重建派生题库（用于派生规则升级）。"""
        sdir = self._server_dir(server)
        parsed: dict = {}
        for _, local_name in _SERVER_FILES[server]:
            f = sdir / local_name
            if not f.exists():
                logger.warning(
                    f"[PJSK Wordle] 本地缺少 {SERVER_LABELS[server]}/{local_name}，跳过离线重建"
                )
                return False
            try:
                parsed[local_name] = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"[PJSK Wordle] 本地文件 {local_name} 解析失败: {e}")
                return False
        try:
            info = self.meta.get("servers", {}).get(server, {})
            self._store_server(server, parsed, blob_sha=info.get("blob_sha"), via="local-rebuild")
            return True
        except Exception as e:
            logger.warning(f"[PJSK Wordle] {SERVER_LABELS[server]}离线重建失败: {e}")
            return False

    def _store_server(self, server: str, parsed: dict, blob_sha: str | None, via: str):
        """落盘原始文件并重建派生题库。"""
        sdir = self._server_dir(server)
        sdir.mkdir(parents=True, exist_ok=True)
        for name, data in parsed.items():
            (sdir / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        songs = self._build_derived(server, parsed)
        if not songs:
            raise RuntimeError(f"{SERVER_LABELS[server]}题库解析结果为空")

        self.songs[server] = songs
        derived_file = self.derived_path[server]
        derived_file.write_text(json.dumps(songs, ensure_ascii=False), encoding="utf-8")

        # 题库版本号：优先游戏数据版本 dataVersion（如 6.8.0.12），否则 blob sha，否则日期
        current_version = parsed.get("current_version.json") or {}
        data_version = str(current_version.get("dataVersion") or "").strip()
        display_version = data_version or blob_sha or datetime.now().strftime("%Y%m%d")

        servers = self.meta.setdefault("servers", {})
        servers[server] = {
            "version": display_version,
            "data_version": data_version or None,
            "blob_sha": blob_sha,
            "updated_at": time.time(),
            "via": via,
            "count": len(songs),
            "derived_rule": DERIVED_RULE,
        }
        self._save_meta()
        logger.info(
            f"[PJSK Wordle] {SERVER_LABELS[server]}题库已更新: {len(songs)} 首, "
            f"版本 {display_version} (via {via})"
        )
        if self.on_songs_updated:
            try:
                result = self.on_songs_updated()
                if asyncio.iscoroutine(result):
                    # 同步上下文中调度异步回调，不阻塞当前更新流程
                    asyncio.get_running_loop().create_task(result)
            except RuntimeError:
                try:
                    result.close()
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"[PJSK Wordle] 题库更新回调执行失败: {e}")

    # ---------- 补充数据（别名 / BPM / 翻译） ----------

    async def _update_extras(self, keys: list[str]):
        """逐个更新补充数据；单个失败不影响其余，全部失败才抛异常。"""
        errors = []
        for key in keys:
            try:
                raw = await self._fetch_url(_EXTRA_SOURCES[key])
                self._store_extra(key, json.loads(raw.decode("utf-8")), update_meta=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                errors.append(f"{key}: {e}")
                logger.warning(f"[PJSK Wordle] 拉取 {key} 数据失败: {e}")
        if len(errors) == len(keys):
            raise RuntimeError("全部补充数据拉取失败: " + "; ".join(errors))

    def _store_extra(self, key: str, data, update_meta: bool = False):
        if key == "translation":
            # 结构: {"artist": {...}, "title": {日文原名: 中文译名}, "vocalCaption": {...}}
            self.cn_by_title = dict(data.get("title") or {})
            (self.music_dir / "translation_music.json").write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
        elif key == "alias":
            # 结构: {"generated_at": ..., "musics": [{"music_id": int, "title": str, "aliases": [...]}]}
            self.alias_by_id = {}
            self.alias_by_title = {}
            for entry in data.get("musics", []):
                mid = entry.get("music_id")
                aliases = [a for a in entry.get("aliases", []) if a]
                if not isinstance(mid, int):
                    continue
                self.alias_by_id[mid] = aliases
                title = entry.get("title")
                if title:
                    self.alias_by_title[title] = aliases
            (self.music_dir / "alias.json").write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
        elif key == "bpm":
            # 结构: {"generated_at": ..., "songs": [{"music_id": int, "title": str, "bpm": float, ...}]}
            self.bpm_by_id = {}
            self.bpm_by_title = {}
            for entry in data.get("songs", []):
                mid = entry.get("music_id")
                bpm = entry.get("bpm")
                if not isinstance(mid, int) or not isinstance(bpm, (int, float)):
                    continue
                self.bpm_by_id[mid] = float(bpm)
                title = entry.get("title")
                if title and title not in self.bpm_by_title:
                    self.bpm_by_title[title] = float(bpm)
            (self.music_dir / "bpms.json").write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )

        if update_meta:
            extras = self.meta.setdefault("extras", {})
            extras[key] = {
                "updated_at": time.time(),
                "generated_at": data.get("generated_at") if isinstance(data, dict) else None,
            }

    def _load_extras_memory(self):
        """启动时从本地缓存恢复别名/BPM/翻译数据（不刷新更新时间戳）。"""
        path_map = {
            "translation": self.music_dir / "translation_music.json",
            "alias": self.music_dir / "alias.json",
            "bpm": self.music_dir / "bpms.json",
        }
        for key, path in path_map.items():
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._store_extra(key, data, update_meta=False)
            except Exception as e:
                logger.warning(f"[PJSK Wordle] 加载本地 {key} 数据失败: {e}")

    # ---------- 派生题库构建 ----------

    def _build_derived(self, server: str, parsed: dict) -> list[dict]:
        musics = parsed.get("musics.json", [])
        diffs = parsed.get("musicDifficulties.json", [])
        vocals = parsed.get("musicVocals.json", [])
        artists = parsed.get("musicArtists.json", [])
        events = parsed.get("events.json", [])
        event_musics = parsed.get("eventMusics.json", [])

        # 解析 WorldLink（world_bloom 活动类型）关联的歌曲 ID 集合
        wl_event_ids = {e.get("id") for e in events if e.get("eventType") == "world_bloom" and e.get("id") is not None}
        wl_music_ids = {em.get("musicId") for em in event_musics if em.get("eventId") in wl_event_ids and em.get("musicId") is not None}

        master_level: dict[int, int] = {}
        append_ids = set()
        for d in diffs:
            mid = d.get("musicId")
            name = d.get("musicDifficulty")
            if mid is None:
                continue
            if name == "master":
                level = d.get("playLevel")
                if isinstance(level, int):
                    master_level[mid] = level
            elif name == "append":
                append_ids.add(mid)

        vocals_by_music: dict[int, list[dict]] = {}
        for v in vocals:
            mid = v.get("musicId")
            if mid is not None:
                vocals_by_music.setdefault(mid, []).append(v)

        artist_by_id = {a.get("id"): a.get("name") for a in artists if a.get("id") is not None}

        tz = _SERVER_TZ.get(server, timezone.utc)
        result: list[dict] = []
        for m in musics:
            mid = m.get("id")
            title = self._clean_text(m.get("title"))
            if mid is None or not title:
                continue

            published_at = m.get("publishedAt") or m.get("releasedAt")
            if isinstance(published_at, (int, float)) and published_at > 0:
                dt = datetime.fromtimestamp(published_at / 1000, tz=tz)
                date_str = dt.strftime("%Y-%m-%d")
            else:
                date_str = None

            # 作者：国服 musics 内嵌 infos.creator；日服用 musicArtists 关联表
            artist = None
            infos = m.get("infos")
            if isinstance(infos, list) and infos:
                artist = infos[0].get("creator")
            if not artist:
                artist = artist_by_id.get(m.get("creatorArtistId"))
            if not artist:
                artist = m.get("composer") or m.get("lyricist") or "未知"
            artist = self._clean_text(artist)

            bpm = self.bpm_by_id.get(mid)
            if bpm is None:
                bpm = self.bpm_by_title.get(title)

            aliases = list(self.alias_by_id.get(mid, []))
            if not aliases:
                aliases = list(self.alias_by_title.get(title, []))

            cn = self._clean_text(self.cn_by_title.get(title)) or title

            result.append(
                {
                    "id": mid,
                    "title": title,
                    "cn": cn,
                    "aliases": aliases,
                    "category": self._derive_category(
                        vocals_by_music.get(mid, []),
                        server,
                        bool(m.get("isNewlyWrittenMusic")),
                        is_world_link=(mid in wl_music_ids),
                    ),
                    "artist": artist,
                    "date": date_str,
                    "bpm": bpm,
                    "master": master_level.get(mid),
                    "append": mid in append_ids,
                    # 是否为书下曲（書き下ろし，为游戏全新创作的曲目）
                    "newly_written": bool(m.get("isNewlyWrittenMusic")),
                    # 曲绘资源名（jacket 图：{base}/music/jacket/{name}/{name}.png）
                    "jacket": self._clean_text(m.get("assetbundleName")),
                }
            )
        return result

    @staticmethod
    def _clean_text(value: str | None) -> str | None:
        """清洗数据文本：去首尾空白，内部换行/制表符折叠为空格。

                翻译源的部分译名带尾部换行（如 "SAIRAI
        "），直接进入棋盘渲染
                会让 Pillow 的 textlength 抛出多行文本异常。
        """
        if not value:
            return value
        return " ".join(str(value).split())

    @staticmethod
    def _derive_category(
        vocal_entries: list[dict],
        server: str,
        newly_written: bool,
        is_world_link: bool = False,
    ) -> str:
        """根据 vocals 中出现的角色以及活动类型推导单元分类。

        规则：
        1. 若歌曲对应 WorldLink（world_bloom 活动类型）活动曲 → 一律归类为 "WorldLink"；
        2. 有 セカイver.（musicVocalType == "sekai"）→ 以 sekai 版登场角色为准
           - 若登场角色仅包含单一实体团体（或加虚拟歌手）→ 归类为该具体团体；
           - 若包含多个不同实体团体角色 → 归类为 "多人vocal"；
        3. 无セカイver 且为游戏原创曲（isNewlyWrittenMusic）→ 以 original_song 登场角色判定单元；
        4. 无セカイver 的翻唱曲 → 一律归虚拟歌手。
        """
        if is_world_link:
            return "WorldLink"

        sekai_vocals = [v for v in vocal_entries if v.get("musicVocalType") == "sekai"]
        if not sekai_vocals and not newly_written:
            # 无セカイver 的翻唱曲 → 虚拟歌手
            return _unit_display("virtual_singer", server)
        if sekai_vocals:
            search_pool = sekai_vocals
        else:
            search_pool = [
                v for v in vocal_entries if v.get("musicVocalType") == "original_song"
            ] or vocal_entries
        unit_keys: set[str] = set()
        has_virtual = False
        for vocal in sorted(search_pool, key=lambda x: x.get("id") or 0):
            for character in vocal.get("characters", []):
                cid = character.get("characterId")
                if cid is None:
                    continue
                if 1 <= cid <= 20:
                    unit_key = _UNIT_BY_CHAR.get(cid)
                    if unit_key:
                        unit_keys.add(unit_key)
                elif 21 <= cid <= 26:
                    has_virtual = True

        # 只有多个不同实体团体共同演唱时才归为多人 vocal；
        # 单一团体与虚拟歌手混合仍归该实体团体。
        if len(unit_keys) > 1:
            return "多人vocal"
        if unit_keys:
            return _unit_display(next(iter(unit_keys)), server)

        # 无 sekai 版本的翻唱曲，其 VS vocal 角色不代表所属团体。
        if not sekai_vocals:
            for vocal in vocal_entries:
                if any(21 <= (c.get("characterId") or 0) <= 26 for c in vocal.get("characters", [])):
                    has_virtual = True
        if has_virtual:
            return _unit_display("virtual_singer", server)
        return _UNKNOWN_CATEGORY

    # ---------- 本地加载 ----------

    def _load_meta(self):
        if not self.meta_path.exists():
            self.meta = {}
            return
        try:
            self.meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[PJSK Wordle] meta.json 解析失败，将重建: {e}")
            self.meta = {}

    def _save_meta(self):
        self.music_dir.mkdir(parents=True, exist_ok=True)
        self.meta_path.write_text(
            json.dumps(self.meta, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def _load_derived(self, server: str) -> bool:
        path = self.derived_path[server]
        if not path.exists():
            return False
        try:
            songs = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(songs, list) or not songs:
                return False
            self.songs[server] = songs
            return True
        except Exception as e:
            logger.warning(f"[PJSK Wordle] 加载 {SERVER_LABELS[server]}派生题库失败: {e}")
            return False

    # ---------- 网络层 ----------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.request_timeout),
                headers={
                    "User-Agent": "astrbot_plugin_pjsk_wordle (+https://github.com/AstrBotDevs/AstrBot)"
                },
            )
        return self._session

    async def fetch_bytes(self, url: str) -> bytes | None:
        """下载字节数据（图片等），失败返回 None。"""
        try:
            return await self._fetch_url(url)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[PJSK Wordle] 下载资源失败 {url}: {e}")
            return None

    async def _fetch_url(self, url: str) -> bytes:
        session = await self._get_session()
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def _fetch_github_contents(self, repo: str, remote_path: str) -> tuple[bytes, str | None]:
        """通过 GitHub Contents API 拉取文件，返回 (内容, blob sha)。

        失败（网络/限流/文件超过 1MB 无法通过该接口返回）时回退 jsDelivr，
        此时 sha 为 None。
        """
        api_url = f"https://api.github.com/repos/{repo}/contents/{remote_path}?ref={_GITHUB_BRANCH}"
        try:
            session = await self._get_session()
            async with session.get(api_url) as resp:
                if resp.status == 200:
                    payload = await resp.json()
                    content = payload.get("content")
                    encoding = payload.get("encoding", "base64")
                    sha = payload.get("sha")
                    if content and encoding == "base64":
                        return base64.b64decode(content), sha
                    # content 为空通常意味着文件过大，走回退
                    logger.warning(
                        f"[PJSK Wordle] GitHub API 未返回 {repo}/{remote_path} 的内容（可能超过 1MB），回退 jsDelivr"
                    )
                elif resp.status == 403:
                    logger.warning(
                        "[PJSK Wordle] GitHub API 限流(403)，本次回退 jsDelivr。"
                        "提示: 匿名限额 60 次/小时/IP，可等待配额恢复。"
                    )
                else:
                    logger.warning(
                        f"[PJSK Wordle] GitHub API 返回 {resp.status}，回退 jsDelivr: {repo}/{remote_path}"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                f"[PJSK Wordle] GitHub API 拉取失败({repo}/{remote_path}): {e}，回退 jsDelivr"
            )

        # jsDelivr 回退
        jsd_url = _JSDELIVR_TPL.format(repo=repo, branch=_GITHUB_BRANCH, path=remote_path)
        data = await self._fetch_url(jsd_url)
        return data, None
