"""Pure policy and validated Emby-family HTTP queries; no MoviePilot state or side effects."""
from dataclasses import dataclass, field
from pathlib import PurePosixPath
import re
from typing import Callable


class UncertainLibrary(RuntimeError):
    pass


class UnsafeSelection(RuntimeError):
    pass


@dataclass
class Library:
    # All versions of an episode must participate in comparisons.
    episodes: dict = field(default_factory=dict)
    servers: tuple = ()

    def coverage(self):
        result = {}
        for season, episode in self.episodes:
            result.setdefault(str(season), []).append(episode)
        return {s: sorted(set(e)) for s, e in result.items()}


class EmbyLibrary:
    """Treat only validated 2xx JSON responses as successful lookups.

    Search by provider ID, validate every hit and union duplicate Series IDs.
    A title fallback catches items without provider IDs, which are uncertain.
    """
    def __init__(self, request: Callable, page_size=200):
        self.request = request
        self.page_size = page_size

    def items(self, endpoint, params):
        all_items = []
        start = 0
        expected_total = None
        while True:
            payload = self.request(endpoint, {**params, 'StartIndex': start, 'Limit': self.page_size})
            if not isinstance(payload, dict) or not isinstance(payload.get('Items'), list):
                raise UncertainLibrary('媒体库响应缺少 Items 列表')
            total = payload.get('TotalRecordCount')
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise UncertainLibrary('媒体库响应缺少有效 TotalRecordCount')
            if expected_total is not None and expected_total != total:
                raise UncertainLibrary('分页期间媒体库数量变化')
            expected_total = total
            items = payload['Items']
            if len(items) > self.page_size or start + len(items) > total:
                raise UncertainLibrary('媒体库分页数量不一致')
            if any(not isinstance(item, dict) or not re.fullmatch(r'[A-Za-z0-9-]+', str(item.get('Id') or '')) for item in items):
                raise UncertainLibrary('媒体库返回无效条目')
            all_items.extend(items)
            start += len(items)
            if start == total:
                return all_items
            if not items or start > 20000:
                raise UncertainLibrary('媒体库分页不完整或超过保护上限')

    @staticmethod
    def identity(item, source):
        ids = item.get('ProviderIds')
        if not isinstance(ids, dict):
            return None
        aliases = {'tmdb': 'tmdb', 'imdb': 'imdb', 'tvdb': 'tvdb', 'douban': 'douban',
                   'bangumi': 'bangumi', 'anilist': 'anilist'}
        key = aliases.get(source.lower())
        if not key:
            raise UncertainLibrary('不支持的媒体来源')
        return next((str(value).strip() for name, value in ids.items()
                     if name.lower() == key and value is not None), None)

    def query(self, source, media_id, title, year):
        source = {'themoviedb': 'tmdb', 'thetvdb': 'tvdb'}.get(str(source).lower(), str(source).lower())
        common = {'IncludeItemTypes': 'Series', 'Recursive': 'true',
                  'Fields': 'ProviderIds,ProductionYear,Path', 'IsVirtual': 'false'}
        hits = self.items('Items', {**common, 'AnyProviderIdEquals': f'{source}.{media_id}'})
        matched = {}
        for hit in hits:
            if self.identity(hit, source) != str(media_id):
                raise UncertainLibrary('媒体库未正确执行媒体ID筛选')
            matched[str(hit['Id'])] = hit
        # Do not silently conclude absence when name-matching entries lack identity.
        named = self.items('Items', {**common, 'SearchTerm': title})
        for hit in named:
            if self.identity(hit, source) == str(media_id):
                matched[str(hit['Id'])] = hit
            elif (hit.get('Name') == title and (not year or str(hit.get('ProductionYear')) == str(year))
                  and self.identity(hit, source) is None):
                raise UncertainLibrary('同名同年份剧集缺少媒体ID，不能确认缺集')
        library = Library()
        for item_id in matched:
            episodes = self.items(f'Shows/{item_id}/Episodes', {
                'IsMissing': 'false', 'IsVirtual': 'false',
                'Fields': 'MediaSources,MediaStreams,Path,ProviderIds',
            })
            for item in episodes:
                season, episode = item.get('ParentIndexNumber'), item.get('IndexNumber')
                end = item.get('IndexNumberEnd', episode)
                if (isinstance(season, bool) or not isinstance(season, int) or season < 0
                        or isinstance(episode, bool) or not isinstance(episode, int) or episode <= 0
                        or isinstance(end, bool) or not isinstance(end, int) or not episode <= end <= episode + 100):
                    raise UncertainLibrary('媒体库剧集缺少可信季集编号')
                if item.get('IsMissing') or item.get('IsVirtualItem'):
                    continue
                for ep in range(episode, end + 1):
                    library.episodes.setdefault((season, ep), []).append(item)
        return library


