"""慧享家门锁 — lock 实体 (支持远程开锁)"""
from __future__ import annotations

import logging
import time

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.exceptions import HomeAssistantError

try:  # HA 2025.10+ 用 LockState 枚举; 旧版兼容字符串常量
    from homeassistant.components.lock import LockState

    STATE_LOCKED = LockState.LOCKED.value
    STATE_UNLOCKED = LockState.UNLOCKED.value
except ImportError:  # pragma: no cover
    STATE_LOCKED = "locked"
    STATE_UNLOCKED = "unlocked"

from .const import DOMAIN, MANUFACTURER
from .coordinator import HxjLockCoordinator

_LOGGER = logging.getLogger(__name__)

# 开门后实体显示 unlocked 的持续时间 (秒)
UNLOCKED_DISPLAY_SECONDS = 10


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
        async_add_entities(
            HxjLockEntity(coordinator, did) for did in new_ids)

    _add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))


class HxjLockEntity(CoordinatorEntity, LockEntity):
    """云锁实体。注意: 云端协议不反馈门闩实际状态, 默认显示 locked, 开锁后短暂显示 unlocked"""

    _attr_has_entity_name = True
    # HA 2026.6 的 LockEntityFeature 只剩 OPEN(lock 域已改成"开/关"模型,
    # LOCK/UNLOCK 枚举被移除 —— 引用不存在的枚举会让整个平台 import 失败)。
    # LOCK 不声明: 服务端没有远程上锁接口, 声明了界面才会给出必然失败的上锁按钮。
    _attr_supported_features = LockEntityFeature.OPEN

    def __init__(self, coordinator: HxjLockCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        info = (coordinator.data or {}).get(device_id, {}).get("info", {})
        name = info.get("deviceName") or device_id
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{device_id}"
        self._attr_name = None  # 主实体: 友好名直接用设备名, 避免 "大门锁3 大门锁3"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_id)},
            "name": name,
            "manufacturer": MANUFACTURER,
            "model": f"云锁 subDeviceType={info.get('subDeviceType')}",
            "sw_version": info.get("softwareVersion"),
            "hw_version": info.get("hardwareVersion"),
        }
        self._open_until: float = 0.0

    # ---------- 状态 ----------
    @property
    def available(self) -> bool:
        return (self.coordinator.last_update_success
                and self._device_id in (self.coordinator.data or {}))

    @property
    def state(self) -> str:
        if time.time() < self._open_until:
            return STATE_UNLOCKED
        return STATE_LOCKED

    @property
    def extra_state_attributes(self) -> dict:
        dev = (self.coordinator.data or {}).get(self._device_id) or {}
        info = dev.get("info", {})
        events = dev.get("events", [])
        return {
            "device_id": self._device_id,
            "online": info.get("deviceState") == 3,
            "rssi": info.get("rssi"),
            "device_mac": info.get("deviceMac"),
            "last_event": events[0]["time"] + " " + events[0]["title"] if events else None,
            "event_count": len(events),
            "remote_lock_supported": False,
            "note": "云锁通道仅支持远程开锁; 关门由门体机械回锁完成, 并以关门事件上报",
        }

    # ---------- 开锁 ----------
    async def async_unlock(self, **kwargs) -> None:
        """HA 界面「解锁」按钮 — 云锁的远程动作就是开锁"""
        await self._async_cloud_open()

    async def async_open(self, **kwargs) -> None:
        """HA 界面「打开」按钮"""
        await self._async_cloud_open()

    async def async_lock(self, **kwargs) -> None:
        """云锁通道没有远程上锁接口, 给出明确原因而不是笼统的"不支持" """
        raise HomeAssistantError(
            "云锁通道不支持远程上锁: 服务端只提供 appCloudOpenLock。"
            "关门由门体机械回锁完成, 完成后会以「关门」事件上报。")

    async def _async_cloud_open(self) -> None:
        try:
            results = await self.coordinator.async_open_lock(self._device_id)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(f"远程开锁失败: {err}") from err
        resp = results.get(self._device_id) or {}
        if resp.get("resultCode") != 0:
            raise HomeAssistantError(
                f"远程开锁被拒绝: resultCode={resp.get('resultCode')} "
                f"reason={resp.get('reason')}")
        _LOGGER.info("hxj_lock: %s 远程开锁成功", self._device_id)
        self._open_until = time.time() + UNLOCKED_DISPLAY_SECONDS
        self.async_write_ha_state()
        # 到点恢复 locked 显示
        # 注意: 必须用模块级 async_call_later —— 新版 HA 已移除 HomeAssistant.helpers,
        # 用 self.hass.helpers.event.async_call_later 会抛 AttributeError,
        # 导致整个开锁服务返回 500 (界面表现为"开关锁报错", 其实锁已经开了)
        async_call_later(
            self.hass, UNLOCKED_DISPLAY_SECONDS + 1, self._async_reset_state)

    @callback
    def _async_reset_state(self, *_) -> None:
        self.async_write_ha_state()
