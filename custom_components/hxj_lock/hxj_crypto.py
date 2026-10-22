#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
慧享家协议所需加密原语 — 纯 Python 标准库实现 (无第三方依赖, 适配 Home Assistant 环境)

与 App 行为的等价性已用真实库交叉验证:
  - DES/CBC/PKCS5   == javax.crypto "DES/CBC/PKCS5Padding" (与 pycryptodome 逐字节一致)
  - RSA PKCS#1 v1.5 == Java "RSA/ECB/PKCS1Padding"      (与 cryptography 加解密互通)
"""
import base64
import os

# ============================================================
# 单 DES (FIPS 46-3) — 表驱动实现
# ============================================================

_IP = [
    58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4,
    62, 54, 46, 38, 30, 22, 14, 6, 64, 56, 48, 40, 32, 24, 16, 8,
    57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
    61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7,
]
_FP = [
    40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31,
    38, 6, 46, 14, 54, 22, 62, 30, 37, 5, 45, 13, 53, 21, 61, 29,
    36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
    34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25,
]
_E = [
    32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9,
    8, 9, 10, 11, 12, 13, 12, 13, 14, 15, 16, 17,
    16, 17, 18, 19, 20, 21, 20, 21, 22, 23, 24, 25,
    24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1,
]
_P = [
    16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10,
    2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4, 25,
]
_PC1 = [
    57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18,
    10, 2, 59, 51, 43, 35, 27, 19, 11, 3, 60, 52, 44, 36,
    63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22,
    14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 28, 20, 12, 4,
]
_PC2 = [
    14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10,
    23, 19, 12, 4, 26, 8, 16, 7, 27, 20, 13, 2,
    41, 52, 31, 37, 47, 55, 30, 40, 51, 45, 33, 48,
    44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32,
]
_SHIFTS = [1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1]
_SBOX = [
    [14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7,
     0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
     4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0,
     15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13],
    [15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10,
     3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
     0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15,
     13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9],
    [10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8,
     13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
     13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7,
     1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12],
    [7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15,
     13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
     10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4,
     3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14],
    [2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9,
     14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
     4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14,
     11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3],
    [12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11,
     10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
     9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6,
     4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13],
    [4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1,
     13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
     1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2,
     6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12],
    [13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7,
     1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
     7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8,
     2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11],
]


def _permute(value: int, table, in_bits: int) -> int:
    """按表置换: table 为 1-based 位号 (MSB=1)"""
    out = 0
    for pos in table:
        out = (out << 1) | ((value >> (in_bits - pos)) & 1)
    return out


def _des_subkeys(key8: bytes):
    """8 字节密钥 -> 16 个 48 位子密钥"""
    k = int.from_bytes(key8, "big")
    cd = _permute(k, _PC1, 64)          # 56 bit
    c, d = cd >> 28, cd & 0x0FFFFFFF
    subs = []
    for sh in _SHIFTS:
        c = ((c << sh) | (c >> (28 - sh))) & 0x0FFFFFFF
        d = ((d << sh) | (d >> (28 - sh))) & 0x0FFFFFFF
        subs.append(_permute((c << 28) | d, _PC2, 56))
    return subs


def _des_crypt_block(block8: bytes, subkeys, encrypt: bool) -> bytes:
    x = _permute(int.from_bytes(block8, "big"), _IP, 64)
    l, r = x >> 32, x & 0xFFFFFFFF
    for i in range(16):
        k = subkeys[i] if encrypt else subkeys[15 - i]
        # f(R, K)
        er = _permute(r, _E, 32) ^ k
        s_out = 0
        for b in range(8):
            six = (er >> (42 - 6 * b)) & 0x3F
            row = ((six >> 4) & 0b10) | (six & 1)   # bit1,bit6
            col = (six >> 1) & 0xF
            s_out = (s_out << 4) | _SBOX[b][row * 16 + col]
        f = _permute(s_out, _P, 32)
        l, r = r, l ^ f
    pre = (r << 32) | l    # 注意: 最后一轮不交换 => R16 L16
    return _permute(pre, _FP, 64).to_bytes(8, "big")


def des_cbc_encrypt(plaintext: bytes, key8: bytes, iv8: bytes) -> bytes:
    """DES/CBC 加密 (调用方负责 PKCS5 padding)"""
    subs = _des_subkeys(key8)
    out = bytearray()
    prev = iv8
    for i in range(0, len(plaintext), 8):
        blk = bytes(a ^ b for a, b in zip(plaintext[i:i + 8], prev))
        enc = _des_crypt_block(blk, subs, True)
        out += enc
        prev = enc
    return bytes(out)


def des_cbc_decrypt(ciphertext: bytes, key8: bytes, iv8: bytes) -> bytes:
    subs = _des_subkeys(key8)
    out = bytearray()
    prev = iv8
    for i in range(0, len(ciphertext), 8):
        blk = ciphertext[i:i + 8]
        dec = _des_crypt_block(blk, subs, False)
        out += bytes(a ^ b for a, b in zip(dec, prev))
        prev = blk
    return bytes(out)


def pkcs5_pad(data: bytes, block_size: int = 8) -> bytes:
    n = block_size - (len(data) % block_size)
    return data + bytes([n]) * n


def pkcs5_unpad(data: bytes, block_size: int = 8) -> bytes:
    if not data or len(data) % block_size:
        raise ValueError("PKCS5: 数据长度非法")
    n = data[-1]
    if n < 1 or n > block_size or data[-n:] != bytes([n]) * n:
        raise ValueError("PKCS5: padding 非法")
    return data[:-n]


# ============================================================
# RSA — SubjectPublicKeyInfo DER 解析 + PKCS#1 v1.5 (type 2) 加密
# ============================================================

def _read_tlv(der: bytes, i: int):
    """读 (tag, content_start, content_end)"""
    tag = der[i]
    j = i + 1
    ln = der[j]
    j += 1
    if ln & 0x80:
        nbytes = ln & 0x7F
        ln = int.from_bytes(der[j:j + nbytes], "big")
        j += nbytes
    return tag, j, j + ln


def rsa_pubkey_from_spki_der(der: bytes):
    """解析 X.509 SubjectPublicKeyInfo (RSA) -> (n, e)"""
    _, s1, e1 = _read_tlv(der, 0)                 # 外层 SEQUENCE
    _, s2, e2 = _read_tlv(der, s1)                # AlgorithmIdentifier SEQUENCE
    _, s3, e3 = _read_tlv(der, e2)                # BIT STRING
    if der[s3] != 0:                              # unused bits 必须为 0
        raise ValueError("RSA SPKI: BIT STRING unused bits != 0")
    _, s4, e4 = _read_tlv(der, s3 + 1)            # 内层 SEQUENCE {n, e}
    _, s5, e5 = _read_tlv(der, s4)                # INTEGER n
    n = int.from_bytes(der[s5:e5], "big")
    _, s6, e6 = _read_tlv(der, e5)                # INTEGER e
    e = int.from_bytes(der[s6:e6], "big")
    return n, e


def rsa_encrypt_pkcs1_v15(spki_der_b64: str, message: bytes) -> bytes:
    """RSA/ECB/PKCS1Padding (Java) 等价: PKCS#1 v1.5 type 2 加密"""
    n, e = rsa_pubkey_from_spki_der(base64.b64decode(spki_der_b64))
    k = (n.bit_length() + 7) // 8
    if len(message) > k - 11:
        raise ValueError("RSA: 明文过长")
    ps_len = k - 3 - len(message)
    ps = bytearray()
    while len(ps) < ps_len:
        b = os.urandom(1)
        if b != b"\x00":
            ps += b
    em = b"\x00\x02" + bytes(ps) + b"\x00" + message
    m = int.from_bytes(em, "big")
    c = pow(m, e, n)
    return c.to_bytes(k, "big")


