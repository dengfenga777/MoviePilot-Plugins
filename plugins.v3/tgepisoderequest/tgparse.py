"""从群聊自然语言中识别求片请求：TMDB ID / 季 / 集 / 意图。

支持的说法示例：
    tmdb 12345 缺第5集
    tmdbid:12345 S02E05 资源有问题
    https://www.themoviedb.org/tv/12345 第二季 3-5集 花屏
    12345 第3、7、9集没有
    tmdb=12345 s1 e3 e4 字幕错了
    tmdb 550 电影 资源有问题 / tmdb movie 550 / https://www.themoviedb.org/movie/550
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 明确说是电影（没有季/集时才生效）
MOVIE_WORDS = re.compile(r"电影|影片|剧场版|\bmovie\b|\bfilm\b", re.I)

# 意图关键词
REPLACE_WORDS = r"(有问题|问题|坏了|损坏|花屏|卡顿|没声音|无声|音画不同步|不同步|字幕(错|不对|有问题|缺失|没有)|" \
                r"错集|错了|放错|播不了|不能播|无法播放|打不开|画质差|糊|洗版|替换|重新下|重下|换个|换一个)"
MISSING_WORDS = r"(缺|少了?|没有|没更|未更|漏了?|补一?下|补集|求|找不到|更新)"

TMDB_PATTERNS = [
    re.compile(r"themoviedb\.org/(?P<kind>tv|movie)/(?P<id>\d+)", re.I),
    re.compile(r"tmdb\s*(?:id)?\s*[/:：=#]?\s*(?P<kind>tv|movie)\s*[/:：=#]?\s*(?P<id>\d{2,9})", re.I),
    re.compile(r"(?<![A-Za-z])(?P<kind>tv|movie)\s*/\s*(?P<id>\d{2,9})", re.I),
    re.compile(r"tmdb\s*(?:id)?\s*[:：=#]?\s*(?P<id>\d{2,9})", re.I),
]
# 不写 tmdb 的纯数字 ID：必须在消息开头，后面不能紧跟单位，消息也不能太长（可在配置中关闭）
_UNITS = "%年月日号块元个次分秒岁楼层万千百集季话期部"
BARE_ID = re.compile(rf"^\s*(?P<id>\d{{3,9}})(?![\d.A-Za-z{_UNITS}])(?!\s*(?:[{_UNITS}]|[GgKkMmTtPp](?![A-Za-z\d])))")
BARE_MAX_LEN = 40
# 常见分辨率，单独出现时多半不是 ID
BARE_IGNORE = {480, 576, 720, 1080, 1440, 2160, 4320}

CN_N = r"[0-9零一二两三四五六七八九十]+"

# 自动识别（不带 /mp）时，太长、多行或提到多个 TMDB ID 的消息多半是公告 / 教程，不处理
AUTO_MAX_LEN = 120
AUTO_MAX_LINES = 3


def is_chatter(text: str) -> bool:
    text = (text or "").strip()
    if len(text) > AUTO_MAX_LEN or len([l for l in text.splitlines() if l.strip()]) > AUTO_MAX_LINES:
        return True
    ids = {m.group("id") for p in TMDB_PATTERNS for m in p.finditer(text)}
    return len(ids) > 1


def cn2int(s: str) -> int:
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if "十" in s:
        a, _, b = s.partition("十")
        return (CN_NUM.get(a, 1) if a else 1) * 10 + (CN_NUM.get(b, 0) if b else 0)
    n = 0
    for ch in s:
        n = n * 10 + CN_NUM.get(ch, 0)
    return n


@dataclass
class Request:
    tmdbid: int
    season: Optional[int] = None
    episodes: List[int] = field(default_factory=list)
    intent: str = "missing"  # missing | replace
    is_movie: bool = False

    def describe(self) -> str:
        if self.is_movie:
            return f"TMDB {self.tmdbid}（电影）"
        s = f"S{self.season:02d}" if self.season is not None else "S??"
        eps = ",".join(f"E{e:02d}" for e in self.episodes) or "全季"
        return f"TMDB {self.tmdbid} {s} {eps}"


def _expand(a: int, b: int) -> List[int]:
    if b < a:
        a, b = b, a
    if b - a > 200:  # 防止离谱区间
        return [a, b]
    return list(range(a, b + 1))


def parse(text: str, allow_bare_id: bool = True) -> Optional[Request]:
    if not text:
        return None
    raw = text
    text = text.replace("～", "~").replace("—", "-").replace("–", "-").replace("至", "-").replace("到", "-")

    # ---------- TMDB ID ----------
    tmdbid, is_movie, is_tv, id_span = None, False, False, None
    for p in TMDB_PATTERNS:
        m = p.search(text)
        if m:
            tmdbid = int(m.group("id"))
            kind = (m.groupdict().get("kind") or "").lower()
            is_movie, is_tv = kind == "movie", kind == "tv"
            id_span = m.span()
            break
    bare = False
    if tmdbid is None and allow_bare_id and len(raw.strip()) <= BARE_MAX_LEN:
        m = BARE_ID.search(text)
        if m and int(m.group("id")) not in BARE_IGNORE:
            tmdbid, id_span, bare = int(m.group("id")), m.span("id"), True
    if tmdbid is None:
        return None

    # 把 ID 部分抹掉，避免被当成集数
    rest = text[:id_span[0]] + " " + text[id_span[1]:]

    season: Optional[int] = None
    episodes: List[int] = []

    # S02E05 / S2E5-E8 / S02E05E06
    for m in re.finditer(r"S(\d{1,2})\s*((?:E\d{1,4}\s*(?:-\s*E?\d{1,4})?\s*)+)", rest, re.I):
        season = int(m.group(1))
        for em in re.finditer(r"E(\d{1,4})(?:\s*-\s*E?(\d{1,4}))?", m.group(2), re.I):
            a = int(em.group(1))
            episodes += _expand(a, int(em.group(2))) if em.group(2) else [a]
    rest = re.sub(r"S\d{1,2}\s*(?:E\d{1,4}\s*(?:-\s*E?\d{1,4})?\s*)+", " ", rest, flags=re.I)

    # 季：第二季 / 第2季 / S2 / Season 2
    if season is None:
        m = re.search(rf"第\s*({CN_N})\s*季|(?<![A-Za-z])S(\d{{1,2}})(?![\d])|season\s*(\d{{1,2}})", rest, re.I)
        if m:
            season = cn2int(m.group(1)) if m.group(1) else int(m.group(2) or m.group(3))
            rest = rest[:m.start()] + " " + rest[m.end():]

    # 单独的 E05 / EP05 / E3-E5
    for m in re.finditer(r"(?<![A-Za-z])EP?(\d{1,4})(?:\s*-\s*EP?(\d{1,4}))?(?!\d)", rest, re.I):
        a = int(m.group(1))
        episodes += _expand(a, int(m.group(2))) if m.group(2) else [a]
    rest = re.sub(r"(?<![A-Za-z])EP?\d{1,4}(?:\s*-\s*EP?\d{1,4})?(?!\d)", " ", rest, flags=re.I)

    # 中文集数：第5集 / 第5-8集 / 第3、7、9集 / 5,6集 / 第十二话
    for m in re.finditer(rf"(?:第\s*)?((?:{CN_N}\s*(?:[-~、,，/和与及跟]|\s)\s*)*{CN_N})\s*(?:集|话|話|期)", rest):
        chunk = m.group(1)
        for part in re.split(r"[、,，/和与及跟\s]+", chunk):
            if not part:
                continue
            if "-" in part or "~" in part:
                a, b = re.split(r"[-~]", part, maxsplit=1)
                if a and b:
                    episodes += _expand(cn2int(a), cn2int(b))
            else:
                episodes.append(cn2int(part))

    # ---------- 意图 ----------
    intent = "replace" if re.search(REPLACE_WORDS, raw) else "missing"

    episodes = sorted({e for e in episodes if e > 0})
    if not is_tv and season is None and not episodes and MOVIE_WORDS.search(raw):
        is_movie = True
    if is_movie:
        season, episodes = None, []
    # 裸数字 ID 误触发概率高：必须同时带集数或求片/报错关键词
    if bare and not episodes and not re.search(f"{REPLACE_WORDS}|{MISSING_WORDS}", raw):
        return None
    return Request(tmdbid=tmdbid, season=season, episodes=episodes, intent=intent, is_movie=is_movie)
