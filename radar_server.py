import time
import threading
import pyotp
from datetime import datetime
from flask import Flask, jsonify, render_template_string
from NorenRestApiPy.NorenApi import NorenApi
from dashboard_ui import DASHBOARD_HTML

app = Flask(__name__)

class ShoonyaApiPy(NorenApi):
    def __init__(self):
        NorenApi.__init__(
            self,
            host='https://api.shoonya.com/NorenWClientTP/',
            websocket='wss://api.shoonya.com/NorenWClientWS/'
        )

api = ShoonyaApiPy()

INDICES = {
    "NSE|26000": "NIFTY 50",
    "NSE|26009": "BANK NIFTY",
    "BSE|1":     "SENSEX"
}

HEAVYWEIGHTS = {
    "NSE|1333":  {"name": "HDFCBANK",   "weight": 0.115},
    "NSE|2885":  {"name": "RELIANCE",   "weight": 0.095},
    "NSE|4963":  {"name": "ICICIBANK",  "weight": 0.080},
    "NSE|1594":  {"name": "INFY",       "weight": 0.060},
    "NSE|11536": {"name": "TCS",        "weight": 0.042},
    "NSE|3045":  {"name": "SBIN",       "weight": 0.032},
    "NSE|11483": {"name": "LT",         "weight": 0.042},
    "NSE|5900":  {"name": "AXISBANK",   "weight": 0.032},
    "NSE|1922":  {"name": "KOTAKBANK",  "weight": 0.028},
    "NSE|1660":  {"name": "ITC",        "weight": 0.040},
    "NSE|10604": {"name": "BHARTIARTL", "weight": 0.038},
    "NSE|317":   {"name": "BAJFINANCE", "weight": 0.025},
}

state_lock = threading.Lock()
tick_store = {}
total_ticks = 0
nifty_spot = 24000.0
alert_state = {"active": False, "title": "", "action": "", "id": "", "expires": 0}
live_note = "Monitoring millisecond order flow across Nifty, BankNifty, Sensex & Top 12 Heavyweights..."

def on_tick(tick):
    global total_ticks, nifty_spot
    exch = tick.get("e")
    tk = tick.get("tk")
    if not exch or not tk:
        return

    ltp = float(tick.get("lp", 0))
    if ltp == 0:
        return

    prev_c = float(tick.get("c", 0))
    vol = int(tick.get("v", 0))
    key = f"{exch}|{tk}"
    now_ms = int(time.time() * 1000)

    with state_lock:
        total_ticks += 1
        if key == "NSE|26000":
            nifty_spot = ltp

        if key not in tick_store:
            tick_store[key] = {
                "ltp": ltp,
                "prev_c": prev_c if prev_c > 0 else ltp,
                "win_ltp": ltp,
                "vol": vol,
                "win_vol": 0,
                "ticks": 1,
                "last_ms": now_ms
            }
        else:
            st = tick_store[key]
            if prev_c > 0:
                st["prev_c"] = prev_c
            st["win_vol"] += max(0, vol - st["vol"])
            st["vol"] = vol
            st["ltp"] = ltp
            st["ticks"] += 1
            st["last_ms"] = now_ms

def evaluate_institutional_flow():
    global alert_state, live_note
    while True:
        time.sleep(5)
        now = time.time()
        drivers = []
        net_pts = 0.0

        with state_lock:
            for key, info in HEAVYWEIGHTS.items():
                if key not in tick_store:
                    continue
                st = tick_store[key]
                imp_pct = ((st["ltp"] - st["win_ltp"]) / st["win_ltp"]) * 100 if st["win_ltp"] else 0
                pts = (imp_pct / 100) * nifty_spot * info["weight"]

                if abs(imp_pct) >= 0.12:
                    drivers.append((abs(pts), info["name"], imp_pct, pts, st["ltp"]))
                    net_pts += pts

                st["win_ltp"] = st["ltp"]
                st["win_vol"] = 0

            # Multi-Confirmation Rule: Kam se kam 2 heavyweights ek saath > 12 Nifty pts push dein
            if len(drivers) >= 2 and abs(net_pts) >= 12.0:
                drivers.sort(reverse=True)
                top = drivers[0]
                direction = "BULLISH BREAKOUT" if net_pts > 0 else "BEARISH DUMP"
                alert_state = {
                    "active": True,
                    "title": f"INSTITUTIONAL {direction} ({net_pts:+.1f} NIFTY PTS)",
                    "action": f"EXIT OPTION SELLING / SCALP {top[1]} ({'BUY' if net_pts > 0 else 'SELL'})",
                    "id": str(int(now)),
                    "expires": now + 45
                }
                names = ", ".join([f"{d[1]} ({d[2]:+.2f}%)" for d in drivers[:3]])
                live_note = (
                    f"<strong>🔥 INSTITUTIONAL IMPULSE DETECTED:</strong> {names} ek saath move kar rahe hain "
                    f"(Net Impact: <strong>{net_pts:+.1f} Nifty pts</strong>). "
                    f"Stock Scalp Focus: <strong>{top[1]} @ {top[4]:.2f}</strong>."
                )
            elif now > alert_state["expires"]:
                alert_state["active"] = False

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)

@app.route("/api/state")
def get_state():
    with state_lock:
        idx_out = {}
        for key, name in INDICES.items():
            if key in tick_store:
                st = tick_store[key]
                pct = ((st["ltp"] - st["prev_c"]) / st["prev_c"]) * 100 if st["prev_c"] else 0
                idx_out[name] = {"ltp": st["ltp"], "pct": pct}
            else:
                idx_out[name] = {"ltp": 0.0, "pct": 0.0}

        stocks_out = []
        for key, info in HEAVYWEIGHTS.items():
            if key in tick_store:
                st = tick_store[key]
                day_pct = ((st["ltp"] - st["prev_c"]) / st["prev_c"]) * 100 if st["prev_c"] else 0
                imp_pct = ((st["ltp"] - st["win_ltp"]) / st["win_ltp"]) * 100 if st["win_ltp"] else 0
                pts = (day_pct / 100) * nifty_spot * info["weight"]
                status = "⚡ ACTIVE" if abs(imp_pct) >= 0.10 else "Calm"
                stocks_out.append({
                    "name": info["name"], "ltp": st["ltp"], "day_pct": day_pct,
                    "imp_pct": imp_pct, "pts": pts, "ticks": st["ticks"], "status": status
                })

        stocks_out.sort(key=lambda x: abs(x["pts"]), reverse=True)

        return jsonify({
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "total_ticks": total_ticks,
            "indices": idx_out,
            "stocks": stocks_out,
            "alert": alert_state,
            "note": live_note
        })

def start_radar(user, pwd, totp_secret, vendor_code, api_key):
    totp = pyotp.TOTP(totp_secret).now()
    ret = api.login(
        userid=user, password=pwd, twoFA=totp,
        vendor_code=vendor_code, api_secret=api_key, imei="abc1234"
    )
    if not ret or ret.get("stat") != "Ok":
        raise RuntimeError(f"Shoonya Login Failed: {ret}")

    print("✅ Shoonya Millisecond WebSocket Connected!")
    api.start_websocket(
        order_update_callback=lambda x: None,
        subscribe_callback=on_tick,
        socket_open_callback=lambda: api.subscribe(list(INDICES.keys()) + list(HEAVYWEIGHTS.keys()))
    )
    threading.Thread(target=evaluate_institutional_flow, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
