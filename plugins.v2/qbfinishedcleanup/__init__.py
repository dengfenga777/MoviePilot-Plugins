import datetime
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType, ServiceInfo
from app.utils.string import StringUtils

lock = threading.Lock()


class QbFinishedCleanup(_PluginBase):
    # 插件名称
    plugin_name = "qB已整理自动清理"
    # 插件描述
    plugin_desc = "磁盘剩余空间低于阈值时，删除 qB 指定标签的已完成任务和本地文件。"
    # 插件图标
    plugin_icon = "delete.jpg"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "misaya"
    # 作者主页
    author_url = "https://github.com/dengfenga777"
    # 插件配置项ID前缀
    plugin_config_prefix = "qbfinishedcleanup_"
    # 加载顺序
    plugin_order = 22
    # 可使用的用户级别
    auth_level = 2

    _scheduler: Optional[BackgroundScheduler] = None
    _event = threading.Event()

    _enabled: bool = False
    _onlyonce: bool = False
    _notify: bool = False
    _downloaders: List[str] = []
    _cron: str = ""
    _tag: str = "已整理"
    _threshold_gb: str = "500"
    _check_path: str = ""
    _max_delete: str = "10"
    _completed_only: bool = True
    _dry_run: bool = False

    def init_plugin(self, config: dict = None):
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._notify = config.get("notify")
            self._downloaders = config.get("downloaders") or []
            self._cron = config.get("cron")
            self._tag = config.get("tag") or "已整理"
            self._threshold_gb = str(config.get("threshold_gb") or "500")
            self._check_path = config.get("check_path") or ""
            self._max_delete = str(config.get("max_delete") or "10")
            self._completed_only = config.get("completed_only", True)
            self._dry_run = config.get("dry_run", False)

        if self.get_state() or self._onlyonce:
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("qB已整理自动清理服务启动，立即运行一次")
                self._scheduler.add_job(
                    func=self.cleanup,
                    trigger="date",
                    run_date=datetime.datetime.now(
                        tz=pytz.timezone(settings.TZ)
                    ) + datetime.timedelta(seconds=3)
                )
                self._onlyonce = False
                self.__update_config()
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def get_state(self) -> bool:
        return bool(self._enabled and self._cron and self._downloaders)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        if self.get_state():
            return [{
                "id": "QbFinishedCleanup",
                "name": "qB已整理自动清理服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.cleanup,
                "kwargs": {}
            }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        qb_items = [
            {"title": config.name, "value": config.name}
            for config in DownloaderHelper().get_configs().values()
            if config.type == "qbittorrent"
        ]
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "onlyonce", "label": "立即运行一次"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "notify", "label": "发送通知"}
                                }]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VCronField",
                                    "props": {
                                        "model": "cron",
                                        "label": "执行周期",
                                        "placeholder": "*/15 * * * *"
                                    }
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "multiple": True,
                                        "chips": True,
                                        "clearable": True,
                                        "model": "downloaders",
                                        "label": "qB下载器",
                                        "items": qb_items
                                    }
                                }]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "threshold_gb",
                                        "label": "剩余空间阈值GB",
                                        "placeholder": "500"
                                    }
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "tag",
                                        "label": "清理标签",
                                        "placeholder": "已整理"
                                    }
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "max_delete",
                                        "label": "单次最多删除",
                                        "placeholder": "10，0表示不限"
                                    }
                                }]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "check_path",
                                        "label": "检测目录",
                                        "placeholder": "留空使用任务保存目录"
                                    }
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "completed_only", "label": "只删已完成"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "dry_run", "label": "试运行"}
                                }]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VAlert",
                                "props": {
                                    "type": "warning",
                                    "variant": "tonal",
                                    "text": "会删除 qB 任务和本地文件，只处理所选 qB 中带指定标签的任务。"
                                }
                            }]
                        }]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "notify": False,
            "downloaders": [],
            "cron": "*/15 * * * *",
            "tag": "已整理",
            "threshold_gb": "500",
            "check_path": "",
            "max_delete": "10",
            "completed_only": True,
            "dry_run": False
        }

    def get_page(self) -> List[dict]:
        history = self.get_data("history") or []
        if not history:
            return [{
                "component": "div",
                "text": "暂无清理记录",
                "props": {"class": "text-center"}
            }]

        contents = []
        for item in history[:30]:
            contents.append({
                "component": "VCard",
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "text-subtitle-1"},
                        "text": f"{item.get('time')} {item.get('downloader')}"
                    },
                    {
                        "component": "VCardText",
                        "text": (
                            f"删除：{item.get('deleted_count')} 个，"
                            f"释放约：{item.get('deleted_size')}，"
                            f"运行模式：{item.get('mode')}"
                        )
                    }
                ]
            })
        return [{
            "component": "div",
            "props": {"class": "grid gap-3 grid-info-card"},
            "content": contents
        }]

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            logger.error(f"qB已整理自动清理服务停止失败：{str(e)}")

    @property
    def service_infos(self) -> Dict[str, ServiceInfo]:
        services = DownloaderHelper().get_services(
            type_filter="qbittorrent",
            name_filters=self._downloaders
        )
        if not services:
            logger.warning("qB已整理自动清理：未获取到可用 qB 下载器")
            return {}

        active_services = {}
        for service_name, service_info in services.items():
            if service_info.instance.is_inactive():
                logger.warning(f"qB已整理自动清理：下载器 {service_name} 未连接")
            else:
                active_services[service_name] = service_info
        return active_services

    def cleanup(self):
        """
        清理 qB 中指定标签的已整理任务。
        """
        tags = self.__tag_list()
        if not tags:
            logger.warning("qB已整理自动清理：清理标签为空，跳过")
            return

        threshold_bytes = int(self.__to_float(self._threshold_gb, 500) * 1024 ** 3)
        max_delete = self.__to_int(self._max_delete, 10)
        if threshold_bytes <= 0:
            logger.warning("qB已整理自动清理：剩余空间阈值无效，跳过")
            return

        services = self.service_infos
        if not services:
            return

        with lock:
            for downloader_name, service_info in services.items():
                if self._event.is_set():
                    logger.info("qB已整理自动清理服务停止")
                    return
                self.__cleanup_downloader(
                    downloader_name=downloader_name,
                    downloader=service_info.instance,
                    tags=tags,
                    threshold_bytes=threshold_bytes,
                    max_delete=max_delete
                )

    def __cleanup_downloader(self, downloader_name: str, downloader: Any,
                             tags: List[str], threshold_bytes: int,
                             max_delete: int):
        torrents, error = downloader.get_torrents(tags=tags)
        if error:
            logger.error(f"qB已整理自动清理：获取 {downloader_name} 种子失败")
            return

        candidates = []
        for torrent in torrents or []:
            if self._completed_only and not self.__is_completed(torrent):
                continue
            item = self.__build_item(torrent)
            if item:
                candidates.append(item)

        if not candidates:
            logger.info(f"qB已整理自动清理：{downloader_name} 没有符合标签 {','.join(tags)} 的已完成任务")
            return

        check_path = self.__resolve_check_path(candidates)
        if not check_path:
            logger.error("qB已整理自动清理：未找到可用检测目录，跳过")
            return

        usage = shutil.disk_usage(check_path)
        free_bytes = usage.free
        logger.info(
            f"qB已整理自动清理：{downloader_name} 检测目录 {check_path} "
            f"剩余 {StringUtils.str_filesize(free_bytes)}，"
            f"阈值 {StringUtils.str_filesize(threshold_bytes)}"
        )
        if free_bytes > threshold_bytes:
            logger.info(f"qB已整理自动清理：{downloader_name} 剩余空间充足，跳过")
            return

        candidates.sort(key=lambda item: (item.get("done_time") or 0, item.get("added_time") or 0))
        deleted_count = 0
        deleted_bytes = 0
        projected_free = free_bytes

        for item in candidates:
            if self._event.is_set():
                logger.info("qB已整理自动清理服务停止")
                return
            if max_delete > 0 and deleted_count >= max_delete:
                break

            text_item = (
                f"{item.get('name')} "
                f"大小：{StringUtils.str_filesize(item.get('size') or 0)} "
                f"路径：{item.get('save_path') or '-'}"
            )
            if self._dry_run:
                logger.info(f"qB已整理自动清理试运行：将删除种子及文件：{text_item}")
                success = True
            else:
                success = downloader.delete_torrents(delete_file=True, ids=[item.get("id")])
                if success:
                    logger.info(f"qB已整理自动清理：已删除种子及文件：{text_item}")
                else:
                    logger.error(f"qB已整理自动清理：删除失败：{text_item}")
                    continue

            deleted_count += 1
            deleted_bytes += item.get("size") or 0
            projected_free += item.get("size") or 0
            if projected_free > threshold_bytes:
                break

        if deleted_count:
            self.__save_history({
                "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "downloader": downloader_name,
                "deleted_count": deleted_count,
                "deleted_size": StringUtils.str_filesize(deleted_bytes),
                "mode": "试运行" if self._dry_run else "删除文件"
            })
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【qB已整理自动清理完成】",
                    text=(
                        f"{downloader_name} {'试运行' if self._dry_run else '删除'} "
                        f"{deleted_count} 个任务，约 {StringUtils.str_filesize(deleted_bytes)}"
                    )
                )
        else:
            logger.info(f"qB已整理自动清理：{downloader_name} 没有执行删除")

    def __resolve_check_path(self, candidates: List[dict]) -> Optional[str]:
        paths = [self._check_path] if self._check_path else []
        if not paths:
            paths = [item.get("save_path") for item in candidates if item.get("save_path")]
        for path in paths:
            if not path:
                continue
            check_path = Path(path)
            if check_path.exists():
                return str(check_path)
        return None

    def __build_item(self, torrent: Any) -> Optional[dict]:
        torrent_id = self.__torrent_attr(torrent, "hash")
        if not torrent_id:
            return None
        return {
            "id": torrent_id,
            "name": self.__torrent_attr(torrent, "name", ""),
            "size": self.__to_int(self.__torrent_attr(torrent, "size", 0), 0),
            "save_path": self.__torrent_attr(torrent, "save_path", ""),
            "done_time": self.__to_int(self.__torrent_attr(torrent, "completion_on", 0), 0),
            "added_time": self.__to_int(self.__torrent_attr(torrent, "added_on", 0), 0),
            "state": self.__torrent_attr(torrent, "state", "")
        }

    def __is_completed(self, torrent: Any) -> bool:
        progress = self.__to_float(self.__torrent_attr(torrent, "progress", 0), 0)
        completion_on = self.__to_int(self.__torrent_attr(torrent, "completion_on", 0), 0)
        state = str(self.__torrent_attr(torrent, "state", ""))
        return progress >= 0.9999 or completion_on > 0 or state in {
            "uploading", "stalledUP", "pausedUP", "forcedUP", "queuedUP", "checkingUP"
        }

    def __tag_list(self) -> List[str]:
        return [tag.strip() for tag in str(self._tag or "").split(",") if tag.strip()]

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "notify": self._notify,
            "downloaders": self._downloaders,
            "cron": self._cron,
            "tag": self._tag,
            "threshold_gb": self._threshold_gb,
            "check_path": self._check_path,
            "max_delete": self._max_delete,
            "completed_only": self._completed_only,
            "dry_run": self._dry_run
        })

    def __save_history(self, item: dict):
        history = self.get_data("history") or []
        history.insert(0, item)
        self.save_data("history", history[:50])

    @staticmethod
    def __torrent_attr(torrent: Any, name: str, default: Any = None) -> Any:
        if isinstance(torrent, dict):
            return torrent.get(name, default)
        return getattr(torrent, name, default)

    @staticmethod
    def __to_int(value: Any, default: int = 0) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def __to_float(value: Any, default: float = 0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
