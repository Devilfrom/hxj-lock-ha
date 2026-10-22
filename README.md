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
├── coordinator.py       数据协调器 (轮询 + 实时推送监听 + 本地乐观事件 + 门状态推导)
├── lock.py              锁实体 (远程开锁, 状态随门状态事件联动)
├── binary_sensor.py     在线状态 + 门状态 (device_class=door)
├── sensor.py            最近事件 + 门锁状态 (中文文字)
├── event.py             事件实体 (开门/关门/报警/低电量…)
├── image.py             门前抓拍图片实体
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
| `lock.<设备名>` | lock | 显示 locked/unlocked; `unlock` 服务 = 远程开锁; 属性含 `door_state` / `door_state_name` |
| `binary_sensor.<设备名>_在线状态` | connectivity | deviceState==3 为在线; 属性含 rssi/MAC/版本 |
| `binary_sensor.<设备名>_门状态` | door | **门是开是关**: on=开 / off=关; 属性含 `state_name`(开/关) / `last_event` / `source` |
| `sensor.<设备名>_门锁状态` | sensor | 门状态的中文文字: `开门` / `关门` / `未知`, 图标跟随 |
| `sensor.<设备名>_最近事件` | sensor | 如 `2026-10-21 19:10:02 开门 钥匙:002`; 属性含最近 20 条事件 (每条带 `type` / `type_name`) |
| `event.<设备名>_事件` | event | 事件实体; 属性含 `event_type`(open/close/…) / **`event_type_name`(开门/关门/…)** / `title` / `time` / `has_media` |
| `image.<设备名>_门前抓拍` | image | 最近一次门口抓拍图; 状态为图片更新时间, 图片经 HA 图片代理加载 |

> **关于门状态**: 云端协议**不下发门闩状态** (设备列表里只有 `deviceState` 在线
> 状态), 所以门状态是由**开门/关门事件推导**出来的:
> - 收到"开门"事件 → 开; 收到"关门"事件 → 关;
> - 云端"关门"事件上报极少 (实测 100 条日志里只有 4 条关门 vs 90 条开门),
>   所以开门后若 180 秒仍没收到关门事件, 按"门体已机械回锁"处理, 自动置为关;
> - HA 重启后门状态会从云端日志自动恢复, 不会一直是"未知"。
>
> **关于"远程上锁"**: 云锁通道 (`appCloudDeviceList`) 逆出的命令里只有
> `appCloudOpenLock`, **没有远程上锁/关门接口** —— `appCloseDoorLock` 属于
> 普通网关门锁通道, 与云锁无关。所以本集成只暴露 `LockEntityFeature.OPEN`,
> 关门靠门体机械回锁, 完成后再以"关门"事件上报。`lock.lock` 服务对该实体
> 不可用属预期行为, 不是 bug。

## 门锁事件通知

开门 / 关门 / 报警 / 低电量 / 新增删除用户 / 门前抓拍等事件会**同时**走三条通道:

| 通道 | 用途 | 延迟 |
|---|---|---|
| **事件实体** `event.<设备名>_事件` | 状态变为事件类型, 可用"事件"触发器写自动化 | **本机开锁 ~1 秒**; 他人开锁 20~90 秒 |
| **总线事件** `hxj_lock_event` | 载荷含 `device_id` / `event_type` / `event_type_name` / `title` / `log_id` / `time` / `has_media` / `source` | 同上 |
| **HA 持久通知** | 前端通知面板弹出"门锁事件 · 开门" (选项里可关) | 同上 |

> **本机远程开锁的延迟已优化到 ~1 秒**: 云端把开门记录落库要 **20~70 秒**,
> 而推送只在客户端"下次鉴权"时才下发 (服务端把事件排队) —— 等云端事件会有一段
> 20~90 秒的"真空期"。现在开锁指令云端确认成功后**立即在本地产生事件**
> (事件实体 / 总线事件 / 持久通知三路同时发出), 云端真实事件稍后到达时按
> **时间窗 + keyName** 识别为"同一次开门"并**静默合并**, 不会重复通知。
>
> 设备侧用钥匙/指纹开门 (非本机触发) 则仍要等云端落库, 延迟 20~90 秒。

