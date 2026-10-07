# ============================================================
#  ██████╗ ██████╗ ██╗███╗   ███╗███████╗    ███████╗███████╗
#  ██╔══██╗██╔══██╗██║████╗ ████║██╔════╝    ██╔════╝██╔════╝
#  ██████╔╝██████╔╝██║██╔████╔██║█████╗      █████╗  █████╗
#  ██╔═══╝ ██╔══██╗██║██║╚██╔╝██║██╔══╝      ██╔══╝  ██╔══╝
#  ██║     ██║  ██║██║██║ ╚═╝ ██║███████╗    ██║     ██║
#  ╚═╝     ╚═╝  ╚═╝╚═╝╚═╝     ╚═╝╚══════╝    ╚═╝     ╚═╝
#
#  PRIME FF — FREE FIRE BD SERVER INFO API
#  VERCEL READY | NO EXTERNAL API
#  JOIN @primeff55 FOR MORE
# ============================================================

import asyncio
import time
import httpx
import json
import threading
import os
import sys
import base64
from collections import defaultdict
from functools import wraps
from flask import Flask, request, jsonify
from flask_cors import CORS
from cachetools import TTLCache
from typing import Tuple, Optional
from proto import FreeFire_pb2, main_pb2, AccountPersonalShow_pb2
from google.protobuf import json_format, message
from google.protobuf.message import Message
from Crypto.Cipher import AES
import logging

# ---------- PRIME FF Logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PRIME-FF")

# ---------- Config ----------
MAIN_KEY = base64.b64decode('WWcmdGMlREV1aDYlWmNeOA==')
MAIN_IV = base64.b64decode('Nm95WkRyMjJFM3ljaGpNJQ==')
RELEASEVERSION = "OB55"
USERAGENT = "Dalvik/2.1.0 (Linux; U; Android 13; CPH2095 Build/RKQ1.211119.001)"
LOGIN_USERAGENT = "UnityPlayer/2018.4.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)"

# ---------- BD ONLY ----------
BD_REGION = "BD"
BD_CREDS = "uid=4423054565&password=CKR_PRO_BOT_WF9AMKXDI"
BD_SERVER_URL_FALLBACK = "https://clientbp.ppmainecoonghj.com"
LOGIN_URL = "https://loginbp.ppmainecoonghj.com/MajorLogin"

# ---------- App Setup ----------
app = Flask(__name__)
CORS(app)
cache = TTLCache(maxsize=200, ttl=600)
cached_tokens = defaultdict(dict)

# ---------- Shared HTTP clients ----------
HTTP_LIMITS = httpx.Limits(max_keepalive_connections=20, max_connections=50)
HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

_http_client: Optional[httpx.Client] = None
def http_sync() -> httpx.Client:
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(limits=HTTP_LIMITS, timeout=HTTP_TIMEOUT)
    return _http_client

_http_async: Optional[httpx.AsyncClient] = None
def http() -> httpx.AsyncClient:
    global _http_async
    if _http_async is None:
        _http_async = httpx.AsyncClient(limits=HTTP_LIMITS, timeout=HTTP_TIMEOUT)
    return _http_async

# ---------- Helpers ----------
def pad(text: bytes) -> bytes:
    padding_length = AES.block_size - (len(text) % AES.block_size)
    return text + bytes([padding_length] * padding_length)

def aes_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    return AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext))

def decode_protobuf(encoded_data: bytes, message_type: message.Message):
    instance = message_type()
    try:
        instance.ParseFromString(encoded_data)
        return instance
    except Exception as e:
        logger.error(f"Protobuf decode error: {e}")
        return None

async def json_to_proto(json_data: str, proto_message: Message) -> bytes:
    json_format.ParseDict(json.loads(json_data), proto_message)
    return proto_message.SerializeToString()

def json_to_proto_sync(json_data: str, proto_message: Message) -> bytes:
    json_format.ParseDict(json.loads(json_data), proto_message)
    return proto_message.SerializeToString()

# ---------- LoginRes scan parser (jwt.py trick) ----------
def _try_parse_login_res(data: bytes):
    try:
        msg = FreeFire_pb2.LoginRes()
        msg.ParseFromString(data)
        if msg.account_id and msg.account_id > 0:
            return json.loads(json_format.MessageToJson(msg))
    except Exception:
        pass
    return None

