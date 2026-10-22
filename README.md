# 慧享家门锁 HA 集成 (custom component)

把慧享家 App 逆向还原的云锁协议打包成 Home Assistant 自定义集成。
**零第三方依赖**（DES/RSA 为纯 Python 实现，证书随包分发），直接放进
`config/custom_components/` 即可用。

> ⚠️ **免责声明**：本项目为个人逆向学习成果，仅供个人家庭自动化自用，
> 与慧享家/官方无任何关联，不提供任何担保。请自行承担使用风险，
> 勿用于任何商业或非法用途。

## 文件清单

```
custom_components/hxj_lock/
├── __init__.py          集成入口 / 服务注册
├── manifest.json        清单 (requirements 为空)
├── const.py             常量
├── config_flow.py       UI 配置流程 (账号/密码/极验令牌/reauth/选项)
├── coordinator.py       数据协调器 (轮询 + 实时推送监听)
├── lock.py              锁实体 (远程开锁)
├── binary_sensor.py     在线状态
├── sensor.py            最近事件
├── event.py             事件实体 (开门/关门/报警/低电量…)
├── device_trigger.py    设备触发器 (自动化用"门锁事件"触发)
├── protocol.py          协议客户端 (HSP 帧 + DES-CBC + HMAC 签名)
├── hxj_crypto.py        纯 Python 单 DES / RSA
├── client.crt / client.key   客户端证书 (提取自 App 的 client.p12)
├── strings.json         翻译源
├── translations/en.json
└── services.yaml        服务描述
```

## 安装

### 方式一：HACS（推荐）

1. HACS → 右上角菜单 → **自定义仓库 (Custom repositories)**
2. 仓库地址填 `https://github.com/Devilfrom/hxj-lock-ha`，类别选 `Integration`
3. 在 HACS 里搜索并安装 **慧享家门锁**，装完重启 HA
4. 设置 → 设备与服务 → 添加集成 → 搜索 "慧享家门锁"

### 方式二：手动复制

1. 把 `custom_components` 目录整个复制到 HA 配置目录:
   - HA OS / Container: `/config/custom_components/`
   - 结果: `/config/custom_components/hxj_lock/__init__.py` 存在
2. 重启 HA (或 开发者工具 → YAML → 重载, 但新集成需重启才被发现)
3. 设置 → 设备与服务 → 添加集成 → 搜索 "慧享家门锁"

## 首次配置 (关键: 极验验证码)

慧享家服务器强制要求登录前过**极验验证码**，集成无法在 HA 里弹验证码页面，
采用"人机结合"方案:

1. 在电脑上运行逆向工作区的 `geetest_login.py`:
   ```
   python geetest_login.py
   ```
   浏览器打开 `http://127.0.0.1:8899` 滑过验证码 → 自动登录 →
   生成 `login_result.json` (内含 tokenId)。
2. HA 添加集成时:
   - 填**账号** (如 13xxxxxxxxx)
   - 把 `login_result.json` 里的 `tokenId` 整串粘进 **tokenId** 字段
   - 密码可留空 (有令牌就不需要)
   - 设备名筛选留空 = 账号下全部云锁
3. 完成。集成会立即拉取设备列表并创建实体。

> tokenId 实测可长期复用。失效时 HA 会弹出"需要重新身份验证"，
> 点进去粘贴新 tokenId 即可；也可调用服务 `hxj_lock.set_token` 自动化更新。

## 实体

| 实体 | 类型 | 说明 |
|---|---|---|
| `lock.<设备名>` | lock | 显示 locked/unlocked; `unlock` 服务 = 远程开锁 |
| `binary_sensor.<设备名>_在线状态` | connectivity | deviceState==3 为在线; 属性含 rssi/MAC/版本 |
| `sensor.<设备名>_最近事件` | sensor | 如 `2026-10-21 19:10:02 开门 钥匙:002`; 属性含最近 N 条事件 |
| `event.<设备名>_事件` | event | 事件实体, 状态即最近一次事件类型 (open/close/alarm/…); 属性含 title/log_id/time/has_media |

注意: 云端协议不反馈门闩实际开/关状态, 锁实体默认显示 locked,
执行开锁后 10 秒内显示 unlocked, 之后恢复。

