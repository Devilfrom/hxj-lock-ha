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
        async_add_entities(HxjOnlineSensor(coordinator, did) for did in new_ids)

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