def extract_login_res(raw: bytes) -> dict:
    """Scan technique — handles prefixed / truncated responses."""
    parsed = _try_parse_login_res(raw)
    if parsed:
        return parsed

    idx = 0
    while True:
        idx = raw.find(b"\x08", idx)
        if idx == -1:
            break
        parsed = _try_parse_login_res(raw[idx:])
        if parsed:
            return parsed
        idx += 1

    jwt_marker = raw.find(b"eyJhbGciOiJIUzI1NiIs")
    if jwt_marker != -1:
        for i in range(jwt_marker - 1, max(jwt_marker - 300, -1), -1):
            if raw[i] == 0x42:
                parsed = _try_parse_login_res(raw[i:])
                if parsed:
                    return parsed
                break

    raise Exception(f"PRIME FF: Could not parse LoginRes. Raw: {raw[:200]}")

# ---------- OAuth guest token ----------
def get_access_token(account: str):
    url = "https://ffmconnect.live.gop.garenanow.com/oauth/guest/token/grant"
    payload = (
        account
        + "&response_type=token&client_type=2"
        + "&client_secret=2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3"
        + "&client_id=100067"
    )
    headers = {
        "User-Agent": LOGIN_USERAGENT,
        "Connection": "Keep-Alive",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        resp = http_sync().post(url, data=payload, headers=headers)
        data = resp.json()
        return data.get("access_token", "0"), data.get("open_id", "0")
    except Exception as e:
        logger.error(f"PRIME FF OAuth error: {e}")
        return "0", "0"

# ---------- MajorLogin → JWT ----------
def generate_jwt_token(uid: str, password: str):
    start_time = time.time()

    token_val, open_id = get_access_token(f"uid={uid}&password={password}")
    if token_val == "0" or open_id == "0":
        raise Exception("PRIME FF: Invalid UID/Password — access token not received")

    body = json.dumps({
        "open_id": open_id,
        "open_id_type": "4",
        "login_token": token_val,
        "orign_platform_type": "4",
    })
    proto_bytes = json_to_proto_sync(body, FreeFire_pb2.LoginReq())
    payload = aes_cbc_encrypt(MAIN_KEY, MAIN_IV, proto_bytes)

    headers = {
        "User-Agent": LOGIN_USERAGENT,
        "Accept": "*/*",
        "Accept-Encoding": "deflate, gzip",
        "X-Ga-Sv": "1789534056",
        "Authorization": "Bearer",
        "X-Ga": "v1 1",
        "Releaseversion": RELEASEVERSION,
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Unity-Version": "2018.4.12f1",
        "PlAy_VeR": "1.132.1",
        "Ob_VeR": RELEASEVERSION,
    }

    resp = http_sync().post(LOGIN_URL, data=payload, headers=headers)

    if resp.status_code != 200:
        raise Exception(f"MajorLogin HTTP {resp.status_code}: {resp.text[:80]}")
    if "text/" in resp.headers.get("content-type", ""):
        raise Exception(f"MajorLogin rejected: {resp.text[:80]}")

    msg = extract_login_res(resp.content)
    elapsed = time.time() - start_time

    return {
        "real_uid": str(msg.get("accountId", "")),
        "time": f"{elapsed:.2f}s",
        "token": msg.get("token", ""),
        "lock_region": msg.get("lockRegion", BD_REGION),
        "server_url": msg.get("serverUrl", BD_SERVER_URL_FALLBACK),
    }

# ---------- BD token cache ----------
def _bd_token_cached() -> Optional[dict]:
    info = cached_tokens.get(BD_REGION)
    if info and time.time() < info.get("expires_at", 0):
        return info
    return None

def _refresh_bd_token():
    parts = dict(p.split("=", 1) for p in BD_CREDS.split("&"))
    uid = parts.get("uid", "")
    password = parts.get("password", "")

    if not uid or not password:
        raise RuntimeError("PRIME FF: BD creds missing")

    td = generate_jwt_token(uid, password)
    if not td.get("token"):
        raise RuntimeError("PRIME FF: no token in LoginRes")

    info = {
        "token": f"Bearer {td['token']}",
        "region": td.get("lock_region") or BD_REGION,
        "server_url": td.get("server_url") or BD_SERVER_URL_FALLBACK,
        "expires_at": time.time() + 25200,
    }
    cached_tokens[BD_REGION] = info
    logger.info(f"[BD] Token OK real_uid={td['real_uid']} ({td['time']})")
    return info

def get_token_info_sync() -> Tuple[str, str, str]:
    info = _bd_token_cached()
    if not info:
        info = _refresh_bd_token()
    return info["token"], info["region"], info["server_url"]

async def get_token_info_async() -> Tuple[str, str, str]:
    info = _bd_token_cached()
    if info:
        return info["token"], info["region"], info["server_url"]

    # Run sync blocking token refresh in executor
    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(None, _refresh_bd_token)
    return info["token"], info["region"], info["server_url"]

# ---------- Get Account Information (BD only) ----------
async def GetAccountInformation(uid, unk):
    try:
        payload = await json_to_proto(
            json.dumps({'a': uid, 'b': unk}),
            main_pb2.GetPlayerPersonalShow()
        )
        data_enc = aes_cbc_encrypt(MAIN_KEY, MAIN_IV, payload)

        token, lock, server = await get_token_info_async()

        headers = {
            'User-Agent': USERAGENT,
            'Connection': "Keep-Alive",
            'Accept-Encoding': "gzip",
            'Content-Type': "application/octet-stream",
            'Expect': "100-continue",
            'Authorization': token,
            'X-Unity-Version': "2018.4.11f1",
            'X-GA': "v1 1",
            'ReleaseVersion': RELEASEVERSION,
        }

        r = await http().post(server + "/GetPlayerPersonalShow",
                              data=data_enc, headers=headers)

        if r.status_code != 200:
            logger.error(f"[BD] account info failed {r.status_code}")
            return None
        if "text/" in r.headers.get("content-type", ""):
            return None

        decoded = decode_protobuf(r.content,
                                  AccountPersonalShow_pb2.AccountPersonalShowInfo)
        if not decoded:
            return None

        return json.loads(json_format.MessageToJson(decoded))
    except Exception as e:
        logger.error(f"[BD] GetAccountInformation: {e}")
        return None

# ---------- Cache decorator ----------
def cached_endpoint(ttl=300):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*a, **k):
            key = (request.path, tuple(request.args.items()))
            if key in cache:
                return cache[key]
            res = fn(*a, **k)
            cache[key] = res
            return res
        return wrapper
    return decorator

