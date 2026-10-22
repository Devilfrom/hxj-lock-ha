#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
慧享家 (hxjiot.com) 智能门锁协议客户端 — Home Assistant 集成专用版

与 hxj_client.py 行为一致, 但:
  - 零第三方依赖 (DES/RSA 为纯 Python 实现, 见 hxj_crypto.py)
  - 客户端证书用随包 PEM 文件 (client.crt/client.key, 提取自 App 的 client.p12)
  - 时间用本地时钟 (HA 容器已 NTP 同步)
  - 线程安全: 所有 TCP 操作持锁串行
"""
import base64
import hashlib
import hmac
import json
import os
import random
import socket
import ssl
import struct
import threading
import time
import urllib.request
import urllib.error
import uuid

from .hxj_crypto import (des_cbc_decrypt, des_cbc_encrypt,
                         pkcs5_pad, pkcs5_unpad, rsa_encrypt_pkcs1_v15)

# ---------------- 协议常量 (ServerConfigImpl / HMacSha256Utils 还原) ----------------
SERVER_DOMAIN = "iot.hxjiot.com"
HTTPS_URL = "https://iot.hxjiot.com:11051"
TCP_PORT = 9595
CONTENT_VERSION = "7.6"
APP_COM = "HuiXiangJia"
APP_VERSION = "7.6.0"
PHONE_TYPE = "Android"
PHONE_SYSTEM = "Android14_34"
HSP_MARK = b"HSP"
HMAC_INIT_KEY = "RFs0QzqPK1T39flJr9AUdvQtNcv92aqweTGhca2su2wktupzskiyCU6OCnqtugxw"
RSA_PUBKEY = ("MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDIagxJDNQ3fWb1h8MMQgvtqKzm0"
              "WGW8JHYRn0T8GOm7OXfrxTE/aTaWRw9+rUsfrfCRsrzH7wE/A9jP/+koijdS2a/OuQ"
              "ab/aFQamxOhQTi7u/okeCnWqrZdc9FpNgBKv4oDu+WVoftPlUrDVoAhh0RdF3/4OGr"
              "SMdW8BxDaGEcwIDAQAB")

CERT_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_CRT = os.path.join(CERT_DIR, "client.crt")
CLIENT_KEY = os.path.join(CERT_DIR, "client.key")

# 云锁事件类型 (CatEyeLockDetailsActivityHelper.getCellIconImgName 还原)
LOG_TYPE_TITLE = {
    0: "关门", 1: "开门", 2: "报警", 3: "抓拍/视频事件",
}
LOG_INFO_TITLE = {  # logType=4 时按 logInfoType 细分
    3: "报警", 5: "关门", 6: "开锁", 7: "开锁", 16: "开锁", 17: "开锁",
    8: "低电量", 9: "新增用户",
    10: "删除用户", 11: "删除用户", 13: "删除用户", 32: "删除用户",
}


def md5_upper(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()


def rsa_encrypt_b64(data: bytes) -> str:
    return base64.b64encode(rsa_encrypt_pkcs1_v15(RSA_PUBKEY, data)).decode()


def gen_des_key() -> str:
    """DESTool.generateDESKey 等价: 8 字节(含奇校验位) -> Base64"""
    kb = bytearray(os.urandom(8))
    for i in range(8):
        b = kb[i] & 0xFE
        if bin(b).count("1") % 2 == 0:
            b |= 1
        kb[i] = b
    return base64.b64encode(bytes(kb)).decode()


def http_request_sign(sorted_json: str, ntp_time_ms: int) -> str:
    """sign = Base64(HMAC-SHA256(key=大写hex(HMAC-SHA256(initKey, requestTime)), sortedJson))"""
    k1 = hmac.new(HMAC_INIT_KEY.encode(), str(ntp_time_ms).encode(),
                  hashlib.sha256).hexdigest().upper()
    return base64.b64encode(
        hmac.new(k1.encode(), sorted_json.encode(), hashlib.sha256).digest()).decode()


def sort_json_ascii(obj: dict) -> str:
    """Jackson TreeMap 序列化等价: 键排序 + 紧凑无空格"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_frame(body: bytes, method: str, serial: int, encrypted: bool) -> bytes:
    pkg_type = 1 if method == "appLogin" else 5
    length = len(body) + 12
    head = bytearray(12)
    head[0] = (length >> 16) & 0xFF
    head[1] = (length >> 8) & 0xFF
    head[2] = length & 0xFF
    head[3] = pkg_type
    head[4:7] = HSP_MARK
    head[7] = 1 if encrypted else 0
    struct.pack_into(">I", head, 8, serial)
    return bytes(head) + body


class HxjProtocolError(Exception):
    """协议层错误 (连接/帧/超时)"""


