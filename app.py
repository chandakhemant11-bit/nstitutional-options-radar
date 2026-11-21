import os, time, math
from collections import deque
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

LIVE_STATE = {"last_update": "--:--:--", "micro": {}, "ticks": {}, "heavyweights": [], "chains": {}, "analytics": {}}
ROLLING_HISTORY = {"NIFTY": deque(maxlen=900), "BN": deque(maxlen=900), "SX": deque(maxlen=900)}

def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

def bs_greeks(S, K, T, r, sigma, optt):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0, 0.0, 0.0, 0.0
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    gamma = norm_pdf(d1) / (S * sigma * sqrtT)
    if optt == "CE":
        price = S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
        delta = norm_cdf(d1)
        theta = (-(S * norm_pdf(d1) * sigma) / (2 * sqrtT) - r * K * math.exp(-r * T) * norm_cdf(d2)) / 365.0
    else:
        price = K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)
        delta = norm_cdf(d1) - 1.0
        theta = (-(S * norm_pdf(d1) * sigma) / (2 * sqrtT) + r * K * math.exp(-r * T) * norm_cdf(-d2)) / 365.0
    return price, delta, gamma, theta

def solve_greeks(S, K, mkt_price, optt, T=7/365.0, r=0.07):
    intrinsic = max(0.0, S - K) if optt == "CE" else max(0.0, K - S)
    if mkt_price <= intrinsic + 0.5:
        d = (1.0 if S > K else 0.0) if optt == "CE" else (-1.0 if S < K else 0.0)
        return 12.0, round(d, 2), 0.0001, -1.5
    low, high = 0.03, 2.5
    for _ in range(22):
        mid = 0.5 * (low + high)
        p, _, _, _ = bs_greeks(S, K, T, r, mid, optt)
        if p < mkt_price:
            low = mid
        else:
            high = mid
    iv = 0.5 * (low + high)
    _, delta, gamma, theta = bs_greeks(S, K, T, r, iv, optt)
    return round(iv * 100, 1), round(delta, 2), round(gamma, 5), round(theta, 2)

def analyze_heavyweights(hws, nifty_sp, bn_sp):
    enriched = []
    nifty_pts = 0.0
    bn_pts = 0.0
    adv = 0
    for h in hws:
        chg = float(h.get("chg_pct", 0) or 0)
        wt = float(h.get("wt", 0) or 0)
        doi = int(h.get("fut_doi", 0) or 0)
        if chg > 0: adv += 1
        n_contrib = round((nifty_sp * (wt / 100.0) * (chg / 100.0)), 1) if nifty_sp else 0.0
        nifty_pts += n_contrib
        if h.get("bn"):
            bn_wt = wt * 3.1
            bn_pts += round((bn_sp * (bn_wt / 100.0) * (chg / 100.0)), 1) if bn_sp else 0.0
        if chg > 0.15 and doi > 0:
            buildup = "LONG BUILDUP"
        elif chg > 0.15 and doi < 0:
            buildup = "SHORT COVERING"
        elif chg < -0.15 and doi > 0:
            buildup = "SHORT BUILDUP"
        elif chg < -0.15 and doi < 0:
            buildup = "LONG UNWIND"
        else:
            buildup = "NEUTRAL"
        enriched.append({
            "sym": h.get("sym"), "lp": h.get("lp", 0), "chg_pct": chg,
            "n_pts": n_contrib, "fut_doi": doi, "buildup": buildup
        })
    return {
        "stocks": enriched,
        "nifty_pts": round(nifty_pts, 1),
        "bn_pts": round(bn_pts, 1),
        "breadth": f"{adv}/{len(hws)} Green"
    }

