"""PJSK Wordle 游戏核心逻辑。

比对规则：
- 字符串型（曲名、分类、作者、APPEND）：精确匹配 → 绿色，否则深色
- 数值型（上线时间、BPM、MASTER）：精确匹配 → 绿色；相近 → 橙色；否则深色
- 方向箭头（仅有序属性）：↑ 答案更晚/更高，↓ 答案更早/更低
计分规则：把最大次数四等分，由快到慢分别得 4/3/2/1 分（默认 8 次：
第 1-2 次得 4 分，3-4 次得 3 分，5-6 次得 2 分，7-8 次得 1 分），未猜对不得分。
"""

import unicodedata
from datetime import date, datetime

COLOR_GREEN = "green"
COLOR_ORANGE = "orange"
COLOR_DARK = "dark"

ARROW_UP = "up"  # 答案更晚 / 更高
ARROW_DOWN = "down"  # 答案更早 / 更低

MAX_GUESSES = 8

# 数值"相近"的默认判定阈值
DEFAULT_CLOSE_DAYS = 180  # 上线时间相差天数
DEFAULT_CLOSE_BPM = 10  # BPM 相差
DEFAULT_CLOSE_MASTER = 1  # MASTER 难度相差


# 匹配时忽略的标点（统一作用在索引与查询上，不区分歌曲本身）
_IGNORED_PUNCTUATION = set("。．，,、！!？?·•\"'`‘’“”…「」『』()（）【】[]")


