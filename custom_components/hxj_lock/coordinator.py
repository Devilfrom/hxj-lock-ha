"""慧享家门锁数据协调器 — 轮询 + 服务端实时推送 双通道"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.components.persistent_notification import async_create as pn_async_create

from .const import (
    CONF_ACCOUNT, CONF_ANDROID_ID, CONF_DEVICE_NAME, CONF_LOG_COUNT,
    CONF_NOTIFY, CONF_SCAN_INTERVAL, CONF_TOKEN_ID, DEFAULT_LOG_COUNT,
    DEFAULT_NOTIFY, DEFAULT_SCAN_INTERVAL, DEVICE_TYPE_CLOUD_LOCK,
    DOOR_STATE_FALLBACK, DOMAIN, EVENT_FIRE_TYPE, EVENT_TYPES, GEETEST_HINT,
    LOCAL_EVENT_MERGE_WINDOW,
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
        # 被丢弃的历史推送计数(只用于日志聚合)
        self._stale_push_count = 0
        self._initialized = False
        # 门锁事件订阅者 (event 实体用它实时更新自身状态)
        self._event_sinks: list = []
        # 本地乐观事件: 开锁指令云端确认后立即产生, 不等云端落库(要 20~70 秒)
        # did -> [{"ts": 本地时间戳, "category": 事件类型}]
        self._local_pending: dict[str, list] = {}
        # 门状态 (由 open/close 事件推导): did -> {"state","ts","source","title"}
        self._door_state: dict[str, dict] = {}
        # 门状态兜底定时器: 开门后若一直没收到"关门"事件, 判定为已机械回锁
        self._door_timer: dict[str, callable] = {}
        # 门状态变化回调 (锁实体用它刷新状态)
        self._door_sinks: list = []
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
            # 首次刷新只建基线, 不把历史事件回放给 event 实体/通知;
            # 但门状态要从云端日志恢复 (否则 HA 重启后门状态一直是"未知")
            self._restore_door_state(devices)
            return
        for did, ev in new:
            # 门状态由事件推导 (开门/关门)
            self._apply_door_state(did, ev)
            # 与本地乐观事件是同一次开门 -> 不重复通知
            if self._is_duplicate_of_local(did, ev):
                continue
            self._emit_event(did, ev)

    def _restore_door_state(self, devices: dict) -> None:
        """从云端日志恢复门状态 (HA 重启后不至于一直是"未知")。

        取每台锁最近的一条开门/关门事件:
          - 是开门事件, 且距今不超过 DOOR_STATE_FALLBACK -> 判为"开"
          - 否则 -> "关"
        """
        now = int(time.time())
        for did, dev in (devices or {}).items():
            for ev in dev.get("events", []):
                category = ev.get("category")
                if category not in ("open", "close"):
                    continue
                ts = int(ev.get("ts") or 0)
                if category == "open" and now - ts <= DOOR_STATE_FALLBACK:
                    state = "open"
                else:
                    state = "closed"
                self._door_state[did] = {
                    "state": state,
                    "ts": ts,
                    "source": "restore",
                    "title": ev.get("title"),
                }
                _LOGGER.info("hxj_lock: %s 门状态从云端日志恢复为 %s (%s)",
                             did, state, ev.get("time"))
                if state == "open":
                    self._arm_door_fallback(did)
                break

    # ---------- 门状态 (由 open/close 事件推导) ----------
    def _apply_door_state(self, did: str, ev: dict) -> None:
        """根据开门/关门事件更新门状态。

        云端协议**不下发门闩状态** (设备列表里只有 deviceState 在线状态), 且
        "关门"事件上报极少 (实测 100 条日志里只有 4 条关门 vs 90 条开门),
        所以门状态只能由事件推导:
          - 收到 open  -> 开
          - 收到 close -> 关
          - 开门后 DOOR_STATE_FALLBACK 秒仍无关门事件 -> 视为已机械回锁, 置为关
        """
        category = ev.get("category")
        ts = int(ev.get("ts") or time.time())
        if category == "open":
            state = "open"
        elif category == "close":
            state = "closed"
        else:
            return      # 其它类型不改变门状态
        prev = self._door_state.get(did) or {}
        changed = prev.get("state") != state
        # 状态相同也要刷新元信息 (本地乐观事件 -> 云端真实事件, 谁新用谁)
        self._door_state[did] = {
            "state": state,
            "ts": ts,
            "source": ev.get("source", "cloud"),
            "title": ev.get("title"),
        }
        if changed:
            _LOGGER.info("hxj_lock: %s 门状态 -> %s (%s)", did, state,
                         ev.get("title"))
        self._notify_door_sinks(did)
        # 兜底定时器只在**状态发生变化**时重新计时。
        # 否则同一次开门会因"本地事件 / 云端推送 / 轮询"三条路径各 arm 一次,
        # 定时器被不断推后, 永远等不到兜底回锁。
        if not changed:
            return
        # 开门后启动兜底定时器: 云端关门事件缺失时, 到点判定为已回锁
        if state == "open":
            self._arm_door_fallback(did)
        else:
            self._cancel_door_fallback(did)

    def _notify_door_sinks(self, did: str) -> None:
        """通知门状态订阅者 (锁实体用它刷新状态)。

        订阅者会写实体状态, 必须保证在事件循环线程执行。
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not self.hass.loop:
            self.hass.loop.call_soon_threadsafe(self._notify_door_sinks, did)
            return
        for sink in list(self._door_sinks):
            try:
                sink(did)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("hxj_lock: 门状态订阅者回调异常")

    def _safe_update_listeners(self) -> None:
        """触发实体刷新, 并保证在事件循环线程执行。

        HA 2026.6 的事件循环不在主线程, 而轮询刷新走 executor 线程,
        所以任何写实体状态的动作都必须切回事件循环, 否则会报
        "calls async_write_ha_state from a thread other than the event loop"。
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.hass.loop:
            self.async_update_listeners()
        else:
            self.hass.loop.call_soon_threadsafe(self.async_update_listeners)

    def register_door_sink(self, sink) -> None:
        if sink not in self._door_sinks:
            self._door_sinks.append(sink)

    def unregister_door_sink(self, sink) -> None:
        if sink in self._door_sinks:
            self._door_sinks.remove(sink)

    def _arm_door_fallback(self, did: str) -> None:
        """开门后启动兜底定时器 (关门事件缺失时按机械回锁处理)。

        注意: HA 2026.6 的事件循环**不在主线程**跑, 所以不能用
        "当前线程是不是主线程"来判断能否直接调度。async_call_later 内部是
        `hass.loop.call_at(...)`, 从别的线程调用会让定时器在错误的线程上触发,
        触发时 async_write_ha_state 会抛
        "calls async_write_ha_state from a thread other than the event loop"。
        这里统一用 hass.loop.call_soon_threadsafe 把动作切回事件循环再执行。
        """
        # async_call_later 内部是 hass.loop.call_at(), 必须在事件循环线程调用
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not self.hass.loop:
            self.hass.loop.call_soon_threadsafe(self._arm_door_fallback, did)
            return
        self._cancel_door_fallback(did)

        def _fire(*_args) -> None:
            self._door_timer.pop(did, None)
            # 定时器可能不在事件循环线程触发, 统一切回去
            self.hass.loop.call_soon_threadsafe(self._do_door_fallback, did)

        try:
            self._door_timer[did] = async_call_later(
                self.hass, DOOR_STATE_FALLBACK, _fire)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("hxj_lock: 门状态兜底定时器启动失败")

    def _do_door_fallback(self, did: str) -> None:
        """兜底回锁的实际处理 (确保在事件循环线程执行)"""
        st = self._door_state.get(did) or {}
        if st.get("state") != "open":
            return
        self._door_state[did] = {
            **st, "state": "closed", "source": "fallback",
        }
        _LOGGER.info(
            "hxj_lock: %s 开门后 %ds 未收到关门事件, 按机械回锁处理",
            did, DOOR_STATE_FALLBACK)
        self._notify_door_sinks(did)
        self._safe_update_listeners()

    def _cancel_door_fallback(self, did: str) -> None:
        cancel = self._door_timer.pop(did, None)
        if cancel is not None:
            try:
                cancel()
            except Exception:  # noqa: BLE001
                pass

    def _in_event_loop(self) -> bool:
        """当前是否在 HA 事件循环线程里"""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return False
        except Exception:  # noqa: BLE001
            return True
        return running is self.hass.loop

    def get_door_state(self, did: str) -> dict:
        """返回门状态: {"state": "open"/"closed"/"unknown", ...}"""
        st = self._door_state.get(did)
        if not st:
            return {"state": "unknown", "ts": 0, "source": None, "title": None}
        return st

    # ---------- 本地乐观事件 (消除开锁后的延迟真空期) ----------
    def _emit_local_open(self, did: str) -> None:
        """开锁指令云端确认成功后, 立即本地产生一个"开门"事件。

        背景: 云端把开门记录落库要 20~70 秒, 推送又只在"下次鉴权"时下发,
        用户点开门后要等 20~90 秒才看到事件 —— 中间是一段"真空期"。
        这里在指令返回的瞬间就先把事件发出去 (事件实体 / bus 事件 / 通知),
        云端真实事件稍后到达时按时间窗合并, 不会重复通知。
        """
        now = int(time.time())
        ev = {
            "time": fmt_time(now),
            "ts": now,
            "title": "开门 钥匙:远程",
            "logId": None,
            "logType": 1,
            "logInfoType": 0,
            "keyName": "远程",
            "category": "open",
            "hasMedia": False,
            "imageUrl": None,
            "smallImageUrl": None,
            "source": "local",
        }
        # 记下这次本地事件, 云端真实事件到达时用来识别"同一次开门"
        self._local_pending.setdefault(did, []).append(
            {"ts": now, "category": "open"})
        _LOGGER.info("hxj_lock: %s 开锁指令已确认, 立即产生本地开门事件", did)
        self._apply_door_state(did, ev)
        self._write_back_event(did, ev)
        self._emit_event(did, ev)

    def _is_duplicate_of_local(self, did: str, ev: dict) -> bool:
        """云端事件是否与本地乐观事件是"同一次开门"。

        云端开门记录的落库延迟 20~70 秒, 落库后其 createTime 仍是开门那一刻
        的时间, 所以按时间窗 + 事件类型判定即可。

        还要校验 keyName: 远程开锁在云端的 keyName 就是**账号手机号**
        (实测 "开门 钥匙:18074736303"), 而物理钥匙开门是 002/004 等编号。
        这样就不会把"刚远程开锁后有人用钥匙开门"误判成同一次而漏掉通知。
        """
        pending = self._local_pending.get(did)
        if not pending:
            return False
        ts = int(ev.get("ts") or 0)
        category = ev.get("category")
        key_name = ev.get("keyName")
        # 只合并"由本账号远程开锁"产生的那条云端记录
        if self.account and key_name and key_name != self.account:
            return False
        for item in list(pending):
            if item["category"] != category:
                continue
            if abs(ts - item["ts"]) <= LOCAL_EVENT_MERGE_WINDOW:
                pending.remove(item)
                if not pending:
                    self._local_pending.pop(did, None)
                # 把云端 logId 也记入已见集合, 否则轮询路径会把它当成新事件
                # 再触发一次通知 (推送路径命中合并时不会走到下面的 _seen 登记)
                lid = ev.get("logId")
                if lid:
                    seen = self._seen.setdefault(did, {})
                    seen[lid] = True
                    if len(seen) > 500:
                        seen.pop(next(iter(seen)))
                _LOGGER.info(
                    "hxj_lock: %s 云端事件与本地乐观事件合并 (同一次开门), 不重复通知",
                    did)
                return True
        return False

    def _write_back_event(self, did: str, ev: dict) -> None:
        """把事件写回 coordinator.data, 让"最近事件"等实体立刻反映最新事件"""
        dev = (self.data or {}).get(did)
        if dev is None:
            return
        events = dev.setdefault("events", [])
        # 同一次开门 (本地乐观事件 + 云端真实事件) 只保留一条
        if ev.get("logId"):
            events[:] = [e for e in events
                         if not (e.get("logId") is None
                                 and e.get("category") == ev.get("category")
                                 and abs(int(e.get("ts") or 0)
                                         - int(ev.get("ts") or 0))
                                 <= LOCAL_EVENT_MERGE_WINDOW)]
        events.insert(0, ev)
        del events[20:]
        if ev.get("imageUrl"):
            # 新的门前抓拍: 更新快照
            dev["snapshot"] = {
                "log_id": ev.get("logId"),
                "time": ev.get("time"),
                "ts": ev.get("ts"),
                "title": ev.get("title"),
                "image_url": ev.get("imageUrl"),
                "thumb_url": ev.get("smallImageUrl"),
            }
        self._safe_update_listeners()

    def _emit_event(self, did: str, ev: dict) -> None:
        """门锁事件统一分发: 事件实体 + bus 事件 + 持久通知"""
        category = ev.get("category") or "other"
        meta = EVENT_TYPES.get(category, EVENT_TYPES["other"])
        payload = {
            "device_id": did,
            "event_type": category,
            "event_type_name": meta.get("name"),
            "title": ev.get("title"),
            "log_id": ev.get("logId"),
            "time": ev.get("time"),
            "has_media": ev.get("hasMedia"),
            "image_url": ev.get("imageUrl"),
            "source": ev.get("source", "cloud"),
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
        message = f"{payload.get('title')}（{payload.get('time')}）"
        if payload.get("image_url"):
            # 门前抓拍: 把图片地址一并带出来, 点开通知即可看图
            message += f"\n抓拍: {payload['image_url']}"
        # 注意: persistent_notification.async_create 是 @callback 同步函数 (返回 None),
        # 不能用 async_create_task 包一层, 否则会抛 "A valid coroutine must be provided"
        pn_async_create(
            self.hass,
            message=message,
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
                    "imageUrl": r.get("imageUrl"),
                    "smallImageUrl": r.get("smallImageUrl"),
                } for r in logs]
                events.sort(key=lambda x: x.get("ts") or 0, reverse=True)
                # 最近一次带门前抓拍图的记录 (events 已按时间倒序)
                snapshot = None
                for ev in events:
                    if ev.get("imageUrl"):
                        snapshot = {
                            "log_id": ev.get("logId"),
                            "time": ev.get("time"),
                            "ts": ev.get("ts"),
                            "title": ev.get("title"),
                            "image_url": ev.get("imageUrl"),
                            "thumb_url": ev.get("smallImageUrl"),
                        }
                        break
                if snapshot is None:
                    # 本次拉取的窗口里没有带媒体的记录(抓拍/门口事件本来就稀疏),
                    # 保留上一次的快照, 免得旧抓拍被后续几条开门记录顶掉
                    snapshot = (self.data or {}).get(did, {}).get("snapshot")
                    if snapshot is None:
                        # 还没有过快照: 再深扫一次(200 条)找最近一次门前抓拍。
                        # 只在首次遇到时做这一次, 与事件窗口大小解耦 ——
                        # 否则窗口一小(重启/改选项)抓拍图就空了。
                        snapshot = self._fetch_snapshot_deep(client, did)
                devices[did] = {"info": d, "events": events, "snapshot": snapshot,
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
            # 否则 HA 每次重启都会把几天前的旧事件再通知一遍。
            # 服务端每次重连都补推全部历史, 所以这里聚合计数, 避免刷屏。
            self._stale_push_count += 1
            if self._stale_push_count % 20 == 1:
                _LOGGER.debug(
                    "hxj_lock: 已丢弃 %d 条历史推送事件 (早于已知最新事件)",
                    self._stale_push_count)
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
            "imageUrl": data.get("imageUrl"),
            "smallImageUrl": data.get("smallImageUrl"),
        }
        _LOGGER.info("hxj_lock: 收到实时推送事件 %s / %s",
                     ev.get("title"), ev.get("time"))
        if ts > self._newest_ts.get(did, 0):
            self._newest_ts[did] = ts
        # 同步写回 coordinator.data, 让"最近事件"等实体立刻反映最新事件
        self._write_back_event(did, ev)
        # 门状态由事件推导
        self._apply_door_state(did, ev)
        # 与本地乐观事件是同一次开门 -> 不重复通知 (但属性仍会更新)
        if self._is_duplicate_of_local(did, ev):
            return
        self._emit_event(did, ev)

    # ---------- 门前抓拍 ----------
    @staticmethod
    def _fetch_snapshot_deep(client: HxjClient, device_id: str) -> dict | None:
        """深扫日志窗口, 找最近一条带门前抓拍图的记录 (仅在还没有快照时调用一次)"""
        try:
            resp = client.cloud_lock_log(device_id, 0, 200, DEVICE_TYPE_CLOUD_LOCK)
        except (HxjProtocolError, HxjAuthError):
            return None
        logs = (resp.get("data") or {}).get("list") or []
        media = [r for r in logs if r.get("imageUrl")]
        if not media:
            return None
        row = max(media, key=lambda x: x.get("createTime") or 0)
        return {
            "log_id": row.get("logId"),
            "time": fmt_time(row.get("createTime")),
            "ts": row.get("createTime"),
            "title": event_title(row),
            "image_url": row.get("imageUrl"),
            "thumb_url": row.get("smallImageUrl"),
        }

    async def async_get_snapshot(self, device_id: str | None = None) -> dict:
        """返回最近一次门前抓拍信息 (含图片地址)

        注意: HA 的 REST 服务调用端点固定返回 [], 服务返回值要通过
        WebSocket call_service(return_response=True) 或自动化里的
        response_variable 变量才能拿到。
        """
        devices = self.data or {}
        if device_id:
            dev = devices.get(device_id)
            if dev is None:
                raise UpdateFailed(f"未找到设备 {device_id}")
            return dev.get("snapshot") or {}
        snaps = {}
        for did, dev in devices.items():
            snap = dev.get("snapshot")
            if snap:
                snaps[did] = {**snap, "device_id": did}
        if len(snaps) == 1:
            return next(iter(snaps.values()))
        return snaps

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
            # 云端已确认开锁 -> 立即本地产生开门事件, 不等云端落库
            # (落库要 20~70 秒, 否则用户点开门后有 10 秒以上的延迟真空期)
            resp = results.get(did) or {}
            if resp.get("resultCode") == 0:
                self._emit_local_open(did)
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