def compute_max_pain(strikes_dict):
    all_k = sorted(strikes_dict.keys())
    if not all_k:
        return 0
    min_pain = float("inf")
    mp_strike = all_k[len(all_k)//2]
    for exp_p in all_k:
        pain = 0
        for k in all_k:
            ce_oi = (strikes_dict[k].get("CE") or {}).get("oi", 0)
            pe_oi = (strikes_dict[k].get("PE") or {}).get("oi", 0)
            if exp_p > k:
                pain += (exp_p - k) * ce_oi
            elif exp_p < k:
                pain += (k - exp_p) * pe_oi
        if pain < min_pain:
            min_pain = pain
            mp_strike = exp_p
    return mp_strike

def compute_master_analytics(ticks, chains, micro, hws):
    out = {}
    now_ts = time.time()
    tps = int((micro or {}).get("ticks_per_sec", 0))
    nifty_sp = float((ticks.get("NIFTY_SPOT") or {}).get("lp", 0) or 0)
    bn_sp = float((ticks.get("BN_SPOT") or {}).get("lp", 0) or 0)
    hw_summary = analyze_heavyweights(hws, nifty_sp, bn_sp)

    for tag, step in [("NIFTY", 50), ("BN", 100), ("SX", 100)]:
        sp = float((ticks.get(f"{tag}_SPOT") or {}).get("lp", 0) or 0)
        f1 = ticks.get(f"{tag}_F1") or {}
        f2 = ticks.get(f"{tag}_F2") or {}
        chain = chains.get(tag, [])
        if not sp or not chain:
            continue

        atm = int(round(sp / step) * step)
        strikes = {}
        tot_ce_oi, tot_pe_oi = 0, 0
        broad_ce_oi, broad_pe_oi = 0, 0
        max_ce_oi, res_strike = -1, atm
        max_pe_oi, sup_strike = -1, atm
        broad_max_ce, broad_res = -1, atm
        broad_max_pe, broad_sup = -1, atm
        net_gex = 0.0
        ce_tbq, ce_tsq, pe_tbq, pe_tsq = 0, 0, 0, 0
        alerts = []
        iv_map = {}
        otm_pe_ivs, otm_ce_ivs = [], []
        gex_by_strike = {}

        for row in chain:
            st = int(row["strike"])
            optt = row["optt"]
            lp = float(row.get("lp", 0) or 0)
            oi = int(row.get("oi", 0) or 0)
            poi = int(row.get("poi", oi) or oi)
            doi = oi - poi
            iv, delta, gamma, theta = solve_greeks(sp, st, lp, optt)
            row["iv"] = iv
            row["delta"] = delta
            row["gamma"] = gamma
            row["theta"] = theta
            row["doi"] = doi
            iv_map[f"{st}_{optt}"] = iv

            strike_gex = (gamma * oi * sp * sp * 0.01) / 1e7
            signed_gex = strike_gex if optt == "CE" else -strike_gex
            net_gex += signed_gex
            gex_by_strike[st] = gex_by_strike.get(st, 0.0) + signed_gex

            if doi > 0 and lp > 0:
                quad = "LONG BUILD" if (optt == "CE" and sp >= st) or (optt == "PE" and sp <= st) else "SHORT BUILD"
            elif doi < 0:
                quad = "SHORT COVER"
            else:
                quad = "NEUTRAL"
            row["quad"] = quad

            strikes.setdefault(st, {"strike": st, "CE": {}, "PE": {}})
            strikes[st][optt] = row

            dist_steps = abs(st - atm) // step
            if optt == "CE":
                broad_ce_oi += oi
                if oi > broad_max_ce: broad_max_ce, broad_res = oi, st
                if st > atm and dist_steps <= 6: otm_ce_ivs.append(iv)
            else:
                broad_pe_oi += oi
                if oi > broad_max_pe: broad_max_pe, broad_sup = oi, st
                if st < atm and dist_steps <= 6: otm_pe_ivs.append(iv)

            if dist_steps <= 5:
                if optt == "CE":
                    tot_ce_oi += oi
                    ce_tbq += int(row.get("tbq", 0))
                    ce_tsq += int(row.get("tsq", 0))
                    if oi > max_ce_oi: max_ce_oi, res_strike = oi, st
                else:
                    tot_pe_oi += oi
                    pe_tbq += int(row.get("tbq", 0))
                    pe_tsq += int(row.get("tsq", 0))
                    if oi > max_pe_oi: max_pe_oi, sup_strike = oi, st
            else:
                is_itm = (optt == "CE" and st < atm) or (optt == "PE" and st > atm)
                zone = "DEEP ITM" if is_itm else "FAR OTM"
                if row["v"] > 2500000 or iv > 32.0:
                    alerts.append(f"{zone} {st} {optt} (Vol: {row['v']:,} | IV: {iv}% | Δ: {delta})")

        max_pain = compute_max_pain(strikes)
        gamma_flip = min(gex_by_strike.keys(), key=lambda k: abs(gex_by_strike[k])) if gex_by_strike else atm

        hist = ROLLING_HISTORY[tag]
        hist.append({"ts": now_ts, "atm_iv": iv_map.get(f"{atm}_CE", 14.0), "sp": sp})
        past_3m = next((x for x in hist if now_ts - x["ts"] >= 150), hist[0])
        iv_vel_3m = round(iv_map.get(f"{atm}_CE", 14.0) - past_3m["atm_iv"], 2)

        ce_imb = round(ce_tbq / max(1, ce_tsq), 2)
        pe_imb = round(pe_tbq / max(1, pe_tsq), 2)
        ob_signal = f"CE {ce_imb}x | PE {pe_imb}x"

        net_gex_cr = round(net_gex, 1)
        gex_regime = "POSITIVE GEX (Pinning)" if net_gex_cr >= 0 else "NEGATIVE GEX (Squeeze Risk)"

        atm_row = strikes.get(atm, {})
        atm_ce_obj = atm_row.get("CE") or {}
        atm_pe_obj = atm_row.get("PE") or {}
        atm_ce = atm_ce_obj.get("lp", 0)
        atm_pe = atm_pe_obj.get("lp", 0)
        atm_iv = round((atm_ce_obj.get("iv", 14.0) + atm_pe_obj.get("iv", 14.0)) / 2.0, 1)
        straddle_prem = round(atm_ce + atm_pe, 2)
        synth_fut = round(atm + atm_ce - atm_pe, 2) if (atm_ce and atm_pe) else sp
        synth_diff = round(synth_fut - sp, 2)
        pcr = round(tot_pe_oi / tot_ce_oi, 2) if tot_ce_oi > 0 else 0
        broad_pcr = round(broad_pe_oi / broad_ce_oi, 2) if broad_ce_oi > 0 else 0

        hw_pts = hw_summary["bn_pts"] if tag == "BN" else hw_summary["nifty_pts"]
        is_spike = (tps > 120 or abs(iv_vel_3m) >= 1.2 or ce_imb > 2.2 or pe_imb > 2.2 or net_gex_cr < -150)

        if is_spike:
            spike_shield = f"🚨 GAMMA SHIELD ACTIVE! (TPS: {tps}/s | 3m ΔIV: {iv_vel_3m:+.2f}% | {ob_signal}) — EXIT / HEDGE NAKED SHORTS!"
            shield_level = "DANGER"
            if (ce_imb > 1.8 or hw_pts > 35 or synth_diff > 5):
                pro_action = "🔥 OPTION BUYING / CALL BREAKOUT (Momentum CE Scalp — Avoid Shorting Calls)"
                action_cls = "buy"
            elif (pe_imb > 1.8 or hw_pts < -35 or synth_diff < -5):
                pro_action = "🔥 OPTION BUYING / PUT BREAKDOWN (Momentum PE Scalp — Avoid Shorting Puts)"
                action_cls = "buy"
            else:
                pro_action = "⚠️ VOLATILITY SPIKE — WAIT FOR IV COOL-OFF (No Naked Selling)"
                action_cls = "warn"
        else:
            spike_shield = f"🛡️ GAMMA SHIELD GREEN — Positive GEX ({net_gex_cr} Cr) & Stable IV ({iv_vel_3m:+.2f}%)"
            shield_level = "SAFE"
            if hw_pts > 40 and pcr >= 1.05:
                pro_action = "🟢 BULLISH CREDIT SPREAD / SELL OTM PUTS (Heavyweights + Put Writing Support)"
                action_cls = "sell"
            elif hw_pts < -40 and pcr <= 0.85:
                pro_action = "🟢 BEARISH CREDIT SPREAD / SELL OTM CALLS (Heavyweight Drag + Call Writing)"
                action_cls = "sell"
            else:
                pro_action = f"🟢 OPTION SELLING ZONE — SHORT STRANGLE / IRON CONDOR (Range {sup_strike}–{res_strike})"
                action_cls = "sell"

        f1_lp = float(f1.get("lp", 0) or 0)
        f2_lp = float(f2.get("lp", 0) or 0)
        cal_spread = round(f2_lp - f1_lp, 2) if (f1_lp and f2_lp) else 0
        ann_carry = round((cal_spread / f1_lp) * 12 * 100, 2) if f1_lp else 0
        avg_pe_iv = sum(otm_pe_ivs)/len(otm_pe_ivs) if otm_pe_ivs else atm_iv
        avg_ce_iv = sum(otm_ce_ivs)/len(otm_ce_ivs) if otm_ce_ivs else atm_iv
        iv_skew = round(avg_pe_iv - avg_ce_iv, 2)
        wing_iv = round((avg_pe_iv + avg_ce_iv) / 2.0, 1)
        term_structure = "CONTANGO (Normal Carry — Safe Positional Selling)" if (cal_spread > 0 and atm_iv <= wing_iv + 2.5) else "BACKWARDATION / EVENT SPIKE RISK (Hedge Positional Shorts!)"
        swing_10d_move = round(sp * (atm_iv / 100.0) * math.sqrt(10 / 365.0), 0)

        if "BACKWARDATION" in term_structure or iv_skew > 2.8:
            pos_action = "⚠️ POSITIONAL DEFENSIVE: Buy Protective Wings / Reduce Naked Short Lots"
        elif broad_pcr >= 1.15 and ann_carry >= 5.5:
            pos_action = "🟢 POSITIONAL BULLISH: Sell OTM Put Spreads (3–15 Days Carry)"
        elif broad_pcr <= 0.82:
            pos_action = "🟢 POSITIONAL BEARISH: Sell OTM Call Spreads (3–15 Days Carry)"
        else:
            pos_action = f"🟢 POSITIONAL IRON CONDOR: Short {broad_sup} PE & {broad_res} CE (Theta Harvest)"

        all_strikes = sorted(strikes.keys())
        rows_12 = [strikes[k] for k in all_strikes]
        rows_5 = [strikes[k] for k in all_strikes if abs(k - atm) <= 5 * step]
        peak_oi_12 = max([max((strikes[k].get("CE") or {}).get("oi", 0), (strikes[k].get("PE") or {}).get("oi", 0)) for k in all_strikes] + [1])
        peak_oi_5 = max([max((strikes[k].get("CE") or {}).get("oi", 0), (strikes[k].get("PE") or {}).get("oi", 0)) for k in all_strikes if abs(k - atm) <= 5 * step] + [1])

        out[tag] = {
            "atm": atm, "synth_fut": synth_fut, "synth_diff": synth_diff,
            "pcr": pcr, "res_strike": res_strike, "max_ce_oi": max_ce_oi,
            "sup_strike": sup_strike, "max_pe_oi": max_pe_oi,
            "max_pain": max_pain, "gamma_flip": gamma_flip,
            "straddle": straddle_prem, "net_gex": net_gex_cr, "gex_regime": gex_regime,
            "tps": tps, "iv_vel_3m": iv_vel_3m, "ob_signal": ob_signal,
            "spike_shield": spike_shield, "shield_level": shield_level,
            "pro_action": pro_action, "action_cls": action_cls,
            "peak_oi_5": peak_oi_5, "peak_oi_12": peak_oi_12,
            "alerts": alerts[:3], "rows_5": rows_5, "rows_12": rows_12,
            "positional": {
                "cal_spread": cal_spread, "ann_carry": ann_carry, "atm_iv": atm_iv,
                "iv_skew": iv_skew, "broad_pcr": broad_pcr, "term_structure": term_structure,
                "broad_sup": broad_sup, "broad_res": broad_res, "max_pain": max_pain,
                "swing_move": int(swing_10d_move),
                "swing_low": int(sp - swing_10d_move), "swing_high": int(sp + swing_10d_move),
                "pos_action": pos_action
            }
        }
    out["heavyweights"] = hw_summary
    return out

HTML_DASHBOARD = """<!DOCTYPE html>
<html>
<head>
<title>GammaShield — Institutional Options & Volatility Terminal</title>
<style>
  body { font-family: 'Segoe UI', sans-serif; background: #090d16; color: #f1f5f9; margin: 0; padding: 18px; }
  .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #1e293b; padding-bottom: 12px; }
  .tabs { display: flex; gap: 10px; margin: 14px 0; }
  .tab-btn { background: #1e293b; color: #94a3b8; border: none; padding: 10px 20px; border-radius: 8px; cursor: pointer; font-weight: bold; font-size: 13px; }
  .tab-btn.active { background: #0284c7; color: #fff; }
  .shield-banner { padding: 10px 15px; border-radius: 8px; margin-bottom: 10px; font-weight: bold; font-size: 13px; border: 1px solid #065f46; background: #064e3b; color: #6ee7b7; }
  .shield-banner.danger { border-color: #dc2626; background: #450a0a; color: #fca5a5; }
  .decision-banner { padding: 12px 16px; border-radius: 8px; margin-bottom: 14px; font-weight: bold; font-size: 15px; border-left: 5px solid #22c55e; background: #0f172a; color: #4ade80; }
  .decision-banner.buy { border-left-color: #f97316; color: #fdba74; background: #1c1917; }
  .decision-banner.warn { border-left-color: #eab308; color: #fde047; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 15px; }
  .card { background: #131c2e; padding: 15px; border-radius: 12px; border: 1px solid #1e293b; }
  .spot-price { font-size: 26px; font-weight: bold; color: #38bdf8; margin: 4px 0 8px 0; }
  .fut-row { display: flex; justify-content: space-between; align-items: center; background: #0f172a; padding: 7px 11px; border-radius: 8px; margin-top: 6px; border-left: 3px solid #38bdf8; }
  .fut-row.m2 { border-left-color: #a855f7; }
  .prem { font-size: 11px; padding: 2px 6px; border-radius: 5px; background: #1e293b; color: #34d399; font-weight: bold; }
  .badge { background: #064e3b; color: #34d399; padding: 5px 12px; border-radius: 20px; font-size: 12px; font-weight: bold; }
  .meta { font-size: 11px; color: #94a3b8; margin-top: 2px; }
  .hw-box { background: #131c2e; padding: 15px; border-radius: 12px; border: 1px solid #1e293b; margin-top: 15px; }
  .hw-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(175px, 1fr)); gap: 9px; margin-top: 10px; }
  .hw-tile { background: #0f172a; border: 1px solid #1e293b; padding: 9px 11px; border-radius: 8px; }
  .oc-box { background: #131c2e; padding: 15px; border-radius: 12px; border: 1px solid #1e293b; margin-top: 15px; }
  .oc-header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 12px; }
  .idx-btn { background: #1e293b; color: #cbd5e1; border: 1px solid #334155; padding: 7px 14px; border-radius: 6px; cursor: pointer; font-weight: bold; font-size: 12px; }
  .idx-btn.active { background: #0ea5e9; color: #090d16; border-color: #0ea5e9; }
  .depth-btn { background: #1e293b; color: #fbbf24; border: 1px solid #475569; padding: 7px 12px; border-radius: 6px; cursor: pointer; font-weight: bold; font-size: 12px; }
  .depth-btn.active { background: #fbbf24; color: #090d16; }
  .cockpit-strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(205px, 1fr)); gap: 10px; margin-bottom: 12px; }
  .cp-card { background: #0f172a; border: 1px solid #334155; padding: 10px 13px; border-radius: 8px; }
  .cp-label { font-size: 11px; color: #94a3b8; text-transform: uppercase; }
  .cp-val { font-size: 14px; font-weight: bold; margin-top: 3px; }
  .alert-bar { background: #2e1065; border: 1px solid #7c3aed; color: #e9d5ff; padding: 9px 14px; border-radius: 8px; margin-bottom: 12px; font-size: 12px; display: none; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; text-align: right; }
  th { background: #0f172a; color: #94a3b8; padding: 8px; border-bottom: 1px solid #1e293b; }
  td { padding: 6px 8px; border-bottom: 1px solid #1e293b; }
  .atm-row { background: #1e293b !important; font-weight: bold; }
  .strike-col { text-align: center; font-weight: bold; color: #fbbf24; background: #0f172a; }
  .oi-cell { min-width: 110px; }
  .oi-bar-ce { height: 4px; background: #ef4444; border-radius: 2px; margin-top: 3px; margin-left: auto; }
  .oi-bar-pe { height: 4px; background: #22c55e; border-radius: 2px; margin-top: 3px; margin-right: auto; }
  .tag-quad { font-size: 10px; padding: 2px 5px; border-radius: 4px; background: #1e293b; color: #38bdf8; }
  .regime-banner { background: #0f172a; border-left: 4px solid #22c55e; padding: 10px 12px; border-radius: 6px; margin: 10px 0; font-size: 13px; font-weight: bold; color: #4ade80; }
  .pos-row { display: flex; justify-content: space-between; padding: 7px 0; border-bottom: 1px solid #1e293b; font-size: 13px; }
</style>
</head>
<body>
  <div class="header">
    <h2>🛡️ GammaShield — Institutional Options & Volatility Terminal</h2>
    <div><span class="badge" id="ws_mode">● DIRECT ENGINE ONLINE</span> <span id="tm" style="margin-left:10px;color:#94a3b8;">--:--:--</span></div>
  </div>

  <div class="tabs">
    <button class="tab-btn active" onclick="showTab(1)">Intraday Pro-Desk Execution & Gamma Shield</button>
    <button class="tab-btn" onclick="showTab(2)">Positional Swing & Term-Structure Shield (3–15 Days)</button>
  </div>

  <div id="tab1">
    <div class="shield-banner" id="shield_box">🛡️ GAMMA SHIELD INITIALIZING...</div>
    <div class="decision-banner" id="decision_box">🎯 PRO-DESK SIGNAL LOADING...</div>

    <div class="grid">
      <div class="card">
        <div style="color:#94a3b8;font-size:12px;">NIFTY 50 (NSE)</div>
        <div class="spot-price" id="n_sp">--</div>
        <div class="fut-row">
          <div><strong id="n_f1_name">Current Fut</strong><div class="meta">OI: <span id="n_f1_oi">--</span> | Vol: <span id="n_f1_v">--</span></div></div>
          <div style="text-align:right;"><strong id="n_f1_lp" style="font-size:15px;">--</strong><div><span class="prem" id="n_f1_prem">Basis: --</span></div></div>
        </div>
        <div class="fut-row m2">
          <div><strong id="n_f2_name">Next Month Fut</strong><div class="meta">OI: <span id="n_f2_oi">--</span> | Vol: <span id="n_f2_v">--</span></div></div>
          <div style="text-align:right;"><strong id="n_f2_lp" style="font-size:15px;">--</strong><div><span class="prem" id="n_f2_prem">Basis: --</span></div></div>
        </div>
      </div>

      <div class="card">
        <div style="color:#94a3b8;font-size:12px;">BANK NIFTY (NSE)</div>
        <div class="spot-price" id="bn_sp">--</div>
        <div class="fut-row">
          <div><strong id="bn_f1_name">Current Fut</strong><div class="meta">OI: <span id="bn_f1_oi">--</span> | Vol: <span id="bn_f1_v">--</span></div></div>
          <div style="text-align:right;"><strong id="bn_f1_lp" style="font-size:15px;">--</strong><div><span class="prem" id="bn_f1_prem">Basis: --</span></div></div>
        </div>
        <div class="fut-row m2">
          <div><strong id="bn_f2_name">Next Month Fut</strong><div class="meta">OI: <span id="bn_f2_oi">--</span> | Vol: <span id="bn_f2_v">--</span></div></div>
          <div style="text-align:right;"><strong id="bn_f2_lp" style="font-size:15px;">--</strong><div><span class="prem" id="bn_f2_prem">Basis: --</span></div></div>
        </div>
      </div>

      <div class="card">
        <div style="color:#94a3b8;font-size:12px;">SENSEX (BSE)</div>
        <div class="spot-price" id="sx_sp" style="color:#fbbf24;">--</div>
        <div class="fut-row">
          <div><strong id="sx_f1_name">Current Fut</strong><div class="meta">OI: <span id="sx_f1_oi">--</span> | Vol: <span id="sx_f1_v">--</span></div></div>
          <div style="text-align:right;"><strong id="sx_f1_lp" style="font-size:15px;">--</strong><div><span class="prem" id="sx_f1_prem">Basis: --</span></div></div>
        </div>
        <div class="fut-row m2">
          <div><strong id="sx_f2_name">Next Month Fut</strong><div class="meta">OI: <span id="sx_f2_oi">--</span> | Vol: <span id="sx_f2_v">--</span></div></div>
          <div style="text-align:right;"><strong id="sx_f2_lp" style="font-size:15px;">--</strong><div><span class="prem" id="sx_f2_prem">Basis: --</span></div></div>
        </div>
      </div>
    </div>

    <div class="hw-box">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
        <strong style="font-size:15px;">🏛️ Index Heavyweight Movers & Futures Buildup Radar</strong>
        <div style="font-size:13px;" id="hw_summary">Breadth: -- | Nifty Impact: -- pts | BankNifty Impact: -- pts</div>
      </div>
      <div class="hw-grid" id="hw_grid"></div>
    </div>

    <div class="oc-box">
      <div class="oc-header">
        <div><strong style="font-size:15px;">🎯 Microstructure Greeks, Net GEX, Gamma Flip & Synthetic Basis</strong></div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;">
          <button class="depth-btn active" id="dep_5" onclick="setDepth(5)">ATM ± 5</button>
          <button class="depth-btn" id="dep_12" onclick="setDepth(12)">ATM ± 12 (Full)</button>
          <button class="idx-btn active" id="btn_NIFTY" onclick="setIdx('NIFTY')">NIFTY 50</button>
          <button class="idx-btn" id="btn_BN" onclick="setIdx('BN')">BANK NIFTY</button>
          <button class="idx-btn" id="btn_SX" onclick="setIdx('SX')">SENSEX</button>
        </div>
      </div>

      <div class="cockpit-strip">
        <div class="cp-card">
          <div class="cp-label">Net GEX & Gamma Flip</div>
          <div class="cp-val" id="cp_gex" style="color:#38bdf8;">--</div>
        </div>
        <div class="cp-card">
          <div class="cp-label">Sub-MS Speed, 3m ΔIV & Sweep</div>
          <div class="cp-val" id="cp_vel" style="color:#fbbf24;">--</div>
        </div>
        <div class="cp-card">
          <div class="cp-label">Max Pain & ATM Straddle</div>
          <div class="cp-val" id="cp_mp" style="color:#4ade80;">--</div>
        </div>
        <div class="cp-card">
          <div class="cp-label">Synthetic Fut & PCR</div>
          <div class="cp-val" id="cp_syn">--</div>
        </div>
      </div>

      <div class="alert-bar" id="smart_alert"></div>

      <table>
        <thead>
          <tr>
            <th>CALL OI</th><th>ACTION</th><th>IV %</th><th>Δ / Γ</th><th>CALL LTP</th>
            <th style="text-align:center;">STRIKE</th>
            <th style="text-align:left;">PUT LTP</th><th>Δ / Γ</th><th>IV %</th><th>ACTION</th><th style="text-align:left;">PUT OI</th>
          </tr>
        </thead>
        <tbody id="oc_body"></tbody>
      </table>
    </div>
  </div>

  <div id="tab2" style="display:none;">
    <div class="grid">
      <div class="card" id="pos_NIFTY"></div>
      <div class="card" id="pos_BN"></div>
      <div class="card" id="pos_SX"></div>
    </div>
  </div>

<script>
let selectedIdx = "NIFTY";
let chainDepth = 5;
let latestData = {};

function showTab(n) {
  document.getElementById('tab1').style.display = n===1 ? 'block' : 'none';
  document.getElementById('tab2').style.display = n===2 ? 'block' : 'none';
  document.querySelectorAll('.tab-btn').forEach((b, i) => b.classList.toggle('active', i+1===n));
}

function setIdx(idx) {
  selectedIdx = idx;
  ['NIFTY', 'BN', 'SX'].forEach(k => document.getElementById('btn_' + k).classList.toggle('active', k === idx));
  renderChain();
}

function setDepth(d) {
  chainDepth = d;
  document.getElementById('dep_5').classList.toggle('active', d === 5);
  document.getElementById('dep_12').classList.toggle('active', d === 12);
  renderChain();
}

function updateBlock(prefix, t) {
  let sp = (t[prefix + "_SPOT"] || {}).lp || 0;
  let f1 = t[prefix + "_F1"] || {};
  let f2 = t[prefix + "_F2"] || {};
  let p = prefix === "NIFTY" ? "n" : (prefix === "BN" ? "bn" : "sx");
  document.getElementById(p + "_sp").innerText = sp || "--";
  if (f1.lp) {
    document.getElementById(p + "_f1_name").innerText = f1.tsym || "Current Fut";
    document.getElementById(p + "_f1_lp").innerText = f1.lp;
    document.getElementById(p + "_f1_oi").innerText = f1.oi || 0;
    document.getElementById(p + "_f1_v").innerText = f1.v || 0;
    document.getElementById(p + "_f1_prem").innerText = "Basis: " + (sp ? (f1.lp - sp).toFixed(1) : "--");
  }
  if (f2.lp) {
    document.getElementById(p + "_f2_name").innerText = f2.tsym || "Next Fut";
    document.getElementById(p + "_f2_lp").innerText = f2.lp;
    document.getElementById(p + "_f2_oi").innerText = f2.oi || 0;
    document.getElementById(p + "_f2_v").innerText = f2.v || 0;
    document.getElementById(p + "_f2_prem").innerText = "Basis: " + (sp ? (f2.lp - sp).toFixed(1) : "--");
  }
}

function renderHeavyweights() {
  let hw = (latestData.analytics || {}).heavyweights;
  if (!hw) return;
  document.getElementById('hw_summary').innerHTML = `Breadth: <strong style="color:#fbbf24;">${hw.breadth}</strong> &nbsp;|&nbsp; Nifty Impact: <strong style="color:${hw.nifty_pts>=0?'#4ade80':'#f87171'};">${hw.nifty_pts>=0?'+':''}${hw.nifty_pts} pts</strong> &nbsp;|&nbsp; BankNifty Impact: <strong style="color:${hw.bn_pts>=0?'#4ade80':'#f87171'};">${hw.bn_pts>=0?'+':''}${hw.bn_pts} pts</strong>`;
  let htm = "";
  (hw.stocks || []).forEach(s => {
    let clr = s.chg_pct >= 0 ? "#4ade80" : "#f87171";
    htm += `<div class="hw-tile" style="border-left:3px solid ${clr};">
      <div style="display:flex;justify-content:space-between;font-weight:bold;font-size:12px;">
        <span>${s.sym}</span><span style="color:${clr};">${s.chg_pct>=0?'+':''}${s.chg_pct}%</span>
      </div>
      <div style="font-size:11px;color:#cbd5e1;margin-top:3px;">₹${s.lp} (${s.n_pts>=0?'+':''}${s.n_pts} pts)</div>
      <div style="font-size:10px;color:#38bdf8;margin-top:2px;">${s.buildup}</div>
    </div>`;
  });
  document.getElementById('hw_grid').innerHTML = htm;
}

function renderPositionalCards() {
  let titles = {"NIFTY": "NIFTY 50 — Positional Swing (3–15 Days)", "BN": "BANK NIFTY — Positional Swing (3–15 Days)", "SX": "SENSEX — Positional Swing (3–15 Days)"};
  ['NIFTY', 'BN', 'SX'].forEach(tag => {
    let a = (latestData.analytics || {})[tag];
    let el = document.getElementById('pos_' + tag);
    if (!a || !a.positional) return;
    let p = a.positional;
    el.innerHTML = `
      <div style="color:#94a3b8;font-size:12px;font-weight:bold;">${titles[tag]}</div>
      <div class="regime-banner">${p.pos_action}</div>
      <div class="pos-row"><span>Term Structure Regime:</span><strong style="color:#38bdf8;">${p.term_structure}</strong></div>
      <div class="pos-row"><span>2M Rollover Spread (F2 - F1):</span><strong>${p.cal_spread} pts (${p.ann_carry}% Annual Carry)</strong></div>
      <div class="pos-row"><span>ATM IV & Volatility Skew:</span><strong>${p.atm_iv}% | Skew: ${p.iv_skew > 0 ? '+' : ''}${p.iv_skew}%</strong></div>
      <div class="pos-row"><span>Broad ATM ±12 PCR & Max Pain:</span><strong>PCR ${p.broad_pcr} | Max Pain ${p.max_pain}</strong></div>
      <div class="pos-row"><span>Positional OI Range (Support – Res):</span><strong style="color:#fbbf24;">${p.broad_sup} – ${p.broad_res}</strong></div>
      <div class="pos-row" style="border:none;"><span>10-Day Expected Swing Cone (±1σ):</span><strong style="color:#4ade80;">${p.swing_low} – ${p.swing_high} (±${p.swing_move} pts)</strong></div>
    `;
  });
}

function renderChain() {
  let a = (latestData.analytics || {})[selectedIdx];
  if (!a) return;

  let sb = document.getElementById('shield_box');
  sb.innerText = a.spike_shield;
  sb.className = "shield-banner" + (a.shield_level === "DANGER" ? " danger" : "");

  let db = document.getElementById('decision_box');
  db.innerText = a.pro_action;
  db.className = "decision-banner " + (a.action_cls || "sell");

  document.getElementById('cp_gex').innerText = `${a.net_gex} Cr | Flip: ${a.gamma_flip}`;
  document.getElementById('cp_vel').innerText = `${a.tps}/s | 3m ΔIV: ${a.iv_vel_3m >= 0 ? '+' : ''}${a.iv_vel_3m}% | ${a.ob_signal}`;
  document.getElementById('cp_mp').innerText = `Max Pain: ${a.max_pain} | Strad: ₹${a.straddle}`;
  document.getElementById('cp_syn').innerText = `Syn: ${a.synth_fut} (${a.synth_diff >= 0 ? '+' : ''}${a.synth_diff}) | PCR: ${a.pcr}`;

  let alertBox = document.getElementById('smart_alert');
  if (a.alerts && a.alerts.length > 0) {
    alertBox.style.display = 'block';
    alertBox.innerHTML = "🚨 <strong>SMART MONEY FOOTPRINT (ATM ±6 to ±12 Scan):</strong> &nbsp;" + a.alerts.join(" &nbsp;|&nbsp; ");
  } else {
    alertBox.style.display = 'none';
  }

  let rows = chainDepth === 12 ? (a.rows_12 || []) : (a.rows_5 || []);
  let peak = (chainDepth === 12 ? a.peak_oi_12 : a.peak_oi_5) || 1;
  let html = "";
  rows.forEach(r => {
    let isAtm = r.strike === a.atm ? "atm-row" : "";
    let ce = r.CE || {}, pe = r.PE || {};
    let cePct = Math.min(100, Math.round(((ce.oi || 0) / peak) * 100));
    let pePct = Math.min(100, Math.round(((pe.oi || 0) / peak) * 100));

    html += `<tr class="${isAtm}">
      <td class="oi-cell"><div>${(ce.oi || 0).toLocaleString()}</div><div class="oi-bar-ce" style="width:${cePct}%;"></div></td>
      <td><span class="tag-quad">${ce.quad || "-"}</span></td>
      <td style="color:#cbd5e1;">${ce.iv || "--"}%</td>
      <td style="color:#94a3b8;">${ce.delta || "--"} / ${ce.gamma || "--"}</td>
      <td style="color:#38bdf8;font-weight:bold;">${ce.lp || 0}</td>
      <td class="strike-col">${r.strike}</td>
      <td style="text-align:left;color:#f472b6;font-weight:bold;">${pe.lp || 0}</td>
      <td style="color:#94a3b8;">${pe.delta || "--"} / ${pe.gamma || "--"}</td>
      <td style="color:#cbd5e1;">${pe.iv || "--"}%</td>
      <td><span class="tag-quad">${pe.quad || "-"}</span></td>
      <td class="oi-cell" style="text-align:left;"><div>${(pe.oi || 0).toLocaleString()}</div><div class="oi-bar-pe" style="width:${pePct}%;"></div></td>
    </tr>`;
  });
  document.getElementById('oc_body').innerHTML = html;
}

setInterval(() => {
  fetch('/api/state').then(r => r.json()).then(d => {
    latestData = d;
    document.getElementById('tm').innerText = d.last_update;
    if (d.micro && d.micro.mode) document.getElementById('ws_mode').innerText = "● " + d.micro.mode;
    updateBlock("NIFTY", d.ticks || {});
    updateBlock("BN", d.ticks || {});
    updateBlock("SX", d.ticks || {});
    renderHeavyweights();
    renderChain();
    renderPositionalCards();
  });
}, 400);
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML_DASHBOARD)

@app.route("/api/batch", methods=["POST"])
def receive_batch():
    data = request.get_json(force=True)
    LIVE_STATE["micro"] = data.get("micro", {})
    LIVE_STATE["ticks"] = data.get("ticks", {})
    LIVE_STATE["heavyweights"] = data.get("heavyweights", [])
    LIVE_STATE["chains"] = data.get("chains", {})
    LIVE_STATE["analytics"] = compute_master_analytics(LIVE_STATE["ticks"], LIVE_STATE["chains"], LIVE_STATE["micro"], LIVE_STATE["heavyweights"])
    LIVE_STATE["last_update"] = time.strftime("%H:%M:%S")
    return jsonify({"ok": True})

@app.route("/api/state")
def get_state():
    return jsonify(LIVE_STATE)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