class HxjAuthError(Exception):
    """认证失败 (token 失效等, 需要重新登录)"""


class HxjClient:
    """线程安全的慧享家客户端。每个实例维护一条 TCP 连接。"""

    def __init__(self, account: str = "", password: str = "",
                 android_id: str = None, use_test_server: bool = False):
        self.account = account
        self.password_md5 = md5_upper(password) if password else ""
        self.android_id = android_id or uuid.uuid4().hex[:16]
        self.des_key = gen_des_key()
        self.token_id = None
        self.user_info = None
        if use_test_server:
            self.domain, self.https_url, self.tcp_port = \
                "t.iot.hxjiot.com", "https://t-iot.hxjiot.com:10030", 9393
        else:
            self.domain, self.https_url, self.tcp_port = \
                SERVER_DOMAIN, HTTPS_URL, TCP_PORT
        self._serial = 0
        self._sock = None
        self._lock = threading.RLock()
        self._ssl_ctx = self._build_ssl_ctx()

    # ---------- mTLS ----------
    @staticmethod
    def _build_ssl_ctx() -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        if os.path.exists(CLIENT_CRT) and os.path.exists(CLIENT_KEY):
            ctx.load_cert_chain(CLIENT_CRT, CLIENT_KEY)
        return ctx

    # ---------- 序列号 ----------
    def next_serial(self) -> int:
        """AppTool.getSerial 等价: autoIncrease*16777216 + (分钟*65536+秒*256) + rand(256)"""
        now = time.localtime()
        current = now.tm_min * 65536 + now.tm_sec * 256
        s = (self._serial * 16777216) + current + random.randint(0, 255)
        self._serial += 1
        if self._serial == 128:
            self._serial = 0
        return s

    # ---------- HTTPS ----------
    def _https_post(self, body: dict) -> dict:
        payload = json.dumps(body, ensure_ascii=False,
                             separators=(",", ":")).encode()
        req = urllib.request.Request(
            self.https_url, data=payload, method="POST",
            headers={"Content-Type": "text/json; charset=utf-8",
                     "Content-Version": CONTENT_VERSION})
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=15) as resp:
                raw = resp.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "ignore")
        except Exception as e:
            raise HxjProtocolError(f"HTTPS 请求失败: {e}") from e
        try:
            return json.loads(raw)
        except Exception as e:
            raise HxjProtocolError(f"HTTPS 响应解析失败: {raw[:200]}") from e

    def https_login(self, uuid_val: str = None) -> dict:
        """appLogin (loginType=5, 手机号 + MD5密码)。
        uuid_val: 极验二次验证令牌 — 真实登录必过 GT3 极验, 该字段是服务端令牌"""
        data = {
            "loginType": 5,
            "accountName": self.account,
            "password": self.password_md5,
            "appType": 2,
            "des": rsa_encrypt_b64(self.des_key.encode()),
            "lang": "zh", "locale": "Hans",
            "timeZoneOffSet": 28800, "dstOffSet": 0, "countryCode": 86,
            "phoneType": PHONE_TYPE, "phoneSystem": PHONE_SYSTEM,
            "appVersion": APP_VERSION, "appId": self.android_id,
        }
        if uuid_val:
            data["uuid"] = uuid_val
        body = {"msgId": self.next_serial(), "appCom": APP_COM,
                "method": "appLogin", "data": data}
        ntp = int(time.time() * 1000)
        body.update({"requestTime": ntp, "nonce": str(uuid.uuid4()),
                     "deviceId": self.android_id, "signVersion": 1,
                     "sign": http_request_sign(sort_json_ascii(body), ntp)})
        r = self._https_post(body)
        if r.get("resultCode") == 0:
            d = r.get("data") or {}
            self.token_id = d.get("tokenId")
            self.user_info = d.get("userInfo")
        return r

    # ---------- TCP ----------
    def tcp_connect(self) -> dict:
        """tokenId 登录授权 (AuthParam.createAuthParam 字段全集)"""
        with self._lock:
            self.close()
            try:
                self._sock = socket.create_connection(
                    (self.domain, self.tcp_port), timeout=15)
                self._sock.settimeout(20)
            except Exception as e:
                raise HxjProtocolError(f"TCP 连接失败: {e}") from e
            auth_data = {
                "loginType": 0, "tokenId": self.token_id,
                "appType": 2, "appId": self.android_id,
                "countryCode": 0, "lang": "zh", "locale": "Hans",
                "timeZoneOffSet": 28800, "dstOffSet": 0,
                "timeZoneId": "Asia/Shanghai",
                "phoneType": PHONE_TYPE, "phoneSystem": PHONE_SYSTEM,
                "appVersion": APP_VERSION,
                "des": rsa_encrypt_b64(self.des_key.encode()),
            }
            cmd = {"contentVersion": CONTENT_VERSION, "appCom": APP_COM,
                   "method": "appLogin", "data": auth_data}
            frame = build_frame(json.dumps(cmd, separators=(",", ":")).encode(),
                                "appLogin", self.next_serial(), encrypted=False)
            self._sock.sendall(frame)
            resp = self._recv_frame()
            if resp.get("resultCode") != 0:
                code = resp.get("resultCode")
                self.close()
                if code in (500997, 500004, 401):
                    raise HxjAuthError(
                        f"TCP 授权失败 resultCode={code} (token 失效?)")
                raise HxjProtocolError(f"TCP 授权失败 resultCode={code}")
            return resp

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise HxjProtocolError("连接被服务端关闭")
            buf += chunk
        return buf

    def _recv_frame(self) -> dict:
        head = self._recv_exact(12)
        length = (head[0] << 16) | (head[1] << 8) | head[2]
        body = self._recv_exact(length - 12)
        buf = head + body
        if buf[4:7] != HSP_MARK:
            raise HxjProtocolError("bad HSP mark")
        result_hi = buf[7] >> 4
        enc_flag = buf[7] & 0x0F
        serial = struct.unpack_from(">I", buf, 8)[0]
        raw_body = buf[12:length]
        if enc_flag == 1:
            kb = self._des_key_bytes()
            raw_body = pkcs5_unpad(des_cbc_decrypt(raw_body, kb, kb))
        try:
            j = json.loads(raw_body.decode("utf-8", "ignore"))
        except Exception:
            j = {"_raw": raw_body[:200].hex()}
        j["_result"] = result_hi
        j["_serial"] = serial
        return j

    def des_key_b64(self) -> str:
        return self.des_key

    def _des_key_bytes(self) -> bytes:
        return base64.b64decode(self.des_key)

    def tcp_request(self, method: str, data=None, timeout: int = 15) -> dict:
        """DES 加密业务请求 (自动重连一次)"""
        with self._lock:
            try:
                return self._tcp_request_locked(method, data, timeout)
            except (HxjProtocolError, OSError):
                # 连接可能已被服务端关闭: 重连授权后重试一次
                self.tcp_connect()
                return self._tcp_request_locked(method, data, timeout)

    def _tcp_request_locked(self, method: str, data, timeout: int) -> dict:
        if self._sock is None:
            self.tcp_connect()
        cmd = {"contentVersion": CONTENT_VERSION, "appCom": APP_COM,
               "method": method}
        if data is not None:
            cmd["data"] = data
        plain = json.dumps(cmd, separators=(",", ":")).encode()
        kb = self._des_key_bytes()
        enc = des_cbc_encrypt(pkcs5_pad(plain), kb, kb)
        serial = self.next_serial()
        self._sock.sendall(build_frame(enc, method, serial, encrypted=True))
        self._sock.settimeout(timeout)
        resp = self._recv_frame()
        for _ in range(3):  # 跳过推送帧
            if resp.get("_serial") == serial:
                break
            resp = self._recv_frame()
        return resp

    # ---------- 业务接口 ----------
    def cloud_device_list(self, start_num: int = 0, get_num: int = 20) -> dict:
        return self.tcp_request("appCloudDeviceList",
                                {"startNum": start_num, "getNum": get_num})

    def cloud_open_lock(self, device_id: str, device_type: int = 2) -> dict:
        return self.tcp_request("appCloudOpenLock",
                                {"deviceId": device_id, "deviceType": device_type})

    def cloud_lock_log(self, device_id: str, start_num: int = 0,
                       get_num: int = 20, device_type: int = 2) -> dict:
        return self.tcp_request("appCloudLockLogList",
                                {"startNum": start_num, "getNum": get_num,
                                 "deviceType": device_type, "deviceId": device_id})

    def cloud_lock_media(self, log_id: str) -> dict:
        return self.tcp_request("appCloudLockMediaLogInfo", {"logId": log_id})

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None


def event_title(row: dict) -> str:
    """日志行 -> 中文事件标题"""
    lt, lit = row.get("logType"), row.get("logInfoType")
    if lt == 4:
        title = LOG_INFO_TITLE.get(lit, f"门锁消息(info{lit})")
    else:
        title = LOG_TYPE_TITLE.get(lt, f"门锁消息(type{lt})")
    key = row.get("keyName") or ""
    if key:
        title += f" 钥匙:{key}"
    return title


def event_has_media(log_type: int, log_info_type: int) -> bool:
    return log_type == 3 or (log_type == 4 and log_info_type == 15)


def fmt_time(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except Exception:
        return str(ts)