# ============================================================
# 自测: 有 pycryptodome / cryptography 时交叉验证 (仅开发用)
# ============================================================

if __name__ == "__main__":
    import sys
    sys.path.insert(0, r"C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Lib/site-packages")

    key = os.urandom(8)
    iv = key  # App: key=IV
    msg = os.urandom(37)

    # --- DES 交叉验证 ---
    try:
        from Crypto.Cipher import DES as _DES
        ref = _DES.new(key, _DES.MODE_CBC, iv).encrypt(pkcs5_pad(msg))
        mine = des_cbc_encrypt(pkcs5_pad(msg), key, iv)
        assert ref == mine, "DES 加密不一致!"
        back = pkcs5_unpad(des_cbc_decrypt(ref, key, iv))
        assert back == msg, "DES 解密不一致!"
        print("DES/CBC/PKCS5: 与 pycryptodome 逐字节一致 ✓")
    except ImportError:
        # 无参考库: 至少验证往返一致
        ct = des_cbc_encrypt(pkcs5_pad(msg), key, iv)
        assert pkcs5_unpad(des_cbc_decrypt(ct, key, iv)) == msg
        print("DES: 无参考库, 仅往返一致性验证 ✓")

    # --- RSA 交叉验证 ---
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        from cryptography.hazmat.primitives.serialization import Encoding
        priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        spki = priv.public_key().public_bytes(
            Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        spki_b64 = base64.b64encode(spki).decode()
        ct = rsa_encrypt_pkcs1_v15(spki_b64, b"hello-hxj-1234")
        pt = priv.decrypt(ct, padding.PKCS1v15())
        assert pt == b"hello-hxj-1234", "RSA 解密结果不符!"
        print("RSA PKCS#1 v1.5: 与 cryptography 互通 ✓")
    except ImportError:
        print("RSA: 无 cryptography, 跳过交叉验证")

    # --- 真实服务器公钥解析 ---
    RSA_PUBKEY = ("MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDIagxJDNQ3fWb1h8MMQgvtqKzm0"
                  "WGW8JHYRn0T8GOm7OXfrxTE/aTaWRw9+rUsfrfCRsrzH7wE/A9jP/+koijdS2a/OuQ"
                  "ab/aFQamxOhQTi7u/okeCnWqrZdc9FpNgBKv4oDu+WVoftPlUrDVoAhh0RdF3/4OGr"
                  "SMdW8BxDaGEcwIDAQAB")
    n, e = rsa_pubkey_from_spki_der(base64.b64decode(RSA_PUBKEY))
    print(f"服务器 RSA 公钥解析: n={n.bit_length()}bit e={e} ✓")
