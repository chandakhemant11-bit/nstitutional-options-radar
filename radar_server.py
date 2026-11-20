import time
import json
import hashlib
import requests
import pyotp
from flask import Flask, render_template_string, jsonify
from NorenRestApiPy.NorenApi import NorenApi
from dashboard_ui import DASHBOARD_HTML

app = Flask(__name__)

STATE = {
    "status": "Connecting...",
    "spot": 0.0,
    "fut": 0.0,
    "spread": 0.0,
    "ce_iv": 0.0,
    "pe_iv": 0.0,
    "skew": 0.0,
    "signal": "SCANNING",
    "reason": "Waiting for live market ticks...",
    "ticks": 0,
    "last_update": "--:--:--"
}

class ShoonyaApiPy(NorenApi):
    def __init__(self):
        NorenApi.__init__(
            self,
            host='https://api.shoonya.com/NorenWClientTP/',
            websocket='wss://api.shoonya.com/NorenWWS/'
        )

api = ShoonyaApiPy()

def on_tick(tick):
    STATE["ticks"] += 1
    STATE["last_update"] = time.strftime("%H:%M:%S")
    tk = str(tick.get("tk", ""))
    lp = float(tick.get("lp", 0) or 0)
    if lp <= 0:
        return

    if tk == "26000":
        STATE["spot"] = lp
        if STATE["fut"] > 0:
            STATE["spread"] = round(STATE["fut"] - STATE["spot"], 2)

def on_open():
    STATE["status"] = "LIVE CONNECTED"
    print("✅ Shoonya Millisecond WebSocket Connected!")
    api.subscribe(["NSE|26000"])

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)

@app.route("/api/state")
def get_state():
    return jsonify(STATE)

def direct_shoonya_login(user, pwd, totp_secret, vendor_code, api_key):
    totp = pyotp.TOTP(totp_secret).now()
    pwd_hash = hashlib.sha256(pwd.encode('utf-8')).hexdigest()
    app_key = hashlib.sha256(f"{user}|{api_key}".encode('utf-8')).hexdigest()

    payload = {
        "apkversion": "1.0.0",
        "uid": user,
        "pwd": pwd_hash,
        "factor2": totp,
        "vc": vendor_code,
        "appkey": app_key,
        "imei": "abc1234",
        "source": "API"
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    resp = requests.post(
        "https://api.shoonya.com/NorenWClientTP/QuickAuth",
        data=f"jData={json.dumps(payload)}",
        headers=headers,
        timeout=15
    )
    if not resp.text.strip().startswith("{"):
        raise RuntimeError(f"Shoonya Server Blocked/Empty (HTTP {resp.status_code}): {resp.text[:200]}")

    data = resp.json()
    if data.get("stat") != "Ok":
        raise RuntimeError(f"Shoonya Login Rejected: {data}")

    api.set_session(userid=user, password=pwd, usertoken=data["susertoken"])
    return data

def start_radar(user, pwd, totp_secret, vendor_code, api_key):
    direct_shoonya_login(user, pwd, totp_secret, vendor_code, api_key)
    print("✅ Shoonya Login Successful! Starting WebSocket...")
    api.start_websocket(
        order_update_callback=lambda x: None,
        subscribe_callback=on_tick,
        socket_open_callback=on_open
    )
    app.run(host="0.0.0.0", port=5000, use_reloader=False)