@dataclass
class Plan:
    selected: set = field(default_factory=set)
    missing: set = field(default_factory=set)
    upgrades: set = field(default_factory=set)
    reasons: dict = field(default_factory=dict)


def decide(requested, season, library, candidate_quality, quality_of, mode='missing', upgrade_axes=(0,)):
    plan = Plan()
    for ep in sorted(set(requested)):
        versions = library.episodes.get((season, ep), [])
        if not versions:
            plan.selected.add(ep)
            plan.missing.add(ep)
            plan.reasons[str(ep)] = '缺集'
            continue
        if mode != 'upgrade':
            plan.reasons[str(ep)] = '媒体库已有'
            continue
        qualities = [quality_of(item) for item in versions]
        # Any unknown version prevents upgrade. No guessed comparison from download history.
        if candidate_quality is None or any(q is None for q in qualities) or any(
                q[axis] < 0 or (axis == 2 and q[axis] == 0)
                for q in [candidate_quality] + [q for q in qualities if q is not None]
                for axis in upgrade_axes):
            plan.reasons[str(ep)] = '实际画质未知，保留已有版本'
            continue
        def project(q):
            return tuple(q[axis] for axis in upgrade_axes)
        # Component-wise dominance prevents a resolution increase from losing HDR, etc.
        current = [project(q) for q in qualities]
        candidate = project(candidate_quality)
        if all(all(a >= b for a, b in zip(candidate, old)) for old in current) and any(
                all(a >= b for a, b in zip(candidate, old)) and candidate != old for old in current
        ) and candidate not in current:
            plan.selected.add(ep)
            plan.upgrades.add(ep)
            plan.reasons[str(ep)] = '实际版本升级'
        else:
            plan.reasons[str(ep)] = '同档或降级，保留已有版本'
    return plan


def _title_quality(title):
    """Single source of truth for resolution/HDR/source parsed from a release name."""
    title = title or ''
    resolution = next((value for pattern, value in [(r'\b(?:2160[pi]|4k|uhd)\b', 2160),
                        (r'\b1080[pi]\b', 1080), (r'\b720[pi]\b', 720)]
                       if re.search(pattern, title, re.I)), 0)
    hdr = int(bool(re.search(r'\b(?:hdr\w*|dv|dovi)\b|dolby[ ._-]?vision', title, re.I)))
    source = 4 if re.search(r'\bremux\b', title, re.I) else (3 if re.search(r'blu[ ._-]?ray|\bb[dr]rip\b', title, re.I)
             else 2 if re.search(r'web[ ._-]?dl', title, re.I) else 1 if re.search(r'web[ ._-]?rip|\bhdtv\b', title, re.I) else 0)
    return resolution, hdr, source


def rank(title, priority=0):
    """Cross-RSS ordering key: MP rule priority first, then the same quality facts used for upgrades.

    Size is deliberately excluded; callers may use it only as a final tie-breaker.
    """
    resolution, hdr, source = _title_quality(title)
    name = title or ''
    fps = int(bool(re.search(r'(?i)\b(?:60|50|120)\s*fps\b', name)))
    hevc = int(bool(re.search(r'(?i)(?:[hx][ ._-]?265|hevc)', name)))
    try:
        priority = int(priority or 0)
    except (TypeError, ValueError):
        priority = 0
    return priority, resolution, source, hdr, fps, hevc


def quality(title, item=None):
    """Resolution/HDR/source from stream facts and filename. Unknown is never low quality."""
    title = title or ''
    resolution, hdr, source = _title_quality(title)
    if item is not None:
        streams = list(item.get('MediaStreams') or [])
        for media_source in item.get('MediaSources') or []:
            streams.extend(media_source.get('MediaStreams') or [])
        video = [s for s in streams if s.get('Type') == 'Video']
        heights = [s.get('Height', 0) or 0 for s in video]
        if heights:
            height = max(heights)
            width = max((s.get('Width', 0) or 0 for s in video), default=0)
            resolution = 2160 if height > 1080 or width > 1920 else 1080 if height > 720 or width > 1280 else 720 if height > 480 else 480
        hdr_known = bool(hdr) or bool(re.search(r'\bsdr\b', title, re.I))
        for stream in video:
            hdr_known |= bool(stream.get('VideoRangeType') or stream.get('VideoRange') or stream.get('DoviProfile'))
            hdr |= int(any(token in str(stream.get('VideoRangeType') or stream.get('VideoRange') or '').lower()
                           for token in ('hdr', 'dolby', 'dovi')) or bool(stream.get('DoviProfile')))
        if not hdr_known:
            hdr = -1
    return (resolution, hdr, source) if resolution else None


VIDEO = {'.mkv', '.mp4', '.avi', '.ts', '.m2ts', '.mov', '.wmv'}
SUBTITLES = {'.srt', '.ass', '.ssa', '.vtt', '.sup'}


