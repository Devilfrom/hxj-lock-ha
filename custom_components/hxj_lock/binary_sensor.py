"""慧享家门锁 — 在线状态 binary_sensor"""
from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass, BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import HxjLockCoordinator
from .protocol import fmt_time

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
        entities = []
        for did in new_ids:
            entities.append(HxjOnlineSensor(coordinator, did))
            entities.append(HxjDoorStateSensor(coordinator, did))
        async_add_entities(entities)

    _add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))


class HxjOnlineSensor(CoordinatorEntity, BinarySensorEntity):
    """设备在线状态 (deviceState==3)"""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_registry_enabled_default = True

    def __init__(self, coordinator: HxjLockCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        info = (coordinator.data or {}).get(device_id, {}).get("info", {})
        name = info.get("deviceName") or device_id
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{device_id}_online"
        self._attr_name = "在线状态"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_id)},
            "name": name,
            "manufacturer": MANUFACTURER,
        }

    @property
    def available(self) -> bool:
        return (self.coordinator.last_update_success
                and self._device_id in (self.coordinator.data or {}))

    @property
    def is_on(self) -> bool:
        info = (self.coordinator.data or {}).get(self._device_id, {}).get("info", {})
        return info.get("deviceState") == 3

    @property
    def extra_state_attributes(self) -> dict:
        info = (self.coordinator.data or {}).get(self._device_id, {}).get("info", {})
        return {
            "rssi": info.get("rssi"),
            "wlan_name": info.get("wlanName"),
            "device_mac": info.get("deviceMac"),
            "device_state_raw": info.get("deviceState"),
            "software_version": info.get("softwareVersion"),
            "hardware_version": info.get("hardwareVersion"),
        }


class HxjDoorStateSensor(CoordinatorEntity, BinarySensorEntity):
    """门开关状态 (开/关)。

    云锁协议**不下发门闩状态** (设备列表里只有 deviceState 在线状态), 所以
    门状态由开门/关门事件推导:
      - 收到"开门"事件 -> 开 (on)
      - 收到"关门"事件 -> 关 (off)
      - 开门后一段时间仍无关门事件 -> 视为已机械回锁, 置为关
    device_class=door, 可直接用于自动化 (门开着时开灯等)。
    """

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.DOOR
    _attr_entity_registry_enabled_default = True

    def __init__(self, coordinator: HxjLockCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        info = (coordinator.data or {}).get(device_id, {}).get("info", {})
        name = info.get("deviceName") or device_id
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{device_id}_door"
        self._attr_name = "门状态"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_id)},
            "name": name,
            "manufacturer": MANUFACTURER,
        }

    @property
    def available(self) -> bool:
        return (self.coordinator.last_update_success
                and self._device_id in (self.coordinator.data or {}))

    @property
    def is_on(self) -> bool:
        """True = 门开着"""
        return self.coordinator.get_door_state(self._device_id).get("state") == "open"

    @property
    def extra_state_attributes(self) -> dict:
        st = self.coordinator.get_door_state(self._device_id)
        return {
            "state": st.get("state"),
            "state_name": {"open": "开", "closed": "关"}.get(
                st.get("state"), "未知"),
            "last_event": st.get("title"),
            "last_event_time": fmt_time(st.get("ts")) if st.get("ts") else None,
            "source": st.get("source"),
            "note": "云端不反馈门闩状态, 门状态由开门/关门事件推导",
        }
