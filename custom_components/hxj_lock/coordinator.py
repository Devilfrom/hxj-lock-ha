"""慧享家门锁数据协调器 — 每次更新建立独立 TCP 连接 (服务端会关闭空闲连接)"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.components.persistent_notification import async_create as pn_async_create

from .const import (
    CONF_ACCOUNT, CONF_ANDROID_ID, CONF_DEVICE_NAME, CONF_LOG_COUNT,
    CONF_SCAN_INTERVAL, CONF_TOKEN_ID, DEFAULT_LOG_COUNT, DEFAULT_SCAN_INTERVAL,
    DEVICE_TYPE_CLOUD_LOCK, DOMAIN, GEETEST_HINT,
)
from .protocol import HxjAuthError, HxjClient, HxjProtocolError, event_title, fmt_time

_LOGGER = logging.getLogger(__name__)


class HxjLockCoordinator(DataUpdateCoordinator):
    """轮询慧享家云端门锁数据"""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        opts = entry.options
        interval = opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass, _LOGGER, name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=interval),
        )
        self.entry = entry
        self.hass = hass
        self._notified_auth = False

    # ---------- 属性 ----------
    @property
    def account(self) -> str:
        return self.entry.data.get(CONF_ACCOUNT, "")

    @property
    def token_id(self) -> str:
        return self.entry.data.get(CONF_TOKEN_ID, "")

    @property
    def android_id(self) -> str:
        return self.entry.data.get(CONF_ANDROID_ID, "")

    @property
    def device_filter(self) -> str:
        return self.entry.options.get(CONF_DEVICE_NAME, "") or \
            self.entry.data.get(CONF_DEVICE_NAME, "")

    @property
    def log_count(self) -> int:
        return int(self.entry.options.get(CONF_LOG_COUNT, DEFAULT_LOG_COUNT))

    def _make_client(self) -> HxjClient:
        c = HxjClient(self.account, "", android_id=self.android_id)
        c.token_id = self.token_id
        return c

    # ---------- 轮询 ----------
    async def _async_update_data(self) -> dict:
        try:
            return await self.hass.async_add_executor_job(self._update_data)
        except ConfigEntryAuthFailed:
            self._notify_auth_needed()
            raise

    def _update_data(self) -> dict:
        if not self.token_id:
            raise ConfigEntryAuthFailed("未配置 tokenId, 需要重新登录")
        client = self._make_client()
        try:
            client.tcp_connect()  # HxjAuthError -> token 失效
            resp = client.cloud_device_list()
            devices_raw = (resp.get("data") or {}).get("list") or []
            name_filter = self.device_filter
            devices = {}
            for d in devices_raw:
                if name_filter and d.get("deviceName") != name_filter:
                    continue
                did = d.get("deviceId")
                if not did:
                    continue
                log_resp = client.cloud_lock_log(
                    did, 0, self.log_count, DEVICE_TYPE_CLOUD_LOCK)
                log_data = log_resp.get("data") or {}
                logs = log_data.get("list") or []
                events = [{
                    "time": fmt_time(r.get("createTime")),
                    "ts": r.get("createTime"),
                    "title": event_title(r),
                    "logId": r.get("logId"),
                    "hasMedia": bool(r.get("mediaState") or r.get("videoState")),
                } for r in logs]
                events.sort(key=lambda x: x.get("ts") or 0, reverse=True)
                devices[did] = {"info": d, "events": events,
                                "log_sum": log_data.get("listSum")}
            if name_filter and not devices:
                _LOGGER.warning(
                    "hxj_lock: 设备名筛选 %r 未匹配到任何云锁 (账号下共 %d 台)",
                    name_filter, len(devices_raw))
            self._notified_auth = False
            _LOGGER.info(
                "hxj_lock: 数据刷新成功, %d 台云锁在线数据 (账号 %s)",
                len(devices), self.account)
            return devices
        except HxjAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except HxjProtocolError as err:
            raise UpdateFailed(str(err)) from err
        finally:
            client.close()

    # ---------- 远程开锁 ----------
    async def async_open_lock(self, device_id: str | None = None) -> dict:
        """远程开锁; device_id 为空时开所有已发现设备"""
        targets = [device_id] if device_id else list((self.data or {}).keys())
        if not targets:
            raise UpdateFailed("没有可操作的锁设备 (请先完成一次数据刷新)")
        results = {}
        for did in targets:
            results[did] = await self.hass.async_add_executor_job(
                self._open_lock_blocking, did)
        return results

    def _open_lock_blocking(self, device_id: str) -> dict:
        client = self._make_client()
        try:
            client.tcp_connect()
            return client.cloud_open_lock(device_id, DEVICE_TYPE_CLOUD_LOCK)
        except HxjAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except HxjProtocolError as err:
            raise UpdateFailed(str(err)) from err
        finally:
            client.close()

    # ---------- 通知 ----------
    def _notify_auth_needed(self) -> None:
        if self._notified_auth:
            return
        self._notified_auth = True

        def _create() -> None:
            self.hass.async_create_task(pn_async_create(
                self.hass,
                GEETEST_HINT,
                title="慧享家门锁: 登录令牌失效",
                notification_id="hxj_lock_auth",
            ))

        self.hass.loop.call_soon_threadsafe(_create)
