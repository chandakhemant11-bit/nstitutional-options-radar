import math
import time
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

LATEST_STATE = {
    "ts": 0,
    "micro": {"mode": "WAITING FOR BRIDGE...", "ticks_per_sec": 0},
    "ticks": {},
    "heavyweights": [],
    "chains": {"NIFTY": [], "BN": [], "SX": []},
    "stock_chains": {}
}

def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

def bs_greeks(S, K, T, r, sigma, optt):
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    pdf = norm_pdf(d1)
    if optt == "CE":
        delta = norm_cdf(d1)
        theta = (-S * pdf * sigma / (2 * math.sqrt(T)) - r * K * math.exp(-r * T) * norm_cdf(d2)) / 365.0
    else:
        delta = norm_cdf(d1) - 1.0
        theta = (-S * pdf * sigma / (2 * math.sqrt(T)) + r * K * math.exp(-r * T) * norm_cdf(-d2)) / 365.0
    gamma = pdf / (S * sigma * math.sqrt(T))
    vega = (S * pdf * math.sqrt(T)) / 100.0
    return {
        "delta": round(delta, 2),
        "gamma": round(gamma, 5),
        "theta": round(theta, 2),
        "vega": round(vega, 2)
    }

def implied_vol(price, S, K, T, r, optt):
    if price <= 0.5 or S <= 0 or K <= 0 or T <= 0:
        return 0.14
    intrinsic = max(0.0, (S - K) if optt == "CE" else (K - S))
    if price <= intrinsic + 0.2:
        return 0.11
    low, high = 0.05, 1.50
    for _ in range(22):
        mid = 0.5 * (low + high)
        d1 = (math.log(S / K) + (r + 0.5 * mid * mid) * T) / (mid * math.sqrt(T))
        d2 = d1 - mid * math.sqrt(T)
        if optt == "CE":
            val = S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
        else:
            val = K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)
        if val > price:
            high = mid
        else:
            low = mid
    return round(0.5 * (low + high), 4)

def enrich_chain(raw_rows, spot, lot_size=75, dte_days=4.0):
    if not raw_rows or spot <= 0:
        return {"rows": [], "net_gex_cr": 0.0, "max_pain": 0, "gamma_flip": 0, "pcr": 1.0, "atm_iv": 13.5, "sweep_bias": 0.0}
    T = max(dte_days, 0.25) / 365.0
    r = 0.065
    by_strike = {}
    tot_ce_oi, tot_pe_oi = 0, 0
    tot_tbq, tot_tsq = 0, 0

    for item in raw_rows:
        k = int(item.get("strike", 0))
        optt = item.get("optt", "CE")
        lp = float(item.get("lp", 0) or 0)
        oi = int(item.get("oi", 0) or 0)
        poi = int(item.get("poi", oi) or 0)
        v = int(item.get("v", 0) or 0)
        tbq = int(item.get("tbq", 0) or 0)
        tsq = int(item.get("tsq", 0) or 0)

        iv = implied_vol(lp, spot, k, T, r, optt)
        gk = bs_greeks(spot, k, T, r, iv, optt)
        doi = oi - poi

        if optt == "CE":
            tot_ce_oi += oi
            tot_tbq += tbq
            tot_tsq += tsq
        else:
            tot_pe_oi += oi
            tot_tbq += tsq
            tot_tsq += tbq

        if k not in by_strike:
            by_strike[k] = {"strike": k}
        by_strike[k][optt] = {
            "lp": lp, "oi": oi, "doi": doi, "v": v,
            "tbq": tbq, "tsq": tsq,
            "iv": round(iv * 100, 1),
            "delta": gk["delta"], "gamma": gk["gamma"],
            "theta": gk["theta"], "vega": gk["vega"]
        }

    strikes = sorted(by_strike.keys())
    net_gex = 0.0
    atm_strike = min(strikes, key=lambda x: abs(x - spot)) if strikes else spot
    atm_iv = 13.5

    rows_out = []
    for k in strikes:
        ce = by_strike[k].get("CE", {"lp": 0, "oi": 0, "doi": 0, "v": 0, "iv": 13.5, "delta": 0, "gamma": 0, "theta": 0, "vega": 0})
        pe = by_strike[k].get("PE", {"lp": 0, "oi": 0, "doi": 0, "v": 0, "iv": 13.5, "delta": 0, "gamma": 0, "theta": 0, "vega": 0})
        ce_gex = ce["gamma"] * ce["oi"] * lot_size * (spot ** 2) * 0.01
        pe_gex = -pe["gamma"] * pe["oi"] * lot_size * (spot ** 2) * 0.01
        net_gex += (ce_gex + pe_gex)

        if k == atm_strike:
            atm_iv = round((ce["iv"] + pe["iv"]) / 2.0, 1)

        ce_act = "SHORT COVERING" if ce["doi"] < -25000 else ("CALL WRITING" if ce["doi"] > 25000 else "NEUTRAL")
        pe_act = "PUT WRITING" if pe["doi"] > 25000 else ("LONG UNWINDING" if pe["doi"] < -25000 else "NEUTRAL")

        rows_out.append({
            "strike": k,
            "is_atm": (k == atm_strike),
            "ce": ce, "pe": pe,
            "ce_act": ce_act, "pe_act": pe_act
        })

    # Max Pain calculation
    max_pain = atm_strike
    min_loss = float("inf")
    for test_k in strikes:
        loss = 0
        for row in rows_out:
            rk = row["strike"]
            if test_k > rk:
                loss += (test_k - rk) * row["ce"]["oi"]
            elif test_k < rk:
                loss += (rk - test_k) * row["pe"]["oi"]
        if loss < min_loss:
            min_loss = loss
            max_pain = test_k

    # Gamma Flip estimate (strike where PE OI wall transitions to CE OI wall)
    gamma_flip = atm_strike
    for row in rows_out:
        if row["ce"]["oi"] > row["pe"]["oi"] and row["strike"] >= spot * 0.985:
            gamma_flip = row["strike"]
            break

    pcr = round(tot_pe_oi / tot_ce_oi, 2) if tot_ce_oi > 0 else 1.0
    sweep_bias = round(((tot_tbq - tot_tsq) / max(tot_tbq + tot_tsq, 1)) * 100, 1)

    return {
        "rows": rows_out,
        "net_gex_cr": round(net_gex / 1e7, 1),
        "max_pain": max_pain,
        "gamma_flip": gamma_flip,
        "pcr": pcr,
        "atm_iv": atm_iv,
        "sweep_bias": sweep_bias
    }

