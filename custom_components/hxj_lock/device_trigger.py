"""慧享家门锁 — device_trigger

让用户在自动化 UI 的「设备」里直接选本门锁的触发事件, 例如
"大门锁3 — 开门时" / "大门锁3 — 报警时"。底层监听 coordinator 发出的
hxj_lock_event bus 事件, 按 device registry 反查云锁 deviceId 匹配后触发 action。
"""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, EVENT_FIRE_TYPE, EVENT_TYPES

_LOGGER = logging.getLogger(__name__)

TRIGGER_TYPES = list(EVENT_TYPES.keys())  # open/close/alarm/low_battery/.../other

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend({
    vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES),
})


async def async_get_triggers(hass: HomeAssistant, device_id: str) -> list[dict]:
    """列出该门锁设备支持的触发器 (每种事件类型一个)"""
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(device_id)
    if not device or not any(ident[0] == DOMAIN for ident in device.identifiers):
        return []
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DEVICE_ID: device_id,
            CONF_DOMAIN: DOMAIN,
            CONF_TYPE: t,
            "name": EVENT_TYPES[t]["name"],
        }
        for t in TRIGGER_TYPES
    ]


async def async_validate_trigger_config(trigger: dict) -> dict:
    return TRIGGER_SCHEMA(trigger)


async def async_attach_trigger(hass, config, action, trigger_info):
    """监听 hxj_lock_event bus 事件, 匹配设备与事件类型后触发 action"""
    device_id = config[CONF_DEVICE_ID]
    subtype = config[CONF_TYPE]

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(device_id)
    cloud_ids = {ident[1] for ident in device.identifiers if ident[0] == DOMAIN} \
        if device else set()

    @callback
    def _handle(event):
        if event.data.get("device_id") not in cloud_ids:
            return
        if subtype != "other" and event.data.get("event_type") != subtype:
            return
        hass.async_run_job(action, {
            "trigger": config,
            "event": event.data,
        })

    return hass.bus.async_listen(EVENT_FIRE_TYPE, _handle)


async def async_get_trigger_capabilities(hass, config):
    return {"extra_fields": vol.Schema({})}
