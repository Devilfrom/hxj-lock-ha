"""慧享家门锁集成常量"""
from homeassistant.const import Platform

DOMAIN = "hxj_lock"
TITLE = "慧享家门锁"
MANUFACTURER = "慧享家 (hxjiot)"

# 配置项
CONF_ACCOUNT = "account"
CONF_PASSWORD = "password"
CONF_TOKEN_ID = "token_id"
CONF_ANDROID_ID = "android_id"
CONF_DEVICE_NAME = "device_name"

# 选项
CONF_SCAN_INTERVAL = "scan_interval"
CONF_LOG_COUNT = "log_count"

DEFAULT_SCAN_INTERVAL = 300      # 秒
DEFAULT_LOG_COUNT = 5
DEVICE_TYPE_CLOUD_LOCK = 2       # appCloudOpenLock/appCloudLockLogList 固定 deviceType=2

PLATFORMS = [Platform.LOCK, Platform.BINARY_SENSOR, Platform.SENSOR]

# 服务
SERVICE_SET_TOKEN = "set_token"
SERVICE_OPEN_LOCK = "open_lock"
SERVICE_REFRESH = "refresh"

# token 失效时的提示 (极验人机结合登录)
GEETEST_HINT = (
    "慧享家登录需要过极验验证码（人机结合）。\n"
    "请在电脑上运行 geetest_login.py（来自逆向工作区），"
    "浏览器滑过验证码后得到 tokenId，\n"
    "然后调用服务 hxj_lock.set_token 填入，或重新添加集成时粘贴。"
)