def compute_decision_engine(state):
    ticks = state.get("ticks", {})
    hws = state.get("heavyweights", [])
    n_spot = float((ticks.get("NIFTY_SPOT") or {}).get("lp", 0) or 0)
    n_f1 = float((ticks.get("NIFTY_F1") or {}).get("lp", n_spot) or n_spot)
    n_f2 = float((ticks.get("NIFTY_F2") or {}).get("lp", n_f1) or n_f1)
    sx_spot = float((ticks.get("SX_SPOT") or {}).get("lp", 0) or 0)

    n_chain = enrich_chain(state.get("chains", {}).get("NIFTY", []), n_spot, lot_size=75, dte_days=3.5)
    bn_spot = float((ticks.get("BN_SPOT") or {}).get("lp", 0) or 0)
    bn_chain = enrich_chain(state.get("chains", {}).get("BN", []), bn_spot, lot_size=30, dte_days=3.5)
    sx_chain = enrich_chain(state.get("chains", {}).get("SX", []), sx_spot, lot_size=20, dte_days=3.5)

    # Heavyweight Attribution
    green_cnt = 0
    nifty_pts = 0.0
    sensex_pts = 0.0
    enriched_hws = []
    for h in hws:
        chg = float(h.get("chg_pct", 0) or 0)
        wt_n = float(h.get("wt", 0) or 0)
        wt_sx = float(h.get("wt_sx", wt_n * 1.15) or 0)
        f_doi = int(h.get("fut_doi", 0) or 0)
        if chg > 0:
            green_cnt += 1
        n_imp = round((n_spot * (wt_n / 100.0) * (chg / 100.0)), 1) if n_spot > 0 else 0.0
        sx_imp = round((sx_spot * (wt_sx / 100.0) * (chg / 100.0)), 1) if sx_spot > 0 else 0.0
        nifty_pts += n_imp
        sensex_pts += sx_imp

        if chg >= 0 and f_doi >= 0:
            buildup = "LONG BUILDUP"
        elif chg >= 0 and f_doi < 0:
            buildup = "SHORT COVERING"
        elif chg < 0 and f_doi >= 0:
            buildup = "SHORT BUILDUP"
        else:
            buildup = "LONG UNWINDING"

        stk_lp = float(h.get("lp", 0) or 0)
        stk_iv = float(h.get("atm_iv", 22.5) or 22.5)
        stk_gk = bs_greeks(stk_lp, stk_lp, 12.0 / 365.0, 0.065, stk_iv / 100.0, "CE")
        if chg > 0.8 and f_doi > 0:
            stk_sig = "BUY CALL / BULL SPREAD"
        elif chg < -0.8 and f_doi > 0:
            stk_sig = "BUY PUT / BEAR SPREAD"
        elif abs(chg) < 0.45:
            stk_sig = "SELL STRANGLE (THETA)"
        else:
            stk_sig = "SELL OTM PUT" if chg > 0 else "SELL OTM CE"

        item = dict(h)
        item.update({
            "nifty_pts": n_imp,
            "sensex_pts": sx_imp,
            "buildup": buildup,
            "atm_iv": stk_iv,
            "atm_delta": stk_gk["delta"],
            "atm_gamma": stk_gk["gamma"],
            "stk_signal": stk_sig
        })
        enriched_hws.append(item)

    enriched_hws.sort(key=lambda x: abs(x["nifty_pts"]), reverse=True)
    top_movers = [f"{x['sym']} ({'+' if x['nifty_pts']>=0 else ''}{x['nifty_pts']}p)" for x in enriched_hws[:3]]

    # Regime Judge: Option Buying vs Option Selling
    gex = n_chain["net_gex_cr"]
    pcr = n_chain["pcr"]
    iv = n_chain["atm_iv"]
    sweep = n_chain["sweep_bias"]
    total_hws = max(len(enriched_hws), 1)
    breadth_ratio = green_cnt / total_hws

    if gex < -150 and (breadth_ratio >= 0.70 or breadth_ratio <= 0.30) and iv >= 14.5:
        regime = "OPTION BUYING REGIME (GAMMA EXPANSION)"
        regime_color = "#ff9f1c"
        if breadth_ratio >= 0.70 and pcr >= 1.05:
            action = "STRONG BUY CE — GAMMA BREAKOUT"
            sub = f"Negative GEX ({gex} Cr) + {green_cnt}/{total_hws} Heavyweights Long + Order Sweep {sweep}% | Avoid Shorting Calls"
            banner_bg = "linear-gradient(90deg, #064e3b, #047857)"
            border_c = "#10b981"
        elif breadth_ratio <= 0.30 and pcr <= 0.90:
            action = "STRONG BUY PE — GAMMA CRASH VELOCITY"
            sub = f"Negative GEX ({gex} Cr) + Heavyweight Selloff ({green_cnt}/{total_hws} Green) | Momentum Put Buying Active"
            banner_bg = "linear-gradient(90deg, #7f1d1d, #b91c1c)"
            border_c = "#ef4444"
        else:
            action = "EXIT IMMEDIATELY ON REVERSAL / SCALP ONLY"
            sub = "High Gamma Volatility Detected — Tighten Trailing SL to Cost"
            banner_bg = "linear-gradient(90deg, #78350f, #b45309)"
            border_c = "#f59e0b"
    else:
        regime = "OPTION SELLING REGIME (POSITIVE GEX THETA HARVEST)"
        regime_color = "#38bdf8"
        if breadth_ratio >= 0.65 and pcr >= 1.15:
            action = "SELL OTM PE / BULL PUT CREDIT SPREAD"
            sub = f"Positive GEX (+{gex} Cr) Dealer Support + Put Writing Wall (PCR {pcr}) | High Probability Theta Decay"
            banner_bg = "linear-gradient(90deg, #065f46, #0f766e)"
            border_c = "#34d399"
        elif breadth_ratio <= 0.35 and pcr <= 0.85:
            action = "SELL OTM CE / BEAR CALL CREDIT SPREAD"
            sub = f"Heavy Call Writing Resistance (PCR {pcr}) + Weak Heavyweight Breadth ({green_cnt}/{total_hws} Green)"
            banner_bg = "linear-gradient(90deg, #881337, #9f1239)"
            border_c = "#fb7185"
        else:
            action = "SELL IRON CONDOR / STRANGLE (RANGE PINNED) — AVOID NAKED BUYING"
            sub = f"Price Pinned Near Max Pain {n_chain['max_pain']} | Positive GEX (+{gex} Cr) Absorbing Moves"
            banner_bg = "linear-gradient(90deg, #1e3a8a, #1d4ed8)"
            border_c = "#60a5fa"

    # Exit Alert Check
    exit_alert = None
    if sweep < -18 and breadth_ratio < 0.45:
        exit_alert = "EXIT CE IMMEDIATELY — Institutional Ask Sweep & Heavyweight Reversal Detected!"
    elif sweep > 18 and breadth_ratio > 0.55:
        exit_alert = "EXIT PE IMMEDIATELY — Aggressive Bid Sweep & Heavyweight Short Covering!"

    # Positional 3-15 Day Assessment
    basis_f1 = round(n_f1 - n_spot, 1)
    roll_spread = round(n_f2 - n_f1, 1)
    if iv < 13.0 and breadth_ratio >= 0.60:
        pos_verdict = "POSITIONAL DEBIT CALL SPREAD (LOW IV ENTRY)"
        pos_reason = f"ATM IV {iv}% is cheap (<13%) + F1-F2 Rollover Spread +{roll_spread} pts shows institutional long carry."
    elif iv >= 14.5:
        pos_verdict = "POSITIONAL CREDIT SPREAD / IRON CONDOR (HIGH IV THETA SELL)"
        pos_reason = f"Elevated IV ({iv}%) offers rich 3–15 day theta decay; anchor short strikes outside Max Pain {n_chain['max_pain']}."
    else:
        pos_verdict = "POSITIONAL BULL PUT / RATIO SPREAD"
        pos_reason = f"F1 Basis +{basis_f1} pts | Net Nifty Impact {'+' if nifty_pts>=0 else ''}{round(nifty_pts,1)} pts | Sensex {'+' if sensex_pts>=0 else ''}{round(sensex_pts,1)} pts."

    # Enrich Stock Option Chains
    raw_stk_chains = state.get("stock_chains", {})
    enriched_stk_chains = {}
    hw_map = {x["sym"]: x for x in enriched_hws}
    for sym, srows in raw_stk_chains.items():
        sp = float((hw_map.get(sym) or {}).get("lp", 0) or 0)
        enriched_stk_chains[sym] = enrich_chain(srows, sp, lot_size=500, dte_days=12.0)

    return {
        "micro": state.get("micro", {}),
        "ticks": ticks,
        "decision": {
            "regime": regime,
            "regime_color": regime_color,
            "action": action,
            "sub": sub,
            "banner_bg": banner_bg,
            "border_c": border_c,
            "exit_alert": exit_alert,
            "green_cnt": green_cnt,
            "total_hws": total_hws,
            "nifty_pts": round(nifty_pts, 1),
            "sensex_pts": round(sensex_pts, 1),
            "top_movers": top_movers,
            "pos_verdict": pos_verdict,
            "pos_reason": pos_reason,
            "basis_f1": basis_f1,
            "roll_spread": roll_spread
        },
        "chains": {
            "NIFTY": n_chain,
            "BN": bn_chain,
            "SX": sx_chain
        },
        "heavyweights": enriched_hws,
        "stock_chains": enriched_stk_chains
    }

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>GammaShield v2.0 — Institutional Options, Futures & 30-Stock Terminal</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Segoe UI', Roboto, Arial, sans-serif; }
  body { background: #070b14; color: #e2e8f0; padding: 12px 18px; font-size: 13px; }
  .topbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; border-bottom: 1px solid #1e293b; padding-bottom: 8px; }
  .brand { font-size: 18px; font-weight: 700; color: #f8fafc; letter-spacing: 0.4px; }
  .brand span { color: #38bdf8; }
  .status-pill { background: #064e3b; color: #34d399; border: 1px solid #10b981; padding: 4px 10px; border-radius: 4px; font-weight: 700; font-size: 11px; }
  .tabs { display: flex; gap: 8px; margin-bottom: 10px; }
  .tab-btn { background: #0f172a; color: #94a3b8; border: 1px solid #1e293b; padding: 7px 14px; border-radius: 5px; cursor: pointer; font-weight: 600; font-size: 12px; }
  .tab-btn.active { background: #0284c7; color: #ffffff; border-color: #38bdf8; }
  .regime-strip { display: flex; justify-content: space-between; align-items: center; background: #0f172a; border: 1px solid #1e293b; padding: 6px 12px; border-radius: 5px; margin-bottom: 8px; font-size: 12px; }
  .action-banner { padding: 11px 15px; border-radius: 6px; margin-bottom: 10px; border-left: 5px solid #10b981; }
  .action-title { font-size: 16px; font-weight: 800; color: #ffffff; letter-spacing: 0.3px; }
  .action-sub { font-size: 12px; color: #e2e8f0; margin-top: 3px; }
  .exit-flash { background: #991b1b; border: 1px solid #f87171; color: #ffffff; padding: 8px 12px; border-radius: 5px; font-weight: 800; margin-bottom: 10px; text-align: center; animation: pulse 1.2s infinite; }
  @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.65; } 100% { opacity: 1; } }
  .idx-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 10px; }
  .card { background: #0d1424; border: 1px solid #1e293b; border-radius: 6px; padding: 10px 12px; }
  .idx-title { font-size: 11px; color: #94a3b8; font-weight: 700; }
  .idx-spot { font-size: 22px; font-weight: 800; color: #38bdf8; margin: 2px 0 6px 0; }
  .fut-row { display: flex; justify-content: space-between; background: #131c31; padding: 5px 8px; border-radius: 4px; margin-top: 4px; font-size: 11.5px; }
  .section-hdr { display: flex; justify-content: space-between; align-items: center; margin: 10px 0 6px 0; font-weight: 700; color: #cbd5e1; font-size: 12.5px; }
  .hw-strip { display: grid; grid-template-columns: repeat(10, 1fr); gap: 6px; margin-bottom: 10px; }
  .hw-box { background: #0f172a; border: 1px solid #1e293b; border-radius: 5px; padding: 6px; cursor: pointer; transition: 0.15s; }
  .hw-box:hover { border-color: #38bdf8; background: #16223b; }
  .green { color: #34d399; } .red { color: #f87171; } .amber { color: #fbbf24; } .cyan { color: #38bdf8; }
  .metrics-bar { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 8px; }
  .metric-box { background: #0f172a; border: 1px solid #1e293b; padding: 7px 10px; border-radius: 5px; }
  .metric-lbl { font-size: 10.5px; color: #94a3b8; }
  .metric-val { font-size: 13.5px; font-weight: 700; color: #f8fafc; margin-top: 2px; }
  .idx-switch button { background: #1e293b; color: #cbd5e1; border: none; padding: 5px 12px; border-radius: 4px; cursor: pointer; font-weight: 700; margin-left: 4px; }
  .idx-switch button.active { background: #f59e0b; color: #000; }
  .chain-wrap { max-height: 460px; overflow-y: auto; border: 1px solid #1e293b; border-radius: 6px; }
  table { width: 100%; border-collapse: collapse; font-size: 11.5px; }
  th { background: #111827; color: #94a3b8; padding: 7px 6px; position: sticky; top: 0; z-index: 2; border-bottom: 1px solid #334155; text-align: center; }
  td { padding: 5px 6px; border-bottom: 1px solid #172033; text-align: center; }
  tr.atm-row { background: #172554 !important; font-weight: 700; }
  tr:hover { background: #131d33; }
  .badge { padding: 2px 6px; border-radius: 3px; font-size: 10px; font-weight: 700; }
  .b-buy { background: #064e3b; color: #34d399; }
  .b-sell { background: #7f1d1d; color: #fca5a5; }
  .b-neu { background: #1e293b; color: #94a3b8; }
  .hidden { display: none; }
</style>
</head>
<body>

<div class="topbar">
  <div class="brand">&#9724; GammaShield <span>v2.0</span> — Institutional Options, Futures &amp; 30-Stock Terminal</div>
  <div>
    <span id="tpsBadge" style="color:#94a3b8; margin-right:10px; font-size:11.5px;">0 ticks/s</span>
    <span id="modeBadge" class="status-pill">CONNECTING...</span>
  </div>
</div>

<div class="tabs">
  <button class="tab-btn active" onclick="switchTab(1, this)">1. Intraday Pro-Desk Execution &amp; Gamma Shield</button>
  <button class="tab-btn" onclick="switchTab(2, this)">2. Positional Swing &amp; Term-Structure (3–15 Days)</button>
  <button class="tab-btn" onclick="switchTab(3, this)">3. All 30 Stocks Attribution, Futures &amp; Stock Options</button>
</div>

<div class="regime-strip">
  <div><strong>REGIME JUDGE:</strong> <span id="regimeText" class="amber">ANALYZING...</span></div>
  <div><strong>INDEX DRIVERS:</strong> <span id="topMoversText" class="cyan">Loading...</span></div>
  <div><strong>NET IMPACT:</strong> Nifty <span id="nImpact">0</span> pts | Sensex <span id="sxImpact">0</span> pts</div>
</div>

<div id="exitBox" class="exit-flash hidden"></div>

<div id="actionBanner" class="action-banner" style="background: linear-gradient(90deg, #064e3b, #047857);">
  <div class="action-title" id="actionTitle">INITIALIZING INSTITUTIONAL DECISION ENGINE...</div>
  <div class="action-sub" id="actionSub">Waiting for tick stream...</div>
</div>

<!-- TAB 1: INTRADAY EXECUTION & FULL 25-STRIKE OPTION CHAIN -->
<div id="tab1">
  <div class="idx-grid" id="idxCards"></div>

  <div class="section-hdr">
    <div>Top 10 Heavyweight Movers (Click Any Stock for Live Option Chain &amp; Greeks)</div>
    <div id="breadthSummary" class="green">Breadth: --</div>
  </div>
  <div class="hw-strip" id="top10Strip"></div>

  <div class="section-hdr">
    <div>Microstructure Greeks, Net GEX, Gamma Flip &amp; Full ATM ±12 Option Chain (25 Strikes)</div>
    <div class="idx-switch">
      <button class="active" onclick="setActiveIdx('NIFTY', this)">NIFTY (25)</button>
      <button onclick="setActiveIdx('BN', this)">BANKNIFTY (25)</button>
      <button onclick="setActiveIdx('SX', this)">SENSEX (25)</button>
    </div>
  </div>

  <div class="metrics-bar" id="chainMetrics"></div>

  <div class="chain-wrap">
    <table>
      <thead>
        <tr>
          <th>CALL OI (ΔOI)</th><th>ACTION</th><th>CE IV%</th><th>CE Δ / Γ</th><th>CE Θ</th><th>CALL LTP</th>
          <th style="color:#fbbf24;">STRIKE</th>
          <th>PUT LTP</th><th>PE Θ</th><th>PE Δ / Γ</th><th>PE IV%</th><th>ACTION</th><th>PUT OI (ΔOI)</th>
        </tr>
      </thead>
      <tbody id="chainBody"></tbody>
    </table>
  </div>
</div>

<!-- TAB 2: POSITIONAL SWING & TERM-STRUCTURE SHIELD (3-15 DAYS) -->
<div id="tab2" class="hidden">
  <div class="card" style="margin-bottom:10px; border-left:4px solid #38bdf8;">
    <div style="font-size:14px; font-weight:800; color:#38bdf8;" id="posVerdict">POSITIONAL REGIME VERDICT</div>
    <div style="margin-top:4px; color:#cbd5e1;" id="posReason">Analyzing F1–F2 Rollover Spread &amp; Implied Volatility Percentile...</div>
  </div>
  <div class="idx-grid" id="posGrid"></div>
</div>

<!-- TAB 3: ALL 30 STOCKS ATTRIBUTION, FUTURES & INTERACTIVE STOCK OPTION CHAIN -->
<div id="tab3" class="hidden">
  <div class="section-hdr">
    <div>All 30 Sensex + Nifty Heavyweights — Index Point Attribution, Futures Buildup &amp; Stock Greeks (Click Any Row for Stock Option Chain)</div>
    <div class="cyan">Click a Stock Row Below &#8595;</div>
  </div>
  <div class="chain-wrap" style="max-height:320px; margin-bottom:12px;">
    <table>
      <thead>
        <tr>
          <th>STOCK</th><th>LTP</th><th>CHG %</th><th>NIFTY PTS</th><th>SENSEX PTS</th>
          <th>FUTURES BUILDUP</th><th>ATM IV%</th><th>ATM Δ / Γ</th><th>INSTITUTIONAL OPTION SIGNAL</th>
        </tr>
      </thead>
      <tbody id="all30Body"></tbody>
    </table>
  </div>

  <div class="card" id="stockChainPanel">
    <div class="section-hdr" style="margin-top:0;">
      <div id="stkChainTitle" style="color:#fbbf24; font-size:14px;">Select Any Stock Above to Inspect Its Live Option Chain &amp; Greeks</div>
      <div id="stkChainMeta" class="cyan"></div>
    </div>
    <div class="chain-wrap" style="max-height:280px;">
      <table>
        <thead>
          <tr>
            <th>CALL OI</th><th>CE IV%</th><th>CE Δ / Γ</th><th>CE Θ</th><th>CALL LTP</th>
            <th style="color:#fbbf24;">STRIKE</th>
            <th>PUT LTP</th><th>PE Θ</th><th>PE Δ / Γ</th><th>PE IV%</th><th>PUT OI</th>
          </tr>
        </thead>
        <tbody id="stkChainBody"></tbody>
      </table>
    </div>
  </div>
</div>

<script>
let STATE = null;
let ACTIVE_IDX = 'NIFTY';
let SELECTED_STOCK = 'HDFCBANK';

function switchTab(num, btn) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  ['tab1', 'tab2', 'tab3'].forEach((id, i) => {
    document.getElementById(id).classList.toggle('hidden', (i + 1) !== num);
  });
}

function setActiveIdx(idx, btn) {
  ACTIVE_IDX = idx;
  document.querySelectorAll('.idx-switch button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  if (STATE) renderChain();
}

function selectStock(sym) {
  SELECTED_STOCK = sym;
  switchTab(3, document.querySelectorAll('.tab-btn')[2]);
  if (STATE) renderStockChain();
}

function fmtNum(n) {
  return Number(n || 0).toLocaleString('en-IN');
}

function renderChain() {
  const ch = (STATE.chains || {})[ACTIVE_IDX];
  if (!ch) return;
  document.getElementById('chainMetrics').innerHTML = `
    <div class="metric-box"><div class="metric-lbl">NET GEX &amp; GAMMA FLIP</div><div class="metric-val cyan">${ch.net_gex_cr} Cr | Flip: ${fmtNum(ch.gamma_flip)}</div></div>
    <div class="metric-box"><div class="metric-lbl">ATM IV &amp; ORDER SWEEP</div><div class="metric-val amber">IV: ${ch.atm_iv}% | Sweep: ${ch.sweep_bias}%</div></div>
    <div class="metric-box"><div class="metric-lbl">MAX PAIN &amp; PCR</div><div class="metric-val green">Max Pain: ${fmtNum(ch.max_pain)} | PCR: ${ch.pcr}</div></div>
    <div class="metric-box"><div class="metric-lbl">STRIKES TRACKED</div><div class="metric-val">ATM ±12 (${ch.rows.length} Strikes)</div></div>
  `;

  let html = '';
  ch.rows.forEach(r => {
    const ceCls = r.ce_act === 'SHORT COVERING' ? 'b-buy' : (r.ce_act === 'CALL WRITING' ? 'b-sell' : 'b-neu');
    const peCls = r.pe_act === 'PUT WRITING' ? 'b-buy' : (r.pe_act === 'LONG UNWINDING' ? 'b-sell' : 'b-neu');
    html += `<tr class="${r.is_atm ? 'atm-row' : ''}">
      <td>${fmtNum(r.ce.oi)} <span class="${r.ce.doi>=0?'green':'red'}">(${r.ce.doi>=0?'+':''}${fmtNum(r.ce.doi)})</span></td>
      <td><span class="badge ${ceCls}">${r.ce_act}</span></td>
      <td>${r.ce.iv}%</td>
      <td>${r.ce.delta} / ${r.ce.gamma}</td>
      <td class="red">${r.ce.theta}</td>
      <td class="cyan"><strong>${r.ce.lp.toFixed(2)}</strong></td>
      <td style="color:#fbbf24; font-weight:800;">${r.strike}</td>
      <td class="cyan"><strong>${r.pe.lp.toFixed(2)}</strong></td>
      <td class="red">${r.pe.theta}</td>
      <td>${r.pe.delta} / ${r.pe.gamma}</td>
      <td>${r.pe.iv}%</td>
      <td><span class="badge ${peCls}">${r.pe_act}</span></td>
      <td>${fmtNum(r.pe.oi)} <span class="${r.pe.doi>=0?'green':'red'}">(${r.pe.doi>=0?'+':''}${fmtNum(r.pe.doi)})</span></td>
    </tr>`;
  });
  document.getElementById('chainBody').innerHTML = html;
}

function renderStockChain() {
  const sc = (STATE.stock_chains || {})[SELECTED_STOCK];
  const hw = (STATE.heavyweights || []).find(x => x.sym === SELECTED_STOCK);
  document.getElementById('stkChainTitle').innerText = `${SELECTED_STOCK} — Live Stock Option Chain & Greeks (Positional & Intraday)`;
  if (hw) {
    document.getElementById('stkChainMeta').innerText = `Spot: ₹${hw.lp} (${hw.chg_pct>=0?'+':''}${hw.chg_pct}%) | ${hw.buildup} | ${hw.stk_signal}`;
  }
  if (!sc || !sc.rows || !sc.rows.length) {
    document.getElementById('stkChainBody').innerHTML = `<tr><td colspan="11">Loading ${SELECTED_STOCK} option strikes...</td></tr>`;
    return;
  }
  let html = '';
  sc.rows.forEach(r => {
    html += `<tr class="${r.is_atm ? 'atm-row' : ''}">
      <td>${fmtNum(r.ce.oi)}</td>
      <td>${r.ce.iv}%</td>
      <td>${r.ce.delta} / ${r.ce.gamma}</td>
      <td class="red">${r.ce.theta}</td>
      <td class="cyan"><strong>${r.ce.lp.toFixed(2)}</strong></td>
      <td style="color:#fbbf24; font-weight:800;">${r.strike}</td>
      <td class="cyan"><strong>${r.pe.lp.toFixed(2)}</strong></td>
      <td class="red">${r.pe.theta}</td>
      <td>${r.pe.delta} / ${r.pe.gamma}</td>
      <td>${r.pe.iv}%</td>
      <td>${fmtNum(r.pe.oi)}</td>
    </tr>`;
  });
  document.getElementById('stkChainBody').innerHTML = html;
}

function renderAll(data) {
  STATE = data;
  const d = data.decision;
  const t = data.ticks || {};

  document.getElementById('modeBadge').innerText = data.micro.mode || 'LIVE';
  document.getElementById('tpsBadge').innerText = `${data.micro.ticks_per_sec || 0} ticks/s`;
  document.getElementById('regimeText').innerText = d.regime;
  document.getElementById('regimeText').style.color = d.regime_color;
  document.getElementById('topMoversText').innerText = (d.top_movers || []).join('  |  ');
  document.getElementById('nImpact').innerText = (d.nifty_pts >= 0 ? '+' : '') + d.nifty_pts;
  document.getElementById('sxImpact').innerText = (d.sensex_pts >= 0 ? '+' : '') + d.sensex_pts;

  const banner = document.getElementById('actionBanner');
  banner.style.background = d.banner_bg;
  banner.style.borderLeftColor = d.border_c;
  document.getElementById('actionTitle').innerText = d.action;
  document.getElementById('actionSub').innerText = d.sub;

  const ex = document.getElementById('exitBox');
  if (d.exit_alert) {
    ex.innerText = d.exit_alert;
    ex.classList.remove('hidden');
  } else {
    ex.classList.add('hidden');
  }

  // Index Cards
  const idxDefs = [
    {title: 'NIFTY 50 SPOT', sp: 'NIFTY_SPOT', f1: 'NIFTY_F1', f2: 'NIFTY_F2'},
    {title: 'BANKNIFTY SPOT', sp: 'BN_SPOT', f1: 'BN_F1', f2: 'BN_F2'},
    {title: 'SENSEX SPOT', sp: 'SX_SPOT', f1: 'SX_F1', f2: 'SX_F2'}
  ];
  let cardsHtml = '';
  idxDefs.forEach(x => {
    const sp = (t[x.sp] || {}).lp || 0;
    const f1 = t[x.f1] || {};
    const f2 = t[x.f2] || {};
    const b1 = (f1.lp && sp) ? (f1.lp - sp).toFixed(1) : '0.0';
    const b2 = (f2.lp && sp) ? (f2.lp - sp).toFixed(1) : '0.0';
    cardsHtml += `<div class="card">
      <div class="idx-title">${x.title}</div>
      <div class="idx-spot">${Number(sp).toLocaleString('en-IN', {minimumFractionDigits:2})}</div>
      <div class="fut-row"><span>${f1.tsym || 'NEAR FUT'} (OI ${fmtNum(f1.oi)})</span><span class="green">${f1.lp || 0} (Basis +${b1})</span></div>
      <div class="fut-row"><span>${f2.tsym || 'NEXT FUT'} (OI ${fmtNum(f2.oi)})</span><span class="cyan">${f2.lp || 0} (Basis +${b2})</span></div>
    </div>`;
  });
  document.getElementById('idxCards').innerHTML = cardsHtml;

  // Top 10 Strip
  const hws = data.heavyweights || [];
  document.getElementById('breadthSummary').innerText = `Breadth: ${d.green_cnt}/${d.total_hws} Green | Nifty ${d.nifty_pts>=0?'+':''}${d.nifty_pts} pts | Sensex ${d.sensex_pts>=0?'+':''}${d.sensex_pts} pts`;
  let stripHtml = '';
  hws.slice(0, 10).forEach(h => {
    const c = h.chg_pct >= 0 ? 'green' : 'red';
    stripHtml += `<div class="hw-box" onclick="selectStock('${h.sym}')">
      <div style="font-weight:800; font-size:11px;">${h.sym} <span class="${c}">${h.chg_pct>=0?'+':''}${h.chg_pct}%</span></div>
      <div style="font-size:10.5px; color:#94a3b8; margin-top:2px;">₹${h.lp} (${h.nifty_pts>=0?'+':''}${h.nifty_pts}p)</div>
      <div style="font-size:9.5px; margin-top:2px;" class="cyan">${h.buildup}</div>
    </div>`;
  });
  document.getElementById('top10Strip').innerHTML = stripHtml;

  // Tab 2 Positional
  document.getElementById('posVerdict').innerText = d.pos_verdict;
  document.getElementById('posReason').innerText = d.pos_reason;
  document.getElementById('posGrid').innerHTML = `
    <div class="card"><div class="idx-title">NIFTY POSITIONAL (3–15D)</div><div class="metric-val cyan">Max Pain: ${data.chains.NIFTY.max_pain} | PCR: ${data.chains.NIFTY.pcr}</div><div style="margin-top:6px;">ATM IV: ${data.chains.NIFTY.atm_iv}% | F1–F2 Roll Spread: +${d.roll_spread} pts</div></div>
    <div class="card"><div class="idx-title">BANKNIFTY POSITIONAL (3–15D)</div><div class="metric-val amber">Max Pain: ${data.chains.BN.max_pain} | PCR: ${data.chains.BN.pcr}</div><div style="margin-top:6px;">ATM IV: ${data.chains.BN.atm_iv}% | Net GEX: ${data.chains.BN.net_gex_cr} Cr</div></div>
    <div class="card"><div class="idx-title">SENSEX POSITIONAL (3–15D)</div><div class="metric-val green">Max Pain: ${data.chains.SX.max_pain} | PCR: ${data.chains.SX.pcr}</div><div style="margin-top:6px;">ATM IV: ${data.chains.SX.atm_iv}% | Net GEX: ${data.chains.SX.net_gex_cr} Cr</div></div>
  `;

  // Tab 3 All 30 Stocks Table
  let t3Html = '';
  hws.forEach(h => {
    const c = h.chg_pct >= 0 ? 'green' : 'red';
    t3Html += `<tr style="cursor:pointer;" onclick="selectStock('${h.sym}')">
      <td style="font-weight:800; color:#f8fafc;">${h.sym}</td>
      <td>₹${h.lp}</td>
      <td class="${c}"><strong>${h.chg_pct>=0?'+':''}${h.chg_pct}%</strong></td>
      <td class="${h.nifty_pts>=0?'green':'red'}">${h.nifty_pts>=0?'+':''}${h.nifty_pts}</td>
      <td class="${h.sensex_pts>=0?'green':'red'}">${h.sensex_pts>=0?'+':''}${h.sensex_pts}</td>
      <td class="cyan">${h.buildup}</td>
      <td>${h.atm_iv}%</td>
      <td>${h.atm_delta} / ${h.atm_gamma}</td>
      <td><span class="badge ${h.chg_pct>=0?'b-buy':'b-sell'}">${h.stk_signal}</span></td>
    </tr>`;
  });
  document.getElementById('all30Body').innerHTML = t3Html;

  renderChain();
  renderStockChain();
}

async function poll() {
  try {
    const res = await fetch('/api/state');
    const data = await res.json();
    if (data && data.decision) renderAll(data);
  } catch (e) {}
  setTimeout(poll, 700);
}
poll();
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_PAGE)

@app.route("/api/batch", methods=["POST"])
def receive_batch():
    payload = request.get_json(silent=True) or {}
    LATEST_STATE["ts"] = time.time()
    LATEST_STATE["micro"] = payload.get("micro", LATEST_STATE["micro"])
    LATEST_STATE["ticks"] = payload.get("ticks", LATEST_STATE["ticks"])
    LATEST_STATE["heavyweights"] = payload.get("heavyweights", LATEST_STATE["heavyweights"])
    LATEST_STATE["chains"] = payload.get("chains", LATEST_STATE["chains"])
    LATEST_STATE["stock_chains"] = payload.get("stock_chains", LATEST_STATE["stock_chains"])
    return jsonify({"ok": True})

@app.route("/api/state")
def get_state():
    return jsonify(compute_decision_engine(LATEST_STATE))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