> **关于"远程上锁"**: 云锁通道 (`appCloudDeviceList`) 逆出的命令里只有
> `appCloudOpenLock`, **没有远程上锁/关门接口** —— `appCloseDoorLock` 属于
> 普通网关门锁通道, 与云锁无关。所以本集成只暴露 `LockEntityFeature.OPEN`,
> 关门靠门体机械回锁, 完成后再以"关门"事件上报。`lock.lock` 服务对该实体
> 不可用属预期行为, 不是 bug。

## 门锁事件通知

开门 / 关门 / 报警 / 低电量 / 新增删除用户 / 门前抓拍等事件会**同时**走三条通道:

| 通道 | 用途 | 延迟 |
|---|---|---|
| **事件实体** `event.<设备名>_事件` | 状态变为事件类型, 可用"事件"触发器写自动化 | 事件入云后 3~5 秒 |
| **总线事件** `hxj_lock_event` | 载荷含 `device_id` / `event_type` / `title` / `log_id` / `time` / `has_media` | 同上 |
| **HA 持久通知** | 前端通知面板弹出"门锁事件 · 开门" (选项里可关) | 同上 |

> 端到端延迟 = 设备上报到云端 (实测 10~70 秒) + 事件送达 (3~5 秒)。
> 远程开锁的实测端到端为 **18 秒**。

事件来源是**服务端实时推送** `appCloudLockEventPush`: 集成在后台维持一条长连接,
服务端把事件排队, 在客户端下次鉴权时下发 (空闲约 90s 会断, 集成每 20s 主动重连
一次以尽快取走)。同时保留 `appCloudLockLogList` 轮询作为兜底, 两条路径按
`logId` 去重, 同一事件只通知一次。

> 持久通知是**内存态**的, HA 重启后消失。要留痕 / 推手机请用通知服务
> (如 WxPusher), 见下方自动化示例。

用设备触发器 (推荐, 可按事件类型细分):

```yaml
automation:
  - alias: 门锁事件通知
    trigger:
      - platform: device
        domain: hxj_lock
        device_id: <设备ID>          # 设备 → 设备ID, 或在 UI 里选
        type: open                  # open / close / alarm / low_battery / add_user / del_user / cat_eye / other
    action:
      - service: notify.wxpusher
        data:
          title: 门锁事件
          message: "{{ trigger.title }} @ {{ trigger.time }}"
```

设备触发器可用变量 (已按 HA 标准触发器范式构造 `trigger` 字典):

| 变量 | 含义 |
|---|---|
| `trigger.title` | 事件标题, 如 `开门 钥匙:18074736303` |
| `trigger.time` | 事件发生时间, 如 `2026-10-22 12:41:54` |
| `trigger.event_type` | `open` / `close` / `alarm` / `low_battery` / … |
| `trigger.log_id` | 云端日志 id (可用来取抓拍) |
| `trigger.has_media` | 该事件是否带抓拍/视频 |
| `trigger.event.*` | 上面几项的子字典形式, 与 event 平台触发器一致 |
| `trigger.device_id` / `trigger.type` | 触发器本身的信息 |

用 WxPusher 推微信 (HA 里配好 `rest_command.wxpusher` 后):

```yaml
automation:
  - alias: 门锁事件推微信
    trigger:
      - platform: device
        domain: hxj_lock
        device_id: <设备ID>
        type: open
    action:
      - service: rest_command.wxpusher
        data:
          title: 门锁事件
          message: "{{ trigger.title }} @ {{ trigger.time }}"
    mode: parallel
    max: 10
```

用事件实体:

```yaml
automation:
  - alias: 门锁事件通知 (事件实体)
    trigger:
      - platform: event
        event_type: event.men_kou_da_men_suo_3_shi_jian
    action:
      - service: notify.wxpusher
        data:
          title: "{{ trigger.event.data.title }}"
          message: "{{ trigger.event.data.time }}"
```

## 服务

| 服务 | 用途 |
|---|---|
| `hxj_lock.set_token` | 更新 tokenId (字段 token_id), 自动重载集成 |
| `hxj_lock.open_lock` | 远程开锁; 可传 device_id, 留空 = 所有锁 |
| `hxj_lock.refresh` | 立即刷新数据 |

