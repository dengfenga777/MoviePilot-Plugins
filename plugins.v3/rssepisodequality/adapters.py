"""MoviePilot runtime bridges. Failure always leaves new qBittorrent tasks paused."""
import time
import uuid

from .engine import EmbyLibrary, Library, UncertainLibrary, UnsafeSelection, select_files, quality, file_scope, VIDEO


def query_library(services, media, timeout=10):
    import requests
    if not services:
        raise UncertainLibrary('未配置可用媒体服务器')
    result = Library(servers=tuple(sorted(services)))
    for name, service in services.items():
        if str(service.type).lower() not in ('emby', 'jellyfin'):
            raise UncertainLibrary('所选媒体服务器类型暂不支持严格查重')
        config = service.config.config or {}
        host = str(config.get('host') or '').rstrip('/')
        token = config.get('apikey')
        if not host.startswith(('http://', 'https://')) or not token:
            raise UncertainLibrary('媒体服务器连接配置不完整')
        base = host + ('/' if host.endswith('/emby') else '/emby/' if service.type == 'emby' else '/')
        def request(endpoint, params):
            try:
                response = requests.get(base + endpoint, params=params,
                                        headers={'X-Emby-Token': token}, timeout=timeout)
            except requests.RequestException:
                # Never include URLs, params, tokens or the original exception in plugin logs.
                raise UncertainLibrary('媒体服务器网络请求失败') from None
            if not 200 <= response.status_code < 300:
                raise UncertainLibrary(f'媒体服务器 HTTP {response.status_code}')
            try:
                return response.json()
            except ValueError:
                raise UncertainLibrary('媒体服务器响应不是有效 JSON') from None
        library = EmbyLibrary(request).query(media.media_source.value, str(media.media_id), media.title, media.year)
        for key, versions in library.episodes.items():
            result.episodes.setdefault(key, []).extend(versions)
    return result


def submit_selected(service, content, download_dir, cookie, category, label, season, episodes, upgrade=None, builtin_tag=None):
    """Validate metadata before adding; independently validate live files before starting.

    Refuse existing tasks rather than changing their priorities or restarting them.
    A failure after addition leaves a paused task for inspection (never deletes data).
    """
    from torrentool.api import Torrent
    if service.type != 'qbittorrent':
        raise UnsafeSelection('严格选集目前仅支持 qBittorrent')
    if not isinstance(content, bytes) or content.startswith(b'magnet:'):
        raise UnsafeSelection('缺少可验证的 torrent 文件，拒绝磁力整包回退')
    torrent = Torrent.from_string(content)
    torrent_hash = torrent.info_hash
    preflight = [{'id': i, 'name': file.name} for i, file in enumerate(torrent.files)]
    enabled, _ = select_files(preflight, season, episodes)
    if upgrade and upgrade.get("episodes"):
        expected = upgrade.get("quality")
        axes = upgrade.get("axes")
        for file in preflight:
            if file['id'] not in enabled or __import__('pathlib').PurePosixPath(file['name']).suffix.lower() not in VIDEO:
                continue
            _, scope = file_scope(file['name'], season)
            if not scope & upgrade['episodes']:
                continue
            advertised = quality(file['name'])
            if (not expected or advertised is None or any(
                    advertised[axis] < expected[axis] or (axis == 2 and not advertised[axis]) for axis in axes)):
                raise UnsafeSelection('种子文件命名画质不足以支持升级判断')
    server = service.instance
    # Confirm both connectivity and absence; empty responses with errors are not absence.
    existing, error = server.get_torrents(ids=torrent_hash)
    if error or existing is None:
        raise UnsafeSelection('下载器任务查询失败')
    if existing:
        raise UnsafeSelection('种子任务已存在，保留现有任务；请检查其选集和暂停状态')
    tag = 'rss-eq-' + uuid.uuid4().hex
    added = False
    started = False
    owned = False
    try:
        state, _ = server.add_torrent(
            content=content, is_paused=True,
            download_dir=service.module.normalize_path(download_dir, service.name),
            tag=([value.strip() for value in label.split(',') if value.strip()] if label
                 else [builtin_tag] if builtin_tag else []) + [tag], cookie=cookie,
            category=category, ignore_category_check=False,
        )
        if not state:
            raise UnsafeSelection('下载器未确认添加成功')
        added = True
        for attempt in range(5):
            tasks, error = server.get_torrents(ids=torrent_hash)
            if error or not tasks or len(tasks) != 1:
                if attempt < 4:
                    time.sleep(1)
                    continue
                raise UnsafeSelection('无法确认新任务身份')
            task = tasks[0]
            if tag not in {value.strip() for value in str(task.get('tags') or '').split(',')}:
                raise UnsafeSelection('任务不属于本次提交')
            owned = True
            if str(task.get('state') or '').lower() in ('pauseddl', 'stoppeddl', 'pausedup', 'stoppedup'):
                break
            if attempt == 4:
                raise UnsafeSelection('新任务未保持暂停，不能安全设置选集')
            time.sleep(1)
        files = server.get_files(torrent_hash, retry=5, interval=1)
        enabled, disabled = select_files(files, season, episodes)
        if disabled and not server.set_files(torrent_hash=torrent_hash, file_ids=disabled, priority=0):
            raise UnsafeSelection('取消非目标文件失败')
        if not server.set_files(torrent_hash=torrent_hash, file_ids=enabled, priority=1):
            raise UnsafeSelection('选择目标文件失败')
        verified = False
        for attempt in range(3):
            actual = server.get_files(torrent_hash)
            if actual:
                check_enabled, check_disabled = select_files(actual, season, episodes)
                if set(check_enabled) == set(enabled) and set(check_disabled) == set(disabled) and all(
                        isinstance(f.get('priority'), int) and
                        ((f['priority'] > 0) if f['id'] in enabled else (f['priority'] == 0))
                        for f in actual):
                    verified = True
                    break
            if attempt < 2:
                time.sleep(1)
        if not verified:
            raise UnsafeSelection('文件优先级复核失败，保留暂停任务')
        layout = server.get_content_layout()
        if not layout:
            raise UnsafeSelection('无法确认下载目录布局')
        if not server.start_torrents(torrent_hash):
            raise UnsafeSelection('启动选集任务失败')
        started = True
        return service.name, torrent_hash, layout, f'严格选集成功：{sorted(episodes)}'
    finally:
        if owned and not started:
            # This is only our newly-added task; never touches a task found before addition.
            server.stop_torrents(torrent_hash)
        if owned and started:
            server.delete_torrents_tag(torrent_hash, tag)