事件来源是**服务端实时推送** `appCloudLockEventPush`: 集成在后台维持一条长连接,
服务端把事件排队, 在客户端下次鉴权时下发 (空闲约 90s 会断, 集成每 15s 主动重连
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
          title: "门锁 · {{ trigger.event_type_name | default(trigger.type) }}"
          message: "{{ trigger.title }} @ {{ trigger.time }}"
```

设备触发器可用变量 (已按 HA 标准触发器范式构造 `trigger` 字典):

| 变量 | 含义 |
|---|---|
| `trigger.title` | 事件标题, 如 `开门 钥匙:18074736303` |
| `trigger.time` | 事件发生时间, 如 `2026-10-22 12:41:54` |
| `trigger.event_type` | `open` / `close` / `alarm` / `low_battery` / … |
| `trigger.event_type_name` | **中文类型名**: `开门` / `关门` / `报警` / `低电量` / … |
| `trigger.log_id` | 云端日志 id (可用来取抓拍) |
| `trigger.has_media` | 该事件是否带抓拍/视频 |
| `trigger.source` | `local` = 本机远程开锁 (立即产生) / `cloud` = 云端上报 |
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
          title: "门锁 · {{ trigger.event_type_name | default(trigger.type) }}"
          message: >-
            {{ trigger.title | default(trigger.event_type_name) }}
            @ {{ trigger.time }}
            {%- if trigger.source == 'local' %} (HA 远程开锁){% endif %}
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
          title: "门锁 · {{ trigger.event.data.event_type_name }}"
          message: "{{ trigger.event.data.title }} @ {{ trigger.event.data.time }}"
```

## 门前抓拍与门外视频

### 抓拍图（已实现）

云锁的门口抓拍图地址**直接写在开门记录里**（`logType=3` 抓拍事件、`logType=4` 且
`logInfoType=15` 的视频事件会带 `imageUrl` / `smallImageUrl`），是
`file.hxjiot.com` 上的公网 JPEG。本集成据此做了两件事:

- **`image.<设备名>_门前抓拍` 实体** —— 显示最近一次抓拍图。图片经 HA 自己的图片代理
  (`/api/image_proxy/...`) 加载，所以云端地址不必对浏览器公网可达。
- **`hxj_lock.get_snapshot` 服务** —— 拿图片地址去推微信、做自动化。

抓拍图的查找与事件窗口解耦：窗口（选项里的"日志条数"）里没有带媒体的记录时，集成
会**额外深扫一次 200 条**去找最近一次抓拍（只在首次遇到时做），所以重启或调小窗口
都不会让抓拍图消失。

> **注意（实测）**：只有**门铃/报警/猫眼类事件**（`logType=3` 等）才带抓拍图，
> **普通开门/关门事件云端不生成抓拍**——`imageUrl` 只在带媒体日志里出现。
> 所以 `image.<设备名>_门前抓拍` 显示的是最近一次报警/门铃时的照片，
> 日期早于最近开门属于正常现象。想做"抓拍推送微信"自动化时，触发条件应选
> `alarm` / `cat_eye` 事件（有图可推），而不是开门事件。

### 门外视频（未实现，说明原因）

`appCloudLockMediaLogInfo` 能取到视频，但拿到的是**加密的私有流**:

| 字段 | 实测值 |
|---|---|
| `mediaUrl` | `https://oss.hxjiot.com/lockUpload/XXXX.mjif` |
| `mediaSecret` | 128 位 hex 串（每条视频不同） |
| `mediaOffset` | 异或密钥偏移（如 1074） |

App 下载后用 `MediaUtils.hxXorCipherMedia(content, mediaSecret, mediaOffset)` 解密，
再用自研 `VideoCodec` + `MediaCodec` 播放 —— `.mjif` 是私有容器格式，没有通用解码器。

要在 HA 里实现需要: ① 复刻 XOR 解密; ② 逆向 `.mjif` 容器与视频编码; ③ 转换成
HA camera 能吃的流 (HLS/fMP4)。工程量大且容错脆弱，因此本集成**只做抓拍图**。
如果确实需要视频，建议单独用 ffmpeg 拉流转封装，或直接用慧享家 App 看。

## 服务

| 服务 | 用途 |
|---|---|
| `hxj_lock.set_token` | 更新 tokenId (字段 token_id), 自动重载集成 |
| `hxj_lock.open_lock` | 远程开锁; 可传 device_id, 留空 = 所有锁 |
| `hxj_lock.refresh` | 立即刷新数据 |
| `hxj_lock.get_snapshot` | 返回最近一次门前抓拍信息 (含图片地址) |

> `get_snapshot` 的返回值要通过 **WebSocket `call_service` (`return_response: true`)**
> 或自动化里的 `response_variable` 变量取 —— HA 的 REST 服务调用端点固定返回 `[]`。

```yaml
- service: hxj_lock.get_snapshot
  response_variable: snap
- service: rest_command.wxpusher
  data:
    title: 门口抓拍
    message: "{{ snap.time }} {{ snap.title }} {{ snap.image_url }}"
```

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
| 日志条数 | 5 | 每次拉取的开门记录条数 (1-200)。窗口越大越容易回溯到历史门前抓拍 |
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
| 门状态一直是"未知" | 云端不下发门闩状态, 且日志里没有任何开门/关门事件可推导 | 触发一次开门/关门即可; HA 重启后会自动从云端日志恢复 |
| 门状态过一会儿自己从"开"变"关" | 云端"关门"事件上报极少, 开门后 180 秒仍无关门事件即按门体机械回锁处理 | 属预期行为; 关门事件到达时会立即变"关" |
| 点开门后事件很快又来一次 | 本机开锁会先本地产生事件, 云端真实事件到达后按时间窗+keyName 识别为同一次并静默合并 | 属预期行为, 只会通知一次; 日志可见 `合并 (同一次开门)` |
| 开门服务卡 30 秒 | 旧版协议跳过推送帧上限只有 20 帧, 新连接补推历史时刚好越界会误判"未收到响应"并重试 | 已修复 (上限 300 帧 + 只在连接层错误时重试), 现在实测 0.7~1.4 秒 |

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
