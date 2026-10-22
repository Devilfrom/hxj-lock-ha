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


async def async_validate_trigger_config(hass, config=None):
    """校验设备触发器配置。

    注意: HA 2026.6 起该回调的签名多了 hass 参数 (DeviceAutomationTriggerProtocol),
    旧版本是 (config)。这里两种都能接受, 避免版本差异导致
    "async_validate_trigger_config() takes 1 positional argument but 2 were given"
    —— 那会让整个 automation 域 setup 失败, 所有自动化都变 unavailable。
    """
    if config is None:            # 兼容旧签名 async_validate_trigger_config(config)
        hass, config = None, hass
    return TRIGGER_SCHEMA(config)


async def async_attach_trigger(hass, config, action, trigger_info):
    """监听 hxj_lock_event bus 事件, 匹配设备与事件类型后触发 action"""
    device_id = config[CONF_DEVICE_ID]
    subtype = config[CONF_TYPE]

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(device_id)
    cloud_ids = {ident[1] for ident in device.identifiers if ident[0] == DOMAIN} \
        if device else set()
    # trigger_info.trigger_data 里带 id/idx/alias, 按 HA 标准触发器范式合并进
    # trigger 变量 (参考 components/homeassistant/triggers/state.py)
    trigger_data = {}
    if isinstance(trigger_info, dict):
        trigger_data = dict(trigger_info.get("trigger_data") or {})
    device_name = device.name if device else device_id

    @callback
    def _handle(event):
        if event.data.get("device_id") not in cloud_ids:
            return
        if subtype != "other" and event.data.get("event_type") != subtype:
            return
        # HA 2026.6 已弃用 async_run_job (每次调用都会打 deprecation 警告),
        # 协程 target 直接用 async_create_task
        hass.async_create_task(
            _run_action(hass, action, {
                "trigger": {
                    **trigger_data,
                    "platform": "device",
                    "domain": DOMAIN,
                    "device_id": device_id,
                    "type": subtype,
                    "event_type": event.data.get("event_type"),
                    "event_type_name": event.data.get("event_type_name"),
                    "title": event.data.get("title"),
                    "time": event.data.get("time"),
                    "log_id": event.data.get("log_id"),
                    "has_media": event.data.get("has_media"),
                    # 同时保留 event 子字典, 与 event 平台触发器的用法保持一致
                    "event": dict(event.data),
                    "description": f"{EVENT_TYPES[subtype]['name']} of {device_name}",
                }
            }))

    return hass.bus.async_listen(EVENT_FIRE_TYPE, _handle)


async def _run_action(hass, action, variables) -> None:
    """执行触发的 action (协程形式, 避免 HA 2026.6 的 async_run_job 警告)"""
    await action(variables)


async def async_get_trigger_capabilities(hass, config):
    return {"extra_fields": vol.Schema({})}
