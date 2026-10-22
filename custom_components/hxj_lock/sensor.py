"""慧享家门锁 — 最近事件 sensor"""
from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, EVENT_TYPES, MANUFACTURER
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
            entities.append(HxjLastEventSensor(coordinator, did))
            entities.append(HxjDoorStateTextSensor(coordinator, did))
        async_add_entities(entities)

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
        snap = dev.get("snapshot") or {}
        return {
            "device_id": self._device_id,
            # 只暴露最近若干条, 避免 log_count 调大后属性过大
            "events": [
                {"time": e["time"], "title": e["title"],
                 "logId": e.get("logId"), "hasMedia": e.get("hasMedia"),
                 "type": e.get("category"),
                 "type_name": EVENT_TYPES.get(e.get("category"), {}).get("name")}
                for e in events[:20]
            ],
            "events_shown": min(len(events), 20),
            "event_total_hint": dev.get("log_sum"),
            "snapshot_time": snap.get("time"),
            "snapshot_image_url": snap.get("image_url"),
        }


class HxjDoorStateTextSensor(CoordinatorEntity, SensorEntity):
    """门锁当前状态 (中文文字: 开门 / 关门 / 未知)。

    与 binary_sensor「门状态」同源, 区别是这里直接给中文文字, 放在仪表盘上
    一眼能看懂门是开还是关, 不用理解 on/off。
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:door-closed"

    def __init__(self, coordinator: HxjLockCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        info = (coordinator.data or {}).get(device_id, {}).get("info", {})
        name = info.get("deviceName") or device_id
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{device_id}_door_text"
        self._attr_name = "门锁状态"
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
        st = self.coordinator.get_door_state(self._device_id)
        state = st.get("state")
        text = {"open": "开门", "closed": "关门"}.get(state, "未知")
        self._attr_icon = {
            "open": "mdi:door-open", "closed": "mdi:door-closed"}.get(
                state, "mdi:help-circle-outline")
        return text

    @property
    def extra_state_attributes(self) -> dict:
        st = self.coordinator.get_door_state(self._device_id)
        return {
            "state": st.get("state"),
            "last_event": st.get("title"),
            "last_event_time": fmt_time(st.get("ts")) if st.get("ts") else None,
            "source": st.get("source"),
        }