# ============================================================
#  PRIME FF Routes
# ============================================================

@app.route('/info', methods=['GET'])
@cached_endpoint()
def get_account_info():
    """PRIME FF: /info?uid=123456789 (BD server)"""
    uid = request.args.get('uid')
    if not uid:
        return jsonify({
            "error": "PRIME FF: Please provide UID. Usage: /info?uid=123456789"
        }), 400

    try:
        data = asyncio.run(GetAccountInformation(uid, "7"))
    except RuntimeError:
        # If event loop already running (rare in Flask threaded mode)
        loop = asyncio.new_event_loop()
        try:
            data = loop.run_until_complete(GetAccountInformation(uid, "7"))
        finally:
            loop.close()

    if not data:
        return jsonify({
            "error": "PRIME FF: UID not found on BD server or account doesn't exist"
        }), 404

    return jsonify(data), 200

@app.route('/health', methods=['GET'])
def health_check():
    info = cached_tokens.get(BD_REGION) or {}
    return jsonify({
        "status": "active",
        "brand": "PRIME FF",
        "region": BD_REGION,
        "has_token": bool(info),
        "cache_size": len(cache),
    }), 200

@app.route('/refresh-token', methods=['GET', 'POST'])
def refresh_token_endpoint():
    try:
        _refresh_bd_token()
        return jsonify({
            "message": "PRIME FF: BD token refreshed successfully",
            "region": BD_REGION,
        }), 200
    except Exception as e:
        return jsonify({"error": f"PRIME FF: {e}"}), 500

@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "name": "PRIME FF — FreeFire BD Server Info API",
        "version": "4.0",
        "brand": "PRIME FF",
        "region": BD_REGION,
        "endpoints": {
            "/info": "Get account info — /info?uid=123456789",
            "/health": "Health check",
            "/refresh-token": "Force refresh BD token",
        },
        "status": "running",
    }), 200

# ---------- Error Handlers ----------
@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "PRIME FF: endpoint not found"}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "PRIME FF: internal server error"}), 500

# ============================================================
#  Startup — works on Vercel + local
# ============================================================

_started = False

def _warmup():
    """Best-effort warm-up. Safe on serverless (per cold start)."""
    try:
        _refresh_bd_token()
    except Exception as e:
        logger.warning(f"PRIME FF warmup: {e}")

def _bootstrap():
    global _started
    if _started:
        return
    _started = True
    # Warm-up in a daemon thread — never blocks module import
    threading.Thread(target=_warmup, daemon=True).start()

# Local dev entry point
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    threading.Thread(target=_warmup, daemon=True).start()
    logger.info(f"PRIME FF starting on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
else:
    # Vercel / gunicorn imports the module — fire warmup once
    _bootstrap()