"""慧享家门锁集成 — 初始化 / 服务注册"""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback

from .const import (
    CONF_TOKEN_ID, DOMAIN, PLATFORMS, SERVICE_GET_SNAPSHOT, SERVICE_OPEN_LOCK,
    SERVICE_REFRESH, SERVICE_SET_TOKEN,
)
from .coordinator import HxjLockCoordinator

_LOGGER = logging.getLogger(__name__)

SET_TOKEN_SCHEMA = vol.Schema({vol.Required(CONF_TOKEN_ID): str})


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """YAML 不支持, 仅 UI 配置"""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = HxjLockCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # 后台长连接: 实时接收服务端推送的门锁事件 (appCloudLockEventPush)
    # 放在平台就绪之后启动, 保证 event 实体已订阅, 不会漏掉事件
    coordinator.start_push_listener()

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    _register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None:
        coordinator.stop_push_listener()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if not hass.data.get(DOMAIN):
            hass.data.pop(DOMAIN, None)
            for service in (SERVICE_SET_TOKEN, SERVICE_OPEN_LOCK, SERVICE_REFRESH):
                if hass.services.has_service(DOMAIN, service):
                    hass.services.async_remove(DOMAIN, service)
    return unload_ok


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """选项变更后重载集成"""
    await hass.config_entries.async_reload(entry.entry_id)


@callback
def _register_services(hass: HomeAssistant) -> None:
    """注册服务 (幂等)"""
    if hass.services.has_service(DOMAIN, SERVICE_SET_TOKEN):
        return

    async def _handle_set_token(call: ServiceCall) -> None:
        token_id = call.data[CONF_TOKEN_ID]
        for coordinator in list(hass.data.get(DOMAIN, {}).values()):
            entry = coordinator.entry
            new_data = {**entry.data, CONF_TOKEN_ID: token_id}
            await hass.config_entries.async_update_entry(entry, data=new_data)
            await hass.config_entries.async_reload(entry.entry_id)
        _LOGGER.info("hxj_lock: tokenId 已更新, 集成已重载")

    async def _handle_open_lock(call: ServiceCall) -> None:
        device_id = call.data.get("device_id")
        for coordinator in list(hass.data.get(DOMAIN, {}).values()):
            await coordinator.async_open_lock(device_id)

    async def _handle_refresh(call: ServiceCall) -> None:
        for coordinator in list(hass.data.get(DOMAIN, {}).values()):
            await coordinator.async_request_refresh()

    async def _handle_get_snapshot(call: ServiceCall) -> dict:
        """返回最近一次门前抓拍信息 (含图片地址), 供自动化使用"""
        device_id = call.data.get("device_id")
        result: dict = {}
        for coordinator in list(hass.data.get(DOMAIN, {}).values()):
            result.update(await coordinator.async_get_snapshot(device_id))
        return result

    hass.services.async_register(
        DOMAIN, SERVICE_SET_TOKEN, _handle_set_token,
        schema=SET_TOKEN_SCHEMA)

    hass.services.async_register(
        DOMAIN, SERVICE_OPEN_LOCK, _handle_open_lock)

    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH, _handle_refresh)

    hass.services.async_register(
        DOMAIN, SERVICE_GET_SNAPSHOT, _handle_get_snapshot,
        supports_response=SupportsResponse.OPTIONAL)
