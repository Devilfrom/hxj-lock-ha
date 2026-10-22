"""慧享家门锁 — event 实体 (门锁事件: 开门/关门/报警/低电量等)"""
from __future__ import annotations

import logging

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, EVENT_TYPES, MANUFACTURER
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
        async_add_entities(
            HxjLockEvent(coordinator, did) for did in new_ids)

    _add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))


class HxjLockEvent(CoordinatorEntity, EventEntity):
    """门锁事件实体。coordinator 收到事件 (轮询或服务端实时推送) 时触发一个
    HA event, 可被自动化直接监听 (event 实体触发器) 或用 device_trigger。"""

    _attr_has_entity_name = True
    _attr_event_types = list(EVENT_TYPES.keys())  # 支持的事件子类型

    def __init__(self, coordinator: HxjLockCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        info = (coordinator.data or {}).get(device_id, {}).get("info", {})
        name = info.get("deviceName") or device_id
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{device_id}_event"
        self._attr_name = "事件"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_id)},
            "name": name,
            "manufacturer": MANUFACTURER,
        }

    @property
    def available(self) -> bool:
        return (self.coordinator.last_update_success
                and self._device_id in (self.coordinator.data or {}))

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # 订阅 coordinator 的门锁事件, 实时更新自身状态
        self.coordinator.register_event_sink(self._async_handle_event)

    async def async_will_remove_from_hass(self) -> None:
        self.coordinator.unregister_event_sink(self._async_handle_event)
        await super().async_will_remove_from_hass()

    @callback
    def _async_handle_event(self, payload: dict) -> None:
        """收到门锁事件 -> 触发 HA event (HA 2026.6.2 用 _trigger_event)"""
        if payload.get("device_id") != self._device_id:
            return
        event_type = payload.get("event_type")
        if event_type not in self._attr_event_types:
            return
        # HA 2026.6.2 的 EventEntity 用 _trigger_event(event_type, attributes) 触发
        # (旧的 async_fire_event 已不存在, 直接调用会 AttributeError 导致本平台 setup 失败)
        self._trigger_event(event_type, {
            "device_id": payload.get("device_id"),
            "title": payload.get("title"),
            "log_id": payload.get("log_id"),
            "time": payload.get("time"),
            "has_media": payload.get("has_media"),
        })
        self.async_write_ha_state()
