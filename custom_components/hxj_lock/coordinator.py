"""慧享家门锁数据协调器 — 轮询 + 服务端实时推送 双通道"""
from __future__ import annotations

import logging
import re
import threading
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.components.persistent_notification import async_create as pn_async_create

from .const import (
    CONF_ACCOUNT, CONF_ANDROID_ID, CONF_DEVICE_NAME, CONF_LOG_COUNT,
    CONF_NOTIFY, CONF_SCAN_INTERVAL, CONF_TOKEN_ID, DEFAULT_LOG_COUNT,
    DEFAULT_NOTIFY, DEFAULT_SCAN_INTERVAL, DEVICE_TYPE_CLOUD_LOCK, DOMAIN,
    EVENT_FIRE_TYPE, EVENT_TYPES, GEETEST_HINT,
)
from .protocol import (
    HxjAuthError, HxjClient, HxjProtocolError, event_category, event_title, fmt_time,
)

_LOGGER = logging.getLogger(__name__)


def _safe_notification_id(device_id: str | None, log_id: str | None) -> str:
    """notification_id 会成为 persistent_notification.<id> 实体 id 的一部分,
    云端 logId 可能含 '/' '+' '=' 等字符, 必须合法化, 否则实体 id 非法"""
    raw = f"hxj_lock_evt_{device_id}_{log_id}"
    return re.sub(r"[^A-Za-z0-9_]+", "_", raw)