HA 自动化里远程开锁示例:

```yaml
automation:
  - alias: 门口来人开门
    trigger:
      - platform: event
        event_type: mobile_app_notification_event
    action:
      - service: hxj_lock.open_lock
        data:
          device_id: ""     # 留空开所有; 或填具体 deviceId
```

## 选项

集成 → 齿轮 → 选项:

| 选项 | 默认 | 说明 |
|---|---|---|
| 轮询间隔 (秒) | 300 | 轮询兜底间隔; 实时推送不受它影响 |
| 日志条数 | 5 | 每次拉取的开门记录条数 |
| 设备名筛选 | 空 | 留空 = 账号下全部云锁; 填设备名 = 只匹配该锁 |
| 事件通知 | 开 | 关掉后不再弹 HA 持久通知 (事件实体与总线事件仍照发) |

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 配置时提示"账号/密码/令牌无效" | 没粘 tokenId, 密码登录被极验拦截 | 先跑 geetest_login.py 拿 tokenId |
| 弹出"需要重新身份验证" | tokenId 失效 | 重跑 geetest_login.py → `hxj_lock.set_token` 或 reauth 粘贴 |
| 日志 `UpdateFailed` | 网络不通/服务端关闭连接 | 自动在下轮重试; 检查 HA 到 iot.hxjiot.com:9595 连通性 |
| 设备列表为空 | 账号下没有云锁 (只有普通门锁) | App 里确认设备类型; 普通门锁通道见逆向报告 2.5 |
| 集成一直 `not_loaded`, 日志报 `Unable to import component: No module named 'custom_components.hxj_lock.xxx'` | **`custom_components/` 下存在带 `manifest.json` 的备份目录** (如 `hxj_lock.bak`) | HA 会把每个子目录都当集成加载, 含点号的目录名不是合法 Python 包名 → 整个 domain 无法 setup, 条目永远 `not_loaded`。**把备份目录移出 `custom_components/`** (放 `/config/` 或别处) |
| 实体突然全部 `unavailable` | 云端瞬时返回空设备列表 | 集成已做保护 (保留上次数据), 不会误判离线; 日志可见 `保留上次数据` |
| 前端通知面板没有门锁通知 | 持久通知是内存态, HA 重启后清空 | 属预期行为; 要留痕请用 WxPusher 等通知服务 |

开启调试日志:

```yaml
logger:
  logs:
    custom_components.hxj_lock: debug
```

## HA 版本兼容性

本集成在 **HA 2026.6.2** 上开发验证。新版 HA 改了几个底层 API, 旧版 HA 也能跑
(代码里做了兼容), 但如果你要在 2026.6+ 上二次开发, 注意这几处:

| API | 2026.6 的变化 | 本集成的做法 |
|---|---|---|
| `EventEntity` | `async_fire_event(dict)` 已删除, 只能用 `_trigger_event(event_type, attrs)` | 用 `_trigger_event`, 并校验 event_type 在允许列表内 |
| `LockEntityFeature` | **只剩 `OPEN`**, `LOCK` / `UNLOCK` 枚举被移除 | 只声明 `OPEN`; 另覆写 `async_unlock` 兼容, `async_lock` 给明确报错 |
| `HomeAssistant.helpers` | 已移除, 不能用 `self.hass.helpers.event.async_call_later` | 改用模块级 `homeassistant.helpers.event.async_call_later(hass, delay, action)` |
| `persistent_notification.async_create` | 是**同步 `@callback`** (返回 None) | 直接调用, 不要包 `async_create_task` |
| `device_trigger.async_validate_trigger_config` | 签名多了 `hass` 参数 | 写成同时接受 `(hass, config)` 与 `(config)` |

> ⚠️ `device_trigger` 的回调签名写错会让**整个 automation 域 setup 失败**
> (所有自动化都变 unavailable), 排查时先看日志里有没有
> `Setup failed for 'automation': Invalid config`。

## 与独立脚本的关系

`workspace_base/hxj_to_ha.py` (REST 推送脚本) 功能与本集成等价,
适合青龙面板/无 HA 自定义组件场景。两边可任选其一, 不要同时用
(会重复拉数据, 但协议上无冲突)。
