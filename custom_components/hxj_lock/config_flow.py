"""慧享家门锁配置流程 — 账号密码 / 极验令牌粘贴 / reauth / 选项"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    CONF_ACCOUNT, CONF_ANDROID_ID, CONF_DEVICE_NAME, CONF_LOG_COUNT,
    CONF_NOTIFY, CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_TOKEN_ID,
    DEFAULT_LOG_COUNT, DEFAULT_NOTIFY, DEFAULT_SCAN_INTERVAL, DOMAIN,
    GEETEST_HINT, TITLE,
)
from .protocol import HxjAuthError, HxjClient, HxjProtocolError

_LOGGER = logging.getLogger(__name__)

# 服务端错误码
RESULT_GEETEST_REQUIRED = 500004   # 极验二次验证不通过/缺失


class HxjLockConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """慧享家门锁 UI 配置流程"""

    VERSION = 1

    def __init__(self) -> None:
        self._account: str = ""
        self._password: str = ""
        self._android_id: str = ""
        self._device_name: str = ""
        self._token_id: str = ""

    # ---------- 工具 ----------
    async def _test_connection(
        self, account: str, password: str, token_id: str,
        android_id: str, device_name: str,
    ) -> dict[str, Any]:
        """在 executor 中验证凭证并拉取设备列表, 返回设备 dict"""

        def _work() -> dict[str, Any]:
            client = HxjClient(account, password, android_id=android_id)
            try:
                if token_id:
                    client.token_id = token_id
                    client.tcp_connect()
                else:
                    r = client.https_login()
                    if r.get("resultCode") != 0:
                        return {"code": r.get("resultCode"),
                                "reason": r.get("reason")}
                    client.tcp_connect()
                devices = (client.cloud_device_list().get("data") or {}).get("list") or []
                if device_name and not any(
                        d.get("deviceName") == device_name for d in devices):
                    names = ", ".join(d.get("deviceName", "?") for d in devices)
                    return {"code": "name_mismatch", "reason": names}
                return {"devices": devices}
            finally:
                client.close()

        return await self.hass.async_add_executor_job(_work)

    # ---------- 步骤 ----------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._account = user_input[CONF_ACCOUNT]
            self._password = user_input.get(CONF_PASSWORD, "")
            self._token_id = user_input.get(CONF_TOKEN_ID, "")
            self._device_name = user_input.get(CONF_DEVICE_NAME, "")
            self._android_id = uuid.uuid4().hex[:16]

            try:
                result = await self._test_connection(
                    self._account, self._password, self._token_id,
                    self._android_id, self._device_name)
            except HxjAuthError as err:
                errors["base"] = "invalid_auth"
                _LOGGER.warning("hxj_lock 凭证无效: %s", err)
                result = {}
            except HxjProtocolError as err:
                errors["base"] = "cannot_connect"
                _LOGGER.warning("hxj_lock 连接测试失败: %s", err)
                result = {}
            if not errors:
                code = result.get("code")
                if code == RESULT_GEETEST_REQUIRED:
                    # 需要人机结合极验 -> 引导粘贴令牌
                    return await self.async_step_paste_token()
                if code:
                    errors["base"] = "invalid_auth" if code == 500004 else \
                        ("name_mismatch" if code == "name_mismatch" else "cannot_connect")
                elif not result.get("devices"):
                    errors["base"] = "no_devices"
                else:
                    return self._create_entry(self._token_id)

        schema = vol.Schema({
            vol.Required(CONF_ACCOUNT): str,
            vol.Optional(CONF_PASSWORD): str,
            vol.Optional(CONF_TOKEN_ID): str,
            vol.Optional(CONF_DEVICE_NAME): str,
        })
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors)

    async def async_step_paste_token(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """极验令牌粘贴页"""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._token_id = user_input[CONF_TOKEN_ID]
            try:
                result = await self._test_connection(
                    self._account, self._password, self._token_id,
                    self._android_id, self._device_name)
            except HxjAuthError as err:
                errors["base"] = "invalid_auth"
                _LOGGER.warning("hxj_lock 令牌无效: %s", err)
                result = {}
            except HxjProtocolError as err:
                errors["base"] = "cannot_connect"
                _LOGGER.warning("hxj_lock 令牌验证失败: %s", err)
                result = {}
            if not errors:
                code = result.get("code")
                if code == RESULT_GEETEST_REQUIRED:
                    errors["base"] = "invalid_auth"
                elif code:
                    errors["base"] = "cannot_connect"
                elif not result.get("devices"):
                    errors["base"] = "no_devices"
                else:
                    return self._create_entry(self._token_id)

        schema = vol.Schema({vol.Required(CONF_TOKEN_ID): str})
        return self.async_show_form(
            step_id="paste_token", data_schema=schema, errors=errors,
            description_placeholders={"hint": GEETEST_HINT})

    def _create_entry(self, token_id: str) -> config_entries.FlowResult:
        data = {
            CONF_ACCOUNT: self._account,
            CONF_PASSWORD: self._password,
            CONF_TOKEN_ID: token_id,
            CONF_ANDROID_ID: self._android_id,
            CONF_DEVICE_NAME: self._device_name,
        }
        return self.async_create_entry(title=TITLE, data=data)

    # ---------- reauth: token 失效后重新粘贴 ----------
    async def async_step_reauth(
        self, entry_data: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        if user_input is not None:
            token_id = user_input[CONF_TOKEN_ID]
            try:
                result = await self._test_connection(
                    reauth_entry.data[CONF_ACCOUNT],
                    reauth_entry.data.get(CONF_PASSWORD, ""),
                    token_id,
                    reauth_entry.data[CONF_ANDROID_ID],
                    reauth_entry.data.get(CONF_DEVICE_NAME, ""))
            except HxjAuthError as err:
                errors["base"] = "invalid_auth"
                _LOGGER.warning("hxj_lock reauth 令牌无效: %s", err)
                result = {}
            except HxjProtocolError as err:
                errors["base"] = "cannot_connect"
                _LOGGER.warning("hxj_lock reauth 失败: %s", err)
                result = {}
            if not errors:
                if result.get("code") == RESULT_GEETEST_REQUIRED:
                    errors["base"] = "invalid_auth"
                elif result.get("code"):
                    errors["base"] = "cannot_connect"
                elif not result.get("devices"):
                    errors["base"] = "no_devices"
                else:
                    new_data = {**reauth_entry.data, CONF_TOKEN_ID: token_id}
                    try:
                        return self.async_update_reload_and_abort(
                            reauth_entry, data=new_data)
                    except AttributeError:  # 旧版 HA 兼容
                        self.hass.async_create_task(
                            self.hass.config_entries.async_reload(reauth_entry.entry_id))
                        return self.async_abort(reason="reauth_successful")

        schema = vol.Schema({vol.Required(CONF_TOKEN_ID): str})
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=schema, errors=errors,
            description_placeholders={"hint": GEETEST_HINT})

    # ---------- 选项 ----------
    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "HxjLockOptionsFlow":
        return HxjLockOptionsFlow(config_entry)


class HxjLockOptionsFlow(config_entries.OptionsFlow):
    """选项: 轮询间隔 / 日志条数 / 设备名筛选"""

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self.entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        schema = vol.Schema({
            vol.Optional(CONF_SCAN_INTERVAL, default=int(self.entry.options.get(
                CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))):
                vol.All(int, vol.Range(min=60, max=3600)),
            vol.Optional(CONF_LOG_COUNT, default=int(self.entry.options.get(
                CONF_LOG_COUNT, DEFAULT_LOG_COUNT))):
                vol.All(int, vol.Range(min=1, max=50)),
            vol.Optional(CONF_DEVICE_NAME, default=self.entry.options.get(
                CONF_DEVICE_NAME, self.entry.data.get(CONF_DEVICE_NAME, ""))): str,
            vol.Optional(CONF_NOTIFY, default=bool(self.entry.options.get(
                CONF_NOTIFY, DEFAULT_NOTIFY))): bool,
        })
        return self.async_show_form(step_id="init", data_schema=schema)
