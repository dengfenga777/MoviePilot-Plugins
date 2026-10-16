"""从 MoviePilot 搜索结果（Context 列表）里为某一集挑候选。纯函数，不依赖 MoviePilot。"""
from __future__ import annotations

import re
from typing import Any, List, Optional


def episodes_of(ctx: Any) -> Optional[List[int]]:
    """种子包含的集数；None 表示整季包或认不出集数。"""
    meta = getattr(ctx, "meta_info", None)
    eps = list(getattr(meta, "episode_list", None) or [])
    if eps:
        return sorted(eps)
    b, e = getattr(meta, "begin_episode", None), getattr(meta, "end_episode", None)
    if b:
        return list(range(b, (e or b) + 1))
    title = getattr(getattr(ctx, "torrent_info", None), "title", "") or ""
    m = re.search(r"S\d{1,2}E(\d{1,4})(?:-?E(\d{1,4}))?", title, re.I)
    if m:
        a = int(m.group(1))
        return list(range(a, int(m.group(2) or a) + 1))
    return None


def season_of(ctx: Any) -> Optional[int]:
    return getattr(getattr(ctx, "meta_info", None), "begin_season", None)


def covers(ctx: Any, episode: int) -> bool:
    eps = episodes_of(ctx)
    return eps is None or episode in eps


def pick(results: List[Any], season: Optional[int], episode: Optional[int],
         allow_pack: bool = True, min_seeders: int = 1, limit: int = 3) -> List[Any]:
    """排序：恰好单集 > 含该集的多集包 > 整季包；组内保持 MoviePilot 的优先级顺序。"""
    buckets: List[List[Any]] = [[], [], []]
    for ctx in results:
        torrent = getattr(ctx, "torrent_info", None)
        if (getattr(torrent, "seeders", 0) or 0) < min_seeders:
            continue
        s = season_of(ctx)
        if season is not None and s is not None and s != season:
            continue
        if episode is None:
            buckets[0].append(ctx)
            continue
        eps = episodes_of(ctx)
        if eps is None:
            if allow_pack:
                buckets[2].append(ctx)
        elif eps == [episode]:
            buckets[0].append(ctx)
        elif episode in eps:
            buckets[1].append(ctx)
    return (buckets[0] + buckets[1] + buckets[2])[:limit]


# 标题末尾的发布组后缀（如 -XXWEB、@XXWEB），很多是站点自制组，会暴露来源站点
_GROUP_SUFFIX = re.compile(r"[-@]([A-Za-z0-9]+)$")
_NOT_GROUP = {"dl", "rip", "hd", "hdr", "sdr", "dv", "4k", "10bit", "x264", "x265", "h264", "h265", "hevc", "avc"}


def clean_title(title: str) -> str:
    """去掉资源标题末尾的发布组后缀，避免在群里暴露站点。"""
    title = (title or "").strip()
    m = _GROUP_SUFFIX.search(title)
    if m and m.group(1).lower() not in _NOT_GROUP:
        title = title[:m.start()].rstrip(" .-_")
    return title


def brief(ctx: Any) -> str:
    t = getattr(ctx, "torrent_info", None)
    size = getattr(t, "size", 0) or 0
    gb = f"{size / 1024 ** 3:.2f}G" if size else "?"
    eps = episodes_of(ctx)
    if eps is None:
        tag = "整季"
    elif len(eps) == 1:
        tag = f"E{eps[0]:02d}"
    else:
        tag = f"E{eps[0]:02d}-E{eps[-1]:02d}"
    dvf = getattr(t, "downloadvolumefactor", None)
    free = " 🆓" if dvf == 0 else (f" {int(dvf * 100)}%" if dvf is not None and dvf < 1 else "")
    return f"{tag} {gb} ⬆{getattr(t, 'seeders', 0) or 0}{free}\n{clean_title(getattr(t, 'title', ''))}"