def normalize_text(text: str) -> str:
    """标准化玩家输入/歌曲名：小写、全角转半角、去空白与常见标点。

    用于曲名与别名的匹配，使 "ＲＯＫＩ"、"ro ki"、"ロキ　"、"命に嫌われている。"
    等输入都能正确命中。
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", str(text))
    normalized = normalized.lower()
    return "".join(ch for ch in normalized if not ch.isspace() and ch not in _IGNORED_PUNCTUATION)


def score_for_guess_count(guess_count: int, max_guesses: int = MAX_GUESSES) -> int:
    """按猜对所用次数计分（随自定义最大次数按比例缩放）。

    把 1~max_guesses 四等分：最快的一档得 4 分，其后依次 3/2/1 分。
    max_guesses=8 时即 第 1-2 次→4 分、3-4 次→3 分、5-6 次→2 分、7-8 次→1 分。
    """
    if guess_count <= 0 or max_guesses <= 0 or guess_count > max_guesses:
        return 0
    quarter = max_guesses / 4
    tier = min(int((guess_count - 1) / quarter), 3)
    return 4 - tier


def parse_date(date_str: str | None) -> date | None:
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _bigrams(text: str) -> frozenset:
    """字符二元组集合（长度不足 2 时退化为单字符集合）。"""
    if len(text) < 2:
        return frozenset(text)
    return frozenset(text[i : i + 2] for i in range(len(text) - 1))


def _dice(a: frozenset, b: frozenset) -> float:
    """二元组 Dice 系数：2|A∩B| / (|A|+|B|)，取值 0~1。"""
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def _edit_distance_at_most(a: str, b: str, max_dist: int) -> int | None:
    """计算 a、b 的编辑距离（插入/删除/替换，每个字符计 1）。

    超过 max_dist 时提前剪枝返回 None；差值本身已超限时直接短路。
    """
    if abs(len(a) - len(b)) > max_dist:
        return None
    if a == b:
        return 0
    if len(a) > len(b):
        a, b = b, a
    prev = list(range(len(a) + 1))
    for j, cb in enumerate(b, 1):
        cur = [j]
        row_min = j
        for i, ca in enumerate(a, 1):
            cost = 0 if ca == cb else 1
            value = min(prev[i - 1] + cost, prev[i] + 1, cur[i - 1] + 1)
            cur.append(value)
            if value < row_min:
                row_min = value
        if row_min > max_dist:
            return None
        prev = cur
    distance = prev[-1]
    return distance if distance <= max_dist else None


class SongMatcher:
    """曲名/别名 -> 歌曲的模糊匹配器。

    匹配按优先级依次尝试：
    1. 精确匹配：规范化（小写/全半角/去空白与常见标点）后完全一致；
    2. 编辑距离容错：长度 ≥4 允许 1 个字符差错（漏字/多字/错字均可，
       如把"25时的情热"打成"25时的情熟"或少打成"25时的情"），长度 ≥8 放宽到 2 个；
       多个候选时按 编辑距离小 > 曲名精确一致 > id 小 排序；
    3. 包含匹配：输入与某个别名互为包含（如只记得半个名字），
       仅当唯一命中一首歌时才接受，存在歧义则不匹配；
    4. 兜底相似匹配（always_match=True 时启用）：对全库做字符二元组
       重合度（Dice）+ 包含加成 + 精确编辑距离复核的综合评分，返回得分
       最高的歌曲——保证 @机器人 的任何回答都至少匹配出一首。
    过短的输入（<4 字符）不参与第 2/3 层，但仍会走兜底匹配。
    """

    FUZZY_MIN_LENGTH = 4  # 编辑距离/包含层生效的最短输入长度
    FUZZY_MAX_DISTANCE = 1  # 默认容错字符数
    FUZZY_MAX_DISTANCE_LONG = 2  # 长输入的容错字符数
    FUZZY_LONG_LENGTH = 8  # 触发放大容错的输入长度
    CONTAIN_SCORE = 0.85  # 包含关系的相似度加成
    REFINE_TOP_N = 30  # 兜底层精修的候选数量

    def __init__(self, songs: list[dict], always_match: bool = True):
        self.songs = songs
        self.always_match = always_match
        self._index: dict[str, list[int]] = {}
        for song in songs:
            names = {song.get("title"), song.get("cn"), *(song.get("aliases") or [])}
            for name in names:
                if not name:
                    continue
                key = normalize_text(name)
                if not key:
                    continue
                self._index.setdefault(key, []).append(song["id"])
        self._by_id = {song["id"]: song for song in songs}
        # 预计算每个键的字符二元组集合（兜底评分用）
        self._entries: list[tuple[str, tuple[int, ...], frozenset]] = [
            (key, tuple(ids), _bigrams(key)) for key, ids in self._index.items()
        ]

    def _select(self, song_ids: set[int], query_key: str) -> dict:
        """多个候选时：曲名/中文名与查询完全一致者优先，其次 id 升序稳定排序。"""

        def rank(song_id: int) -> tuple:
            song = self._by_id[song_id]
            exact = query_key in {
                normalize_text(song.get("title") or ""),
                normalize_text(song.get("cn") or ""),
            }
            return (0 if exact else 1, song_id)

        return self._by_id[min(song_ids, key=rank)]

    def _fuzzy_max_distance(self, key: str) -> int:
        if len(key) >= self.FUZZY_LONG_LENGTH:
            return self.FUZZY_MAX_DISTANCE_LONG
        return self.FUZZY_MAX_DISTANCE

    def _fuzzy_find(self, key: str) -> dict | None:
        max_dist = self._fuzzy_max_distance(key)

        # 编辑距离容错：漏字 / 多字 / 错字
        best_distance = max_dist + 1
        best_ids: set[int] = set()
        for indexed_key, id_list in self._index.items():
            if abs(len(indexed_key) - len(key)) > max_dist:
                continue
            distance = _edit_distance_at_most(indexed_key, key, max_dist)
            if distance is None:
                continue
            if distance < best_distance:
                best_distance = distance
                best_ids = set(id_list)
            elif distance == best_distance:
                best_ids.update(id_list)
        if best_ids:
            return self._select(best_ids, key)

        # 包含匹配：只记得半个名字；仅在唯一命中一首歌时接受
        hit_songs: set[int] = set()
        for indexed_key, id_list in self._index.items():
            if len(indexed_key) < self.FUZZY_MIN_LENGTH:
                continue
            if indexed_key in key or key in indexed_key:
                hit_songs.update(id_list)
                if len(hit_songs) > 1:
                    return None if not self.always_match else self._best_effort_find(key)
        if len(hit_songs) == 1:
            return self._by_id[next(iter(hit_songs))]
        if self.always_match:
            return self._best_effort_find(key)
        return None

    def _best_effort_find(self, key: str) -> dict:
        """兜底相似匹配：全库综合评分，返回得分最高的歌曲（保证有结果）。

        评分 = max(字符二元组 Dice 重合度, 包含加成, 归一化编辑距离)，
        编辑距离仅对 Dice 最高的前 REFINE_TOP_N 个候选做精修以控制耗时。
        同分时按 曲名精确一致 > id 小 决出。
        """
        query_bigrams = _bigrams(key)
        scored: list[tuple[float, str, tuple[int, ...]]] = []
        for indexed_key, ids, key_bigrams in self._entries:
            score = _dice(query_bigrams, key_bigrams)
            if indexed_key in key or key in indexed_key:
                ratio = min(len(indexed_key), len(key)) / max(len(indexed_key), len(key))
                score = max(score, self.CONTAIN_SCORE + 0.1 * ratio)
            scored.append((score, indexed_key, ids))
        scored.sort(key=lambda t: -t[0])

        refined: list[tuple[float, str, tuple[int, ...]]] = []
        for score, indexed_key, ids in scored[: self.REFINE_TOP_N]:
            distance = _edit_distance_at_most(indexed_key, key, max(len(indexed_key), len(key)))
            if distance is not None:
                distance_score = 1 - distance / max(len(indexed_key), len(key))
                score = max(score, distance_score)
            refined.append((score, indexed_key, ids))

        refined.sort(key=lambda t: -t[0])
        best_score = refined[0][0]
        best_ids: set[int] = set()
        for score, _, ids in refined:
            if score < best_score:
                break
            best_ids.update(ids)
        return self._select(best_ids, key)

    def find(self, query: str) -> dict | None:
        key = normalize_text(query)
        if not key:
            # 规范化后为空（纯标点/空白）：兜底返回第一首，保证有结果
            if self.always_match and self.songs:
                return self.songs[0]
            return None

        ids = self._index.get(key)
        if ids:
            return self._select(set(ids), key)

        if len(key) >= self.FUZZY_MIN_LENGTH:
            return self._fuzzy_find(key)
        if self.always_match:
            # 短输入不走编辑距离/包含层，但仍保底返回最接近的一首
            return self._best_effort_find(key)
        return None


class WordleGame:
    """一局 Wordle 的完整状态。"""

    def __init__(self, answer: dict, server: str, max_guesses: int = MAX_GUESSES):
        self.answer = answer
        self.server = server
        self.max_guesses = max_guesses
        self.rows: list[list[dict]] = []  # 每次猜测的 7 格反馈
        self.guess_ids: list[int] = []  # 已猜过的歌曲 id
        self.finished = False
        self.won = False
        self.winner_id: str | None = None
        self.winner_name: str = ""
        self.end_reason = ""  # win / fail / quit / timeout

    @property
    def guess_count(self) -> int:
        return len(self.rows)

    @property
    def remaining(self) -> int:
        return self.max_guesses - self.guess_count

    def is_finished(self) -> bool:
        return self.finished

    def guess(self, song: dict, player_id: str = "", player_name: str = "") -> dict:
        """提交一次猜测，返回本局快照信息。

        返回: {"result": "win"|"ongoing"|"fail", "row": [...], "game": self}
        """
        if self.finished:
            return {"result": "finished", "row": None, "game": self}

        row = compare_songs(song, self.answer)
        self.rows.append(row)
        self.guess_ids.append(song["id"])

        if song["id"] == self.answer["id"]:
            self.finished = True
            self.won = True
            self.end_reason = "win"
            self.winner_id = player_id
            self.winner_name = player_name
            return {"result": "win", "row": row, "game": self}

        if self.guess_count >= self.max_guesses:
            self.finished = True
            self.won = False
            self.end_reason = "fail"
            return {"result": "fail", "row": row, "game": self}

        return {"result": "ongoing", "row": row, "game": self}

    def forfeit(self, reason: str = "quit"):
        """中途结束（退出/超时），不计分。"""
        self.finished = True
        self.won = False
        self.end_reason = reason


def compare_songs(
    guess: dict,
    answer: dict,
    close_days: int = DEFAULT_CLOSE_DAYS,
    close_bpm: int = DEFAULT_CLOSE_BPM,
    close_master: int = DEFAULT_CLOSE_MASTER,
) -> list[dict]:
    """逐列比对猜测与答案，返回 8 个格子的反馈。

    每个格子: {"text": 显示文本, "color": green|orange|dark, "arrow": None|up|down}
    列顺序：曲名 / 上线时间 / 是否为书下曲 / 乐曲分类 / 作者 / BPM / MASTER / APPEND
    """
    cells: list[dict] = []

    # 1. 曲名：仅当猜中答案时为绿（猜中即获胜）
    cells.append(
        {
            "text": guess.get("cn") or guess.get("title") or "?",
            "color": COLOR_GREEN if guess.get("id") == answer.get("id") else COLOR_DARK,
            "arrow": None,
        }
    )

    # 2. 上线时间
    guess_date = parse_date(guess.get("date"))
    answer_date = parse_date(answer.get("date"))
    if guess_date and answer_date:
        if guess_date == answer_date:
            color = COLOR_GREEN
        elif abs((answer_date - guess_date).days) <= close_days:
            color = COLOR_ORANGE
        else:
            color = COLOR_DARK
        arrow = _direction(answer_date, guess_date)
    else:
        color, arrow = COLOR_DARK, None
    cells.append({"text": guess.get("date") or "?", "color": color, "arrow": arrow})

    # 3. 是否为书下曲（是/否，精确匹配）
    gn, an = bool(guess.get("newly_written")), bool(answer.get("newly_written"))
    equal = guess.get("newly_written") is not None and gn == an
    cells.append(
        {"text": "是" if gn else "否", "color": COLOR_GREEN if equal else COLOR_DARK, "arrow": None}
    )

    # 4. 乐曲分类（精确匹配）
    equal = guess.get("category") is not None and guess.get("category") == answer.get("category")
    cells.append(
        {
            "text": guess.get("category") or "?",
            "color": COLOR_GREEN if equal else COLOR_DARK,
            "arrow": None,
        }
    )

    # 5. 作者（精确匹配）
    equal = guess.get("artist") is not None and guess.get("artist") == answer.get("artist")
    cells.append(
        {
            "text": guess.get("artist") or "?",
            "color": COLOR_GREEN if equal else COLOR_DARK,
            "arrow": None,
        }
    )

    # 6. BPM（数值比较）
    gb, ab = guess.get("bpm"), answer.get("bpm")
    if gb is not None and ab is not None:
        if float(gb) == float(ab):
            color = COLOR_GREEN
        elif abs(float(ab) - float(gb)) <= close_bpm:
            color = COLOR_ORANGE
        else:
            color = COLOR_DARK
        arrow = _direction(float(ab), float(gb))
        cells.append({"text": _format_bpm(gb), "color": color, "arrow": arrow})
    else:
        cells.append({"text": _format_bpm(gb), "color": COLOR_DARK, "arrow": None})

    # 7. MASTER 难度（数值比较）
    gm, am = guess.get("master"), answer.get("master")
    if gm is not None and am is not None:
        if int(gm) == int(am):
            color = COLOR_GREEN
        elif abs(int(am) - int(gm)) <= close_master:
            color = COLOR_ORANGE
        else:
            color = COLOR_DARK
        arrow = _direction(int(am), int(gm))
        cells.append({"text": str(gm), "color": color, "arrow": arrow})
    else:
        cells.append({"text": "?" if gm is None else str(gm), "color": COLOR_DARK, "arrow": None})

    # 8. APPEND（布尔精确匹配）
    ga, aa = bool(guess.get("append")), bool(answer.get("append"))
    equal = guess.get("append") is not None and ga == aa
    cells.append(
        {
            "text": "有" if ga else "无",
            "color": COLOR_GREEN if equal else COLOR_DARK,
            "arrow": None,
        }
    )

    return cells


def _direction(answer_value, guess_value) -> str | None:
    """箭头方向：答案更大/更晚 → up(↑)，答案更小/更早 → down(↓)。相等则无箭头。"""
    if answer_value > guess_value:
        return ARROW_UP
    if answer_value < guess_value:
        return ARROW_DOWN
    return None


def _format_bpm(bpm) -> str:
    if bpm is None:
        return "?"
    value = float(bpm)
    if value.is_integer():
        return str(int(value))
    return f"{value:g}"


class GameService:
    """面向插件主流程的游戏服务：匹配器管理与比对封装。"""

    def __init__(
        self,
        close_days: int = DEFAULT_CLOSE_DAYS,
        close_bpm: int = DEFAULT_CLOSE_BPM,
        close_master: int = DEFAULT_CLOSE_MASTER,
        always_match: bool = True,
    ):
        self.close_days = close_days
        self.close_bpm = close_bpm
        self.close_master = close_master
        self.always_match = always_match
        self._matchers: dict[str, SongMatcher] = {}

    def update_songs(self, server: str, songs: list[dict]):
        """题库更新后重建对应服务器的匹配器。"""
        self._matchers[server] = SongMatcher(songs, always_match=self.always_match)

    def get_matcher(self, server: str) -> SongMatcher | None:
        return self._matchers.get(server)

    def find_song(self, server: str, query: str) -> dict | None:
        matcher = self._matchers.get(server)
        if not matcher:
            return None
        return matcher.find(query)

    def compare(self, guess: dict, answer: dict) -> list[dict]:
        return compare_songs(guess, answer, self.close_days, self.close_bpm, self.close_master)
