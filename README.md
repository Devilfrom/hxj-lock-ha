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
├── coordinator.py       数据协调器 (每轮独立 TCP 连接)
├── lock.py              锁实体 (远程开锁)
├── binary_sensor.py     在线状态
├── sensor.py            最近事件
├── protocol.py          协议客户端 (HSP 帧 + DES-CBC + HMAC 签名)
├── hxj_crypto.py        纯 Python 单 DES / RSA
├── client.crt / client.key   客户端证书 (提取自 App 的 client.p12)
├── strings.json         翻译源
├── translations/en.json
└── services.yaml        服务描述
```

## 安装

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

注意: 云端协议不反馈门闩实际开/关状态, 锁实体默认显示 locked,
执行开锁后 10 秒内显示 unlocked, 之后恢复。

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

集成 → 齿轮 → 选项: 轮询间隔 (默认 300 秒) / 日志条数 (默认 5) / 设备名筛选。

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 配置时提示"账号/密码/令牌无效" | 没粘 tokenId, 密码登录被极验拦截 | 先跑 geetest_login.py 拿 tokenId |
| 弹出"需要重新身份验证" | tokenId 失效 | 重跑 geetest_login.py → `hxj_lock.set_token` 或 reauth 粘贴 |
| 日志 `UpdateFailed` | 网络不通/服务端关闭连接 | 自动在下轮重试; 检查 HA 到 iot.hxjiot.com:9595 连通性 |
| 设备列表为空 | 账号下没有云锁 (只有普通门锁) | App 里确认设备类型; 普通门锁通道见逆向报告 2.5 |

开启调试日志:

```yaml
logger:
  logs:
    custom_components.hxj_lock: debug
```

## 与独立脚本的关系

`workspace_base/hxj_to_ha.py` (REST 推送脚本) 功能与本集成等价,
适合青龙面板/无 HA 自定义组件场景。两边可任选其一, 不要同时用
(会重复拉数据, 但协议上无冲突)。
