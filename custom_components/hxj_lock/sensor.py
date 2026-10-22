"""慧享家门锁 — 最近事件 sensor"""
from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
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
        async_add_entities(HxjLastEventSensor(coordinator, did) for did in new_ids)

    _add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))


class HxjLastEventSensor(CoordinatorEntity, SensorEntity):
    """最近一次门锁事件 (开门/报警/低电量等)"""

    _attr_has_entity_name = True

    def __init__(self, coordinator: HxjLockCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        info = (coordinator.data or {}).get(device_id, {}).get("info", {})
        name = info.get("deviceName") or device_id
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{device_id}_last_event"
        self._attr_name = "最近事件"
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
    def native_value(self):
        events = (self.coordinator.data or {}).get(self._device_id, {}).get("events", [])
        if not events:
            return "无记录"
        e = events[0]
        return f"{e['time']} {e['title']}"

    @property
    def extra_state_attributes(self) -> dict:
        dev = (self.coordinator.data or {}).get(self._device_id, {})
        events = dev.get("events", [])
        return {
            "device_id": self._device_id,
            "events": [
                {"time": e["time"], "title": e["title"],
                 "logId": e.get("logId"), "hasMedia": e.get("hasMedia")}
                for e in events
            ],
            "event_total_hint": dev.get("log_sum"),
        }