# "E05", "EP05", "EP.05", "S01EP05". The separator is only allowed after "EP" so that tokens
# such as "E-AC3" or "HE 10" are never read as episodes.
_EPISODE = r'(?:\b|(?<=\d))(?:EP[ ._]?|E)(\d{1,4})(?!\d)'
_EPISODE_RANGE = r'(?:\b|(?<=\d))(?:EP[ ._]?|E)(\d{1,4})[ ._]*-[ ._]*(?:EP[ ._]?|E)?(\d{1,4})(?!\d)'
# Fansub style, only consulted when nothing explicit matched: "Show - 05 [1080p]", "[Group][Show][05][1080p]".
_ANIME_DASH = r'(?:^|\s)-\s?(\d{1,3})(?:v\d)?(?=\s|\[|\(|\.|$)'
_ANIME_BRACKET = r'\[(\d{1,3})(?:v\d)?(?:\s?(?:END|FIN|完))?\]'
_NOT_EPISODE = {480, 576, 720}


def _episodes_in(text):
    """Explicit episode numbers in one path component (file stem or directory name)."""
    episodes = {int(v) for v in re.findall(r'(?i)' + _EPISODE, text)}
    for begin, end in re.findall(r'(?i)' + _EPISODE_RANGE, text):
        begin, end = int(begin), int(end)
        if end < begin or end - begin > 100:
            raise UnsafeSelection('文件集数范围异常')
        episodes.update(range(begin, end + 1))
    if not episodes:
        episodes = {int(v) for v in re.findall(r'第\s*(\d{1,4})\s*[集话話]', text)}
    if not episodes:
        anime = {int(v) for v in re.findall(_ANIME_DASH, text)} or {
            int(v) for v in re.findall(r'(?i)' + _ANIME_BRACKET, text)}
        anime -= _NOT_EPISODE
        # Several bare numbers in one name are ambiguous (e.g. "[01][1080][02]"); refuse to guess.
        if len(anime) == 1:
            episodes = anime
    return episodes


def file_scope(name, default_season):
    """Only explicit filename episodes count; pack/directory ranges never supply coverage."""
    path = PurePosixPath(name.replace('\\', '/'))
    filename = path.name
    seasons = {int(value) for match in re.findall(r'(?i)\bS(\d{1,3})(?=E|\b)|season[ ._-]*(\d{1,3})', str(path)) for value in match if value}
    if len(seasons) > 1:
        raise UnsafeSelection('文件包含冲突季号')
    season = next(iter(seasons), default_season)
    stem = filename[:-len(path.suffix)] if path.suffix else filename
    episode_numbers = _episodes_in(stem)
    if 0 in episode_numbers:
        raise UnsafeSelection('不支持第0集编号')
    return season, episode_numbers


def subtitle_scope(name, default_season):
    """Subtitles inside a per-episode folder ("Subs/Show.S01E05/2_English.srt") take that folder's episode.

    Only a single explicit episode qualifies; a range folder never supplies coverage.
    """
    season, episodes = file_scope(name, default_season)
    if episodes:
        return season, episodes
    for parent in reversed(PurePosixPath(name.replace('\\', '/')).parent.parts):
        found = _episodes_in(parent)
        if found:
            return season, (found if len(found) == 1 and 0 not in found else set())
    return season, set()


def select_files(files, season, selected):
    if not selected or not files:
        raise UnsafeSelection('选集清单或文件列表为空')
    enabled, disabled, covered = [], [], set()
    seen = set()
    for file in files:
        file_id, name = file.get('id'), file.get('name')
        if isinstance(file_id, bool) or not isinstance(file_id, int) or file_id < 0 or file_id in seen or not isinstance(name, str):
            raise UnsafeSelection('文件ID或文件名异常')
        seen.add(file_id)
        path = PurePosixPath(name.replace('\\', '/'))
        if path.is_absolute() or '..' in path.parts:
            raise UnsafeSelection('文件路径异常')
        suffix = path.suffix.lower()
        if suffix not in VIDEO | SUBTITLES or re.search(r'(?i)(?:^|[/ ._-])(?:sample|trailer)(?:[/ ._-]|$)', name):
            disabled.append(file_id)
            continue
        file_season, episodes = (file_scope if suffix in VIDEO else subtitle_scope)(name, season)
        if suffix in VIDEO and not episodes:
            raise UnsafeSelection('视频文件无法识别明确集数')
        relevant = file_season == season and bool(episodes & selected)
        if relevant and not episodes.issubset(selected):
            raise UnsafeSelection('同一文件混有已有集数与缺集，无法拆分')
        if relevant:
            enabled.append(file_id)
            if suffix in VIDEO:
                covered.update(episodes)
        else:
            disabled.append(file_id)
    if covered != selected:
        raise UnsafeSelection('种子文件不能完整覆盖指定集数')
    return enabled, disabled