class HxjLockCoordinator(DataUpdateCoordinator):
    """轮询慧享家云端门锁数据, 并监听服务端实时事件推送。

    事件来源有两条:
      1. 轮询 appCloudLockLogList 拿到的开门/关门记录 (兜底, 最长延迟一个轮询周期)
      2. 服务端 appCloudLockEventPush 长连接推送 (实时)
    两条路径最终都走 _emit_event() 统一分发, 并用 logId 去重。
    """

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
        # 事件去重: did -> {logId: 占位} (保序, 超出阈值截断最早的)
        self._seen: dict[str, dict] = {}
        # 每台锁已知的最新事件时间戳: 用于丢弃服务端补推的历史事件
        self._newest_ts: dict[str, int] = {}
        self._initialized = False
        # 门锁事件订阅者 (event 实体用它实时更新自身状态)
        self._event_sinks: list = []
        # 后台长连接 (实时推送)
        self._listener_client: HxjClient | None = None
        self._listener_stop = threading.Event()
        self._listener_thread: threading.Thread | None = None

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

    @property
    def notify_enabled(self) -> bool:
        return bool(self.entry.options.get(CONF_NOTIFY, DEFAULT_NOTIFY))

    def _make_client(self) -> HxjClient:
        c = HxjClient(self.account, "", android_id=self.android_id)
        c.token_id = self.token_id
        return c

    # ---------- 轮询 ----------
    async def _async_update_data(self) -> dict:
        try:
            data = await self.hass.async_add_executor_job(self._update_data)
        except ConfigEntryAuthFailed:
            self._notify_auth_needed()
            raise
        # 检测新增事件并触发通知 (在主事件循环内)
        await self._process_new_events(data)
        return data

    # ---------- 事件订阅 ----------
    def register_event_sink(self, sink) -> None:
        """注册门锁事件回调 (event 实体用它实时更新状态)"""
        if sink not in self._event_sinks:
            self._event_sinks.append(sink)

    def unregister_event_sink(self, sink) -> None:
        if sink in self._event_sinks:
            self._event_sinks.remove(sink)

    # ---------- 事件检测 / 通知 ----------
    async def _process_new_events(self, devices: dict) -> None:
        """对比已见事件, 发现新增事件 -> 三路分发 (事件实体 / bus 事件 / 持久通知)"""
        new: list[tuple[str, dict]] = []
        for did, dev in (devices or {}).items():
            seen = self._seen.setdefault(did, {})
            # 记录该设备当前最新事件时间 (基线), 推送通道据此丢弃历史事件
            newest = max((ev.get("ts") or 0) for ev in dev.get("events", [])) \
                if dev.get("events") else 0
            if newest > self._newest_ts.get(did, 0):
                self._newest_ts[did] = newest
            for ev in dev.get("events", []):
                lid = ev.get("logId")
                if lid is None:
                    continue
                if lid not in seen:
                    if self._initialized:
                        new.append((did, ev))
                    seen[lid] = True
                    if len(seen) > 500:          # 保序截断, 防无限增长
                        seen.pop(next(iter(seen)))
        if not self._initialized:
            self._initialized = True
            return      # 首次刷新只建基线, 不把历史事件回放给 event 实体/通知
        for did, ev in new:
            self._emit_event(did, ev)

    def _emit_event(self, did: str, ev: dict) -> None:
        """门锁事件统一分发: 事件实体 + bus 事件 + 持久通知"""
        category = ev.get("category") or "other"
        payload = {
            "device_id": did,
            "event_type": category,
            "title": ev.get("title"),
            "log_id": ev.get("logId"),
            "time": ev.get("time"),
            "has_media": ev.get("hasMedia"),
        }
        for sink in list(self._event_sinks):
            try:
                sink(payload)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("hxj_lock: 事件订阅者回调异常")
        self._fire_event_and_notify(category, payload)

    def _fire_event_and_notify(self, category: str, payload: dict) -> None:
        """发 bus 事件 (供 device_trigger 自动化) + 可选的持久通知"""
        meta = EVENT_TYPES.get(category, EVENT_TYPES["other"])
        self.hass.bus.async_fire(EVENT_FIRE_TYPE, payload)
        if not self.notify_enabled:
            return
        # 注意: persistent_notification.async_create 是 @callback 同步函数 (返回 None),
        # 不能用 async_create_task 包一层, 否则会抛 "A valid coroutine must be provided"
        pn_async_create(
            self.hass,
            message=f"{payload.get('title')}（{payload.get('time')}）",
            title=f"门锁事件 · {meta['name']}",
            notification_id=_safe_notification_id(
                payload.get("device_id"), payload.get("log_id")),
        )

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
                    "logType": r.get("logType"),
                    "logInfoType": r.get("logInfoType"),
                    "keyName": r.get("keyName"),
                    "category": event_category(r),
                    "hasMedia": bool(r.get("mediaState") or r.get("videoState")),
                } for r in logs]
                events.sort(key=lambda x: x.get("ts") or 0, reverse=True)
                devices[did] = {"info": d, "events": events,
                                "log_sum": log_data.get("listSum")}
            if not devices:
                if name_filter and devices_raw:
                    _LOGGER.warning(
                        "hxj_lock: 设备名筛选 %r 未匹配到任何云锁 (账号下共 %d 台)",
                        name_filter, len(devices_raw))
                if self.data:
                    # 云端瞬时返回空列表 (开锁后服务端推送与请求错位, 或临时限流):
                    # 保留上次数据, 否则 4 个实体会被误判离线, 也无法再远程开锁
                    _LOGGER.warning(
                        "hxj_lock: 云端本次返回 0 台云锁, 保留上次数据 (%d 台) "
                        "以免实体误判离线", len(self.data))
                    return self.data
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

    # ---------- 实时推送 ----------
    def start_push_listener(self) -> None:
        """启动后台长连接, 实时接收服务端推送的门锁事件"""
        if self._listener_thread is not None:
            return
        if not self.token_id:
            return
        self._listener_stop.clear()

        def _on_push(frame: dict) -> None:
            # 监听线程 -> 切回事件循环再处理
            self.hass.loop.call_soon_threadsafe(self._handle_push_frame, frame)

        def _run() -> None:
            try:
                # 客户端在监听线程内创建: 构造时会 load_cert_chain (阻塞 IO),
                # 放事件循环里会被 HA 判定为 blocking call
                client = HxjClient(self.account, "", android_id=self.android_id)
                client.token_id = self.token_id
                self._listener_client = client
                client.listen_push(_on_push, self._listener_stop)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("hxj_lock: 推送监听线程异常退出")

        self._listener_thread = threading.Thread(
            target=_run, name="hxj_lock_push", daemon=True)
        self._listener_thread.start()
        _LOGGER.info("hxj_lock: 已启动实时事件推送监听")

    def stop_push_listener(self) -> None:
        """停止后台推送监听"""
        self._listener_stop.set()
        client = self._listener_client
        self._listener_client = None
        self._listener_thread = None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def _handle_push_frame(self, frame: dict) -> None:
        """把服务端推送帧转成门锁事件, 走与轮询完全相同的三路分发"""
        data = frame.get("data") or {}
        did = data.get("deviceId")
        if not did:
            return
        if self.device_filter and data.get("deviceName") != self.device_filter:
            return
        log_id = data.get("logId")
        ts = int(data.get("createTime") or 0)
        if ts and ts <= self._newest_ts.get(did, 0):
            # 服务端在鉴权时会补推未读/历史事件: 早于已知最新事件的一律丢弃,
            # 否则 HA 每次重启都会把几天前的旧事件再通知一遍
            _LOGGER.debug("hxj_lock: 丢弃历史推送事件 logId=%s ts=%s",
                          log_id, ts)
            return
        seen = self._seen.setdefault(did, {})
        if log_id:
            if log_id in seen:
                return        # 轮询路径已处理过, 避免重复触发
            seen[log_id] = True
            if len(seen) > 500:
                seen.pop(next(iter(seen)))
        ev = {
            "time": fmt_time(data.get("createTime")),
            "ts": data.get("createTime"),
            "title": event_title(data),
            "logId": log_id,
            "logType": data.get("logType"),
            "logInfoType": data.get("logInfoType"),
            "keyName": data.get("keyName"),
            "category": event_category(data),
            "hasMedia": bool(data.get("mediaState") or data.get("videoState")),
        }
        _LOGGER.info("hxj_lock: 收到实时推送事件 %s / %s",
                     ev.get("title"), ev.get("time"))
        if ts > self._newest_ts.get(did, 0):
            self._newest_ts[did] = ts
        # 同步写回 coordinator.data, 让"最近事件"等实体立刻反映最新事件
        dev = (self.data or {}).get(did)
        if dev is not None:
            events = dev.setdefault("events", [])
            events.insert(0, ev)
            del events[20:]
            self.async_update_listeners()
        self._emit_event(did, ev)

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
            # async_create 是同步 @callback, 不能包 async_create_task
            pn_async_create(
                self.hass,
                GEETEST_HINT,
                title="慧享家门锁: 登录令牌失效",
                notification_id="hxj_lock_auth",
            )

        self.hass.loop.call_soon_threadsafe(_create)
