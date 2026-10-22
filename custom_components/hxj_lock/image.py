"""慧享家门锁 — image 实体 (门前抓拍)

云锁的抓拍图地址就在开门记录里 (logType=3 / logType=4+logInfoType=15 的记录带
imageUrl), 是 file.hxjiot.com 上的公网 JPEG, 直接给前端即可。

前端通过 HA 自己的图片代理 (entity_picture -> /api/... 带 access_token) 取图,
所以云端地址不必对浏览器公网可达, HA 侧负责下载。
"""
from __future__ import annotations

import logging

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MANUFACTURER
from .coordinator import HxjLockCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: HxjLockCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()  # 每平台独立认领, 不能用协调器共享集合

    @callback
    def _add_new_entities() -> None:
        new_ids = [d for d in (coordinator.data or {}) if d not in known]
        if not new_ids:
            return
        known.update(new_ids)
        async_add_entities(HxjLockImage(coordinator, did) for did in new_ids)

    _add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))


class HxjLockImage(CoordinatorEntity, ImageEntity):
    """门前抓拍图片实体 — 状态为图片的更新时间, 图片本体经 HA 图片代理加载。"""

    _attr_has_entity_name = True
    _attr_name = "门前抓拍"
    _attr_should_poll = False
    _attr_content_type = "image/jpeg"
    _attr_image_url = None
    _attr_image_last_updated = None

    def __init__(self, coordinator: HxjLockCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        # CoordinatorEntity.__init__ 不会链式调用 ImageEntity.__init__,
        # 必须显式初始化: 它要建 httpx client 和图片访问用的 access_tokens
        # (缺了会报 "'HxjLockImage' object has no attribute 'access_tokens'")
        ImageEntity.__init__(self, coordinator.hass)
        self._device_id = device_id
        info = (coordinator.data or {}).get(device_id, {}).get("info", {})
        name = info.get("deviceName") or device_id
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{device_id}_image"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_id)},
            "name": name,
            "manufacturer": MANUFACTURER,
        }
        self._apply_snapshot()

    # ---------- 快照 ----------
    @property
    def _snapshot(self) -> dict:
        return (self.coordinator.data or {}).get(self._device_id, {}).get("snapshot") or {}

    @property
    def extra_state_attributes(self) -> dict:
        snap = self._snapshot
        return {
            "log_id": snap.get("log_id"),
            "event_title": snap.get("title"),
            "event_time": snap.get("time"),
            "image_url": snap.get("image_url"),
            "thumb_url": snap.get("thumb_url"),
        }

    @callback
    def _apply_snapshot(self) -> None:
        """把最新抓拍套用到实体上 (image_url 是 cached_property, 赋值即失效缓存)"""
        snap = self._snapshot
        url = snap.get("image_url")
        if not url or url == self._attr_image_url:
            return
        self._attr_image_url = url
        ts = snap.get("ts")
        try:
            self._attr_image_last_updated = (
                dt_util.utc_from_timestamp(int(ts)) if ts else dt_util.utcnow())
        except (TypeError, ValueError):
            self._attr_image_last_updated = dt_util.utcnow()
        _LOGGER.info("hxj_lock: %s 更新门前抓拍 %s", self._device_id, url)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._apply_snapshot()
        self.async_write_ha_state()
