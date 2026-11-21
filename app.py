import math
import time
from datetime import datetime, timedelta
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

SIGNAL_LOCK = {
    "confirmed_key": "",
    "candidate_key": "",
    "candidate_count": 0,
    "shift_ts": 0.0,
    "shift_time_str": "",
    "prev_action": ""
}

TOP4_LOCK = {
    "bull_syms": [],
    "bear_syms": [],
    "cand_bull": [],
    "cand_bull_cnt": 0,
    "cand_bear": [],
    "cand_bear_cnt": 0
}

BANK_STOCKS = {"HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK", "INDUSINDBK"}
BANK_WEIGHTS = {
    "HDFCBANK": 29.0, "ICICIBANK": 24.5, "SBIN": 10.5,
    "AXISBANK": 9.8, "KOTAKBANK": 9.5, "INDUSINDBK": 4.2
}

def ist_now_str():
    ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    return ist.strftime("%I:%M:%S %p")

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

def enrich_chain(raw_rows, spot, step=50, lot_size=75, dte_days=3.5):
    if not raw_rows or spot <= 0:
        return {
            "rows": [], "net_gex_cr": 0.0, "max_pain": 0, "gamma_flip": 0,
            "pcr": 1.0, "atm_iv": 13.5, "ce_atm_iv": 13.5, "pe_atm_iv": 13.5,
            "sweep_bias": 0.0, "tot_ce_oi": 0, "tot_pe_oi": 0,
            "atm_strike": 0, "atm_ce_lp": 0, "atm_pe_lp": 0,
            "call_wall": 0, "put_wall": 0, "itm_flow": "BALANCED"
        }
    T = max(dte_days, 0.25) / 365.0
    r = 0.065
    by_strike = {}
    tot_ce_oi, tot_pe_oi = 0, 0
    tot_tbq, tot_tsq = 0, 0
    max_oi = 1
    call_wall_k, call_wall_oi = 0, -1
    put_wall_k, put_wall_oi = 0, -1
    itm_ce_doi, itm_pe_doi = 0, 0

    for item in raw_rows:
        k = int(item.get("strike", 0))
        optt = item.get("optt", "CE")
        lp = float(item.get("lp", 0) or 0)
        oi = int(item.get("oi", 0) or 0)
        poi = int(item.get("poi", oi) or 0)
        v = int(item.get("v", 0) or 0)
        tbq = int(item.get("tbq", 0) or 0)
        tsq = int(item.get("tsq", 0) or 0)

        if oi > max_oi:
            max_oi = oi

        iv = implied_vol(lp, spot, k, T, r, optt)
        gk = bs_greeks(spot, k, T, r, iv, optt)
        doi = oi - poi

        if optt == "CE":
            tot_ce_oi += oi
            tot_tbq += tbq
            tot_tsq += tsq
            if k < spot:
                itm_ce_doi += doi
            if k >= spot and oi > call_wall_oi:
                call_wall_oi = oi
                call_wall_k = k
        else:
            tot_pe_oi += oi
            tot_tbq += tsq
            tot_tsq += tbq
            if k > spot:
                itm_pe_doi += doi
            if k <= spot and oi > put_wall_oi:
                put_wall_oi = oi
                put_wall_k = k

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
    atm_strike = min(strikes, key=lambda x: abs(x - spot)) if strikes else int(round(spot / step) * step)
    atm_iv, ce_atm_iv, pe_atm_iv = 13.5, 13.5, 13.5
    atm_ce_lp, atm_pe_lp = 0.0, 0.0

    rows_out = []
    for k in strikes:
        ce = by_strike[k].get("CE", {"lp": 0, "oi": 0, "doi": 0, "v": 0, "iv": 13.5, "delta": 0, "gamma": 0, "theta": 0, "vega": 0})
        pe = by_strike[k].get("PE", {"lp": 0, "oi": 0, "doi": 0, "v": 0, "iv": 13.5, "delta": 0, "gamma": 0, "theta": 0, "vega": 0})
        ce_gex = ce["gamma"] * ce["oi"] * lot_size * (spot ** 2) * 0.01
        pe_gex = -pe["gamma"] * pe["oi"] * lot_size * (spot ** 2) * 0.01
        net_gex += (ce_gex + pe_gex)

        if k == atm_strike:
            ce_atm_iv = ce["iv"]
            pe_atm_iv = pe["iv"]
            atm_iv = round((ce_atm_iv + pe_atm_iv) / 2.0, 1)
            atm_ce_lp = ce["lp"]
            atm_pe_lp = pe["lp"]

        ce_act = "SHORT COVERING" if ce["doi"] < -25000 else ("CALL WRITING" if ce["doi"] > 25000 else "NEUTRAL")
        pe_act = "PUT WRITING" if pe["doi"] > 25000 else ("LONG UNWINDING" if pe["doi"] < -25000 else "NEUTRAL")

        rows_out.append({
            "strike": k,
            "is_atm": (k == atm_strike),
            "ce": ce, "pe": pe,
            "ce_pct": round((ce["oi"] / max_oi) * 100, 1),
            "pe_pct": round((pe["oi"] / max_oi) * 100, 1),
            "ce_act": ce_act, "pe_act": pe_act
        })

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

    gamma_flip = atm_strike
    for row in rows_out:
        if row["ce"]["oi"] > row["pe"]["oi"] and row["strike"] >= spot * 0.985:
            gamma_flip = row["strike"]
            break

    pcr = round(tot_pe_oi / tot_ce_oi, 2) if tot_ce_oi > 0 else 1.0
    sweep_bias = round(((tot_tbq - tot_tsq) / max(tot_tbq + tot_tsq, 1)) * 100, 1)

    if itm_ce_doi < -15000:
        itm_flow = "ITM CALL WRITERS TRAPPED (SQUEEZE)"
    elif itm_ce_doi > 15000:
        itm_flow = "ITM CALL BUYING (INSTITUTIONAL)"
    elif itm_pe_doi > 15000:
        itm_flow = "ITM PUT BUYING (BEARISH)"
    elif itm_pe_doi < -15000:
        itm_flow = "ITM PUT WRITERS TRAPPED (DROP)"
    else:
        itm_flow = "BALANCED ITM FLOW"

    return {
        "rows": rows_out,
        "net_gex_cr": round(net_gex / 1e7, 1),
        "max_pain": max_pain,
        "gamma_flip": gamma_flip,
        "pcr": pcr,
        "atm_iv": atm_iv,
        "ce_atm_iv": ce_atm_iv,
        "pe_atm_iv": pe_atm_iv,
        "sweep_bias": sweep_bias,
        "tot_ce_oi": tot_ce_oi,
        "tot_pe_oi": tot_pe_oi,
        "atm_strike": atm_strike,
        "atm_ce_lp": atm_ce_lp,
        "atm_pe_lp": atm_pe_lp,
        "call_wall": call_wall_k or (atm_strike + 4 * step),
        "put_wall": put_wall_k or (atm_strike - 4 * step),
        "itm_flow": itm_flow
    }

def compute_decision_engine(state):
    ticks = state.get("ticks", {})
    hws = state.get("heavyweights", [])
    n_spot = float((ticks.get("NIFTY_SPOT") or {}).get("lp", 0) or 0)
    n_f1 = float((ticks.get("NIFTY_F1") or {}).get("lp", n_spot) or n_spot)
    n_f2 = float((ticks.get("NIFTY_F2") or {}).get("lp", n_f1) or n_f1)
    bn_spot = float((ticks.get("BN_SPOT") or {}).get("lp", 0) or 0)
    sx_spot = float((ticks.get("SX_SPOT") or {}).get("lp", 0) or 0)

    n_chain = enrich_chain(state.get("chains", {}).get("NIFTY", []), n_spot, step=50, lot_size=75, dte_days=3.5)
    bn_chain = enrich_chain(state.get("chains", {}).get("BN", []), bn_spot, step=100, lot_size=30, dte_days=3.5)
    sx_chain = enrich_chain(state.get("chains", {}).get("SX", []), sx_spot, step=100, lot_size=20, dte_days=3.5)

    raw_stk_chains = state.get("stock_chains", {})
    enriched_stk_chains = {}
    for h in hws:
        sym = h.get("sym", "")
        sp = float(h.get("lp", 0) or 0)
        if sym in raw_stk_chains:
            enriched_stk_chains[sym] = enrich_chain(raw_stk_chains[sym], sp, step=10, lot_size=500, dte_days=12.0)

    green_cnt = 0
    nifty_pts = 0.0
    bn_pts = 0.0
    sensex_pts = 0.0
    vel_score = 0.0
    enriched_hws = []

    for h in hws:
        sym = h.get("sym", "")
        chg = float(h.get("chg_pct", 0) or 0)
        vel_30s = float(h.get("vel_30s", 0) or 0)
        wt_n = float(h.get("wt", 0) or 0)
        wt_sx = float(h.get("wt_sx", wt_n * 1.15) or 0)
        f_doi = int(h.get("fut_doi", 0) or 0)

        if chg > 0:
            green_cnt += 1

        n_imp = round((n_spot * (wt_n / 100.0) * (chg / 100.0)), 1) if n_spot > 0 else 0.0
        sx_imp = round((sx_spot * (wt_sx / 100.0) * (chg / 100.0)), 1) if sx_spot > 0 else 0.0
        bn_imp = round((bn_spot * (BANK_WEIGHTS.get(sym, 0.0) / 100.0) * (chg / 100.0)), 1) if (bn_spot > 0 and sym in BANK_STOCKS) else 0.0

        nifty_pts += n_imp
        sensex_pts += sx_imp
        bn_pts += bn_imp
        vel_score += vel_30s * wt_n

        if chg >= 0 and f_doi >= 0:
            buildup = "LONG BUILDUP"
        elif chg >= 0 and f_doi < 0:
            buildup = "SHORT COVERING"
        elif chg < 0 and f_doi >= 0:
            buildup = "SHORT BUILDUP"
        else:
            buildup = "LONG UNWINDING"

        stk_lp = float(h.get("lp", 0) or 0)
        sc_obj = enriched_stk_chains.get(sym)
        stk_iv = sc_obj["atm_iv"] if sc_obj else float(h.get("atm_iv", 22.5) or 22.5)
        stk_gk = bs_greeks(stk_lp, stk_lp, 12.0 / 365.0, 0.065, stk_iv / 100.0, "CE")
        atm_k_stk = sc_obj["atm_strike"] if (sc_obj and sc_obj["atm_strike"] > 0) else round(stk_lp, 0)
        c_wall_stk = sc_obj["call_wall"] if (sc_obj and sc_obj["call_wall"] > 0) else round(stk_lp * 1.025, 0)
        p_wall_stk = sc_obj["put_wall"] if (sc_obj and sc_obj["put_wall"] > 0) else round(stk_lp * 0.975, 0)

        if sc_obj and sc_obj["itm_flow"] != "BALANCED ITM FLOW":
            itm_tag = sc_obj["itm_flow"]
        elif chg > 0.75 and f_doi > 0:
            itm_tag = "ITM CALL BUYING (INSTITUTIONAL)"
        elif chg < -0.75 and f_doi > 0:
            itm_tag = "ITM PUT BUYING (BEARISH)"
        elif chg > 0.65 and f_doi < 0:
            itm_tag = "ITM WRITERS TRAPPED (SQUEEZE)"
        else:
            itm_tag = "NEUTRAL ITM FLOW"

        if chg > 0.85 and f_doi > 0:
            horizon = "BOTH: INTRADAY 2%+ BREAKOUT & POSITIONAL (3–15D)"
            stk_sig = f"BUY {atm_k_stk} CE (1-ITM/ATM) | BULL SPREAD"
            exit_rule = f"EXIT IF: Fut OI starts unwinding or price slips below ₹{round(stk_lp * 0.992, 1)} (Book Partial at Call Wall ₹{c_wall_stk})"
            bull_score = (chg * 12) + (abs(n_imp) * 3) + (15 if f_doi > 0 else 0) + (vel_30s * 20)
            bear_score = -999
        elif chg < -0.85 and f_doi > 0:
            horizon = "BOTH: INTRADAY 2%+ BREAKDOWN & POSITIONAL (3–15D)"
            stk_sig = f"BUY {atm_k_stk} PE (1-ITM/ATM) | BEAR SPREAD"
            exit_rule = f"EXIT IF: Fut OI covers or price reclaims ₹{round(stk_lp * 1.008, 1)} (Book Partial at Put Wall ₹{p_wall_stk})"
            bear_score = (abs(chg) * 12) + (abs(n_imp) * 3) + (15 if f_doi > 0 else 0) + (abs(vel_30s) * 20)
            bull_score = -999
        elif chg >= 0:
            horizon = "INTRADAY SCALP / SELL OTM PUT"
            stk_sig = f"SELL {p_wall_stk} PE (THETA)"
            exit_rule = f"EXIT IF: Price breaks Put Support Wall ₹{p_wall_stk}"
            bull_score = (chg * 10) + (abs(n_imp) * 2)
            bear_score = -999
        else:
            horizon = "INTRADAY SCALP / SELL OTM CE"
            stk_sig = f"SELL {c_wall_stk} CE (THETA)"
            exit_rule = f"EXIT IF: Price breaks Call Resistance Wall ₹{c_wall_stk}"
            bear_score = (abs(chg) * 10) + (abs(n_imp) * 2)
            bull_score = -999

        item = dict(h)
        item.update({
            "nifty_pts": n_imp,
            "bn_pts": bn_imp,
            "sensex_pts": sx_imp,
            "buildup": buildup,
            "atm_iv": stk_iv,
            "atm_delta": stk_gk["delta"],
            "atm_gamma": stk_gk["gamma"],
            "itm_tag": itm_tag,
            "horizon": horizon,
            "stk_signal": stk_sig,
            "exit_rule": exit_rule,
            "bull_score": bull_score,
            "bear_score": bear_score
        })
        enriched_hws.append(item)

    # Anti-Flicker Lock for Top 2 Bull & Top 2 Bear (Requires 12 consecutive cycles to swap names)
    hw_by_sym = {x["sym"]: x for x in enriched_hws}
    raw_bull_syms = [x["sym"] for x in sorted([x for x in enriched_hws if x["chg_pct"] > 0], key=lambda x: x["bull_score"], reverse=True)[:2]]
    raw_bear_syms = [x["sym"] for x in sorted([x for x in enriched_hws if x["chg_pct"] < 0], key=lambda x: x["bear_score"], reverse=True)[:2]]

    if not TOP4_LOCK["bull_syms"]:
        TOP4_LOCK["bull_syms"] = raw_bull_syms
    elif raw_bull_syms != TOP4_LOCK["bull_syms"]:
        if raw_bull_syms == TOP4_LOCK["cand_bull"]:
            TOP4_LOCK["cand_bull_cnt"] += 1
        else:
            TOP4_LOCK["cand_bull"] = raw_bull_syms
            TOP4_LOCK["cand_bull_cnt"] = 1
        if TOP4_LOCK["cand_bull_cnt"] >= 12:
            TOP4_LOCK["bull_syms"] = raw_bull_syms
            TOP4_LOCK["cand_bull_cnt"] = 0
    else:
        TOP4_LOCK["cand_bull_cnt"] = 0

    if not TOP4_LOCK["bear_syms"]:
        TOP4_LOCK["bear_syms"] = raw_bear_syms
    elif raw_bear_syms != TOP4_LOCK["bear_syms"]:
        if raw_bear_syms == TOP4_LOCK["cand_bear"]:
            TOP4_LOCK["cand_bear_cnt"] += 1
        else:
            TOP4_LOCK["cand_bear"] = raw_bear_syms
            TOP4_LOCK["cand_bear_cnt"] = 1
        if TOP4_LOCK["cand_bear_cnt"] >= 12:
            TOP4_LOCK["bear_syms"] = raw_bear_syms
            TOP4_LOCK["cand_bear_cnt"] = 0
    else:
        TOP4_LOCK["cand_bear_cnt"] = 0

    top2_bull = [hw_by_sym[s] for s in TOP4_LOCK["bull_syms"] if s in hw_by_sym]
    top2_bear = [hw_by_sym[s] for s in TOP4_LOCK["bear_syms"] if s in hw_by_sym]

    enriched_hws.sort(key=lambda x: abs(x["nifty_pts"]), reverse=True)
    top_movers = [f"{x['sym']} ({'+' if x['nifty_pts']>=0 else ''}{x['nifty_pts']}p)" for x in enriched_hws[:3]]

    # 80/20 Regime & Side-Specific Gamma Blast Engine
    gex = n_chain["net_gex_cr"]
    pcr = n_chain["pcr"]
    iv = n_chain["atm_iv"]
    sweep = n_chain["sweep_bias"]
    atm_k = n_chain["atm_strike"]
    atm_ce = n_chain["atm_ce_lp"]
    atm_pe = n_chain["atm_pe_lp"]
    c_wall = n_chain["call_wall"]
    p_wall = n_chain["put_wall"]
    mp = n_chain["max_pain"]
    total_hws = max(len(enriched_hws), 1)
    breadth_ratio = green_cnt / total_hws
    dist_mp_pct = (abs(n_spot - mp) / max(n_spot, 1)) * 100.0

    if gex < -150 and (breadth_ratio >= 0.70 or breadth_ratio <= 0.30) and iv >= 14.2:
        if breadth_ratio >= 0.70 and (pcr >= 1.02 or sweep > 8):
            raw_key = "GAMMA_BUY_CE"
            regime = "20% GAMMA EXPANSION ZONE (CALL-SIDE EXPLOSION)"
            regime_color = "#10b981"
            action = f"CALL-SIDE GAMMA BLAST: EXIT SHORT CALLS (CE) IMMEDIATELY -> STRONG BUY {atm_k} CE"
            sub = f"Negative GEX ({gex} Cr) + {green_cnt}/{total_hws} Stocks Surging (Nifty {'+' if nifty_pts>=0 else ''}{round(nifty_pts,1)}p, BN {'+' if bn_pts>=0 else ''}{round(bn_pts,1)}p) | Keep Short PE with Trailing SL"
            trade_card = f"BUY {atm_k} CE @ ₹{atm_ce} | SL: ₹{round(atm_ce*0.78,1)} | Target: ₹{round(atm_ce*1.48,1)} | URGENT: Square-Off Any Short CE Leg!"
            banner_bg = "linear-gradient(90deg, #064e3b, #047857)"
            border_c = "#10b981"
        elif breadth_ratio <= 0.30 and (pcr <= 0.95 or sweep < -8):
            raw_key = "GAMMA_BUY_PE"
            regime = "20% GAMMA CRASH ZONE (PUT-SIDE EXPLOSION)"
            regime_color = "#ef4444"
            action = f"PUT-SIDE GAMMA CRASH: EXIT SHORT PUTS (PE) IMMEDIATELY -> STRONG BUY {atm_k} PE"
            sub = f"Negative GEX ({gex} Cr) + Heavyweight Selloff ({green_cnt}/{total_hws} Green, Nifty {round(nifty_pts,1)}p, BN {round(bn_pts,1)}p) | Short CE Leg Safe"
            trade_card = f"BUY {atm_k} PE @ ₹{atm_pe} | SL: ₹{round(atm_pe*0.78,1)} | Target: ₹{round(atm_pe*1.48,1)} | URGENT: Square-Off Any Short PE Leg!"
            banner_bg = "linear-gradient(90deg, #7f1d1d, #b91c1c)"
            border_c = "#ef4444"
        else:
            raw_key = "VOL_SHOCK_BOTH"
            regime = "20% VOLATILITY SHOCK (BOTH SIDES AT RISK)"
            regime_color = "#f59e0b"
            action = "VOLATILITY SHOCK: EXIT FULL STRADDLE / STRANGLE -> SHIFT TO HEDGED DEBIT SPREAD"
            sub = f"Negative GEX ({gex} Cr) + Dual-Side IV Surge ({iv}%) | Naked Selling Dangerous"
            trade_card = f"CLOSE NAKED SHORT LEGS | Safe Alternative: BULL/BEAR DEBIT SPREAD around {atm_k}"
            banner_bg = "linear-gradient(90deg, #78350f, #b45309)"
            border_c = "#f59e0b"
    else:
        if gex >= 200 and 0.40 <= breadth_ratio <= 0.62 and dist_mp_pct <= 0.35:
            raw_key = "SELL_STRADDLE"
            regime = "80% SELLER REWARD ZONE — MAX THETA CRUSH (PINNED)"
            regime_color = "#38bdf8"
            comb_prem = round(atm_ce + atm_pe, 1)
            action = f"SELL ATM STRADDLE ({atm_k} CE + {atm_k} PE) — DEALERS PINNING MARKET"
            sub = f"Strong Positive GEX (+{gex} Cr) + Spot Pinned Near Max Pain {mp} + Balanced Heavyweights ({green_cnt}/{total_hws} Green)"
            trade_card = f"SELL {atm_k} CE (₹{atm_ce}) + SELL {atm_k} PE (₹{atm_pe}) | Combined Prem: ₹{comb_prem} | Combined SL: ₹{round(comb_prem*1.22,1)}"
            banner_bg = "linear-gradient(90deg, #1e3a8a, #1d4ed8)"
            border_c = "#60a5fa"
        elif breadth_ratio >= 0.63 and pcr >= 1.12:
            raw_key = "SELL_OTM_PE"
            regime = "80% SELLER REWARD ZONE — BULLISH PUT WRITING SUPPORT"
            regime_color = "#34d399"
            action = f"SELL OTM PUT / BULL PUT SPREAD BELOW {p_wall} PE (AVOID SHORTING CALLS)"
            sub = f"Positive GEX (+{gex} Cr) + Strong Put Support Wall at {p_wall} (PCR {pcr}) + Nifty {'+' if nifty_pts>=0 else ''}{round(nifty_pts,1)} pts"
            trade_card = f"SELL {p_wall} PE + BUY {p_wall - 200} PE (Bull Put Credit Spread) | High Theta Win Probability"
            banner_bg = "linear-gradient(90deg, #065f46, #0f766e)"
            border_c = "#34d399"
        elif breadth_ratio <= 0.37 and pcr <= 0.88:
            raw_key = "SELL_OTM_CE"
            regime = "80% SELLER REWARD ZONE — BEARISH CALL WRITING WALL"
            regime_color = "#fb7185"
            action = f"SELL OTM CALL / BEAR CALL SPREAD ABOVE {c_wall} CE (AVOID SHORTING PUTS)"
            sub = f"Heavy Call Resistance Wall at {c_wall} (PCR {pcr}) + Weak Heavyweights ({green_cnt}/{total_hws} Green, Nifty {round(nifty_pts,1)} pts)"
            trade_card = f"SELL {c_wall} CE + BUY {c_wall + 200} CE (Bear Call Credit Spread) | High Theta Win Probability"
            banner_bg = "linear-gradient(90deg, #881337, #9f1239)"
            border_c = "#fb7185"
        else:
            raw_key = "SELL_STRANGLE"
            regime = "80% SELLER REWARD ZONE — RANGE-BOUND STRANGLE / IRON CONDOR"
            regime_color = "#38bdf8"
            action = f"SELL OTM STRANGLE / IRON CONDOR ({p_wall} PE & {c_wall} CE) — AVOID NAKED BUYING"
            sub = f"Positive GEX (+{gex} Cr) Absorbing Moves Between Put Wall {p_wall} and Call Wall {c_wall} (Max Pain {mp})"
            trade_card = f"SELL {c_wall} CE + SELL {p_wall} PE (Safe Range: {c_wall - p_wall} pts) | Hedge Wings at ±200 pts for Margin Benefit"
            banner_bg = "linear-gradient(90deg, #1e3a8a, #0369a1)"
            border_c = "#38bdf8"

    now_ts = time.time()
    if not SIGNAL_LOCK["confirmed_key"]:
        SIGNAL_LOCK["confirmed_key"] = raw_key
        SIGNAL_LOCK["shift_ts"] = now_ts
        SIGNAL_LOCK["shift_time_str"] = ist_now_str()
    elif raw_key != SIGNAL_LOCK["confirmed_key"]:
        if raw_key == SIGNAL_LOCK["candidate_key"]:
            SIGNAL_LOCK["candidate_count"] += 1
        else:
            SIGNAL_LOCK["candidate_key"] = raw_key
            SIGNAL_LOCK["candidate_count"] = 1

        if SIGNAL_LOCK["candidate_count"] >= 3:
            SIGNAL_LOCK["prev_action"] = SIGNAL_LOCK["confirmed_key"]
            SIGNAL_LOCK["confirmed_key"] = raw_key
            SIGNAL_LOCK["candidate_count"] = 0
            SIGNAL_LOCK["shift_ts"] = now_ts
            SIGNAL_LOCK["shift_time_str"] = ist_now_str()
    else:
        SIGNAL_LOCK["candidate_count"] = 0

    secs_since_shift = now_ts - SIGNAL_LOCK["shift_ts"]
    is_blinking_1min = (secs_since_shift <= 60.0)
    blink_remaining = max(0, int(60 - secs_since_shift))

    lead_alert = None
    if vel_score > 14.0 and breadth_ratio >= 0.65:
        lead_alert = f"30-SEC STOCK LEAD SURGE (BULLISH): Top Heavyweights Accelerating -> Nifty {'+' if nifty_pts>=0 else ''}{round(nifty_pts,1)}p | BN {'+' if bn_pts>=0 else ''}{round(bn_pts,1)}p | Sensex {'+' if sensex_pts>=0 else ''}{round(sensex_pts,1)}p"
    elif vel_score < -14.0 and breadth_ratio <= 0.35:
        lead_alert = f"30-SEC STOCK LEAD DROP (BEARISH): Top Heavyweights Selling -> Nifty {round(nifty_pts,1)}p | BN {round(bn_pts,1)}p | Sensex {round(sensex_pts,1)}p"

    basis_f1 = round(n_f1 - n_spot, 1)
    roll_spread = round(n_f2 - n_f1, 1)
    if iv < 13.0 and breadth_ratio >= 0.60:
        pos_verdict = "POSITIONAL DEBIT CALL SPREAD (LOW IV ENTRY — 3 TO 15 DAYS)"
        pos_reason = f"ATM IV {iv}% is cheap (<13%) + F1-F2 Rollover Spread +{roll_spread} pts shows institutional long carry."
    elif iv >= 14.2:
        pos_verdict = "POSITIONAL IRON CONDOR / CREDIT SPREAD (HIGH IV THETA SELL — 3 TO 15 DAYS)"
        pos_reason = f"Elevated IV ({iv}%) offers rich 3–15 day theta decay; sell {c_wall} CE & {p_wall} PE outside Max Pain {mp}."
    else:
        pos_verdict = "POSITIONAL BULL PUT / RATIO SPREAD (3 TO 15 DAYS)"
        pos_reason = f"F1 Basis +{basis_f1} pts | Net Nifty Impact {'+' if nifty_pts>=0 else ''}{round(nifty_pts,1)} pts | Sensex {'+' if sensex_pts>=0 else ''}{round(sensex_pts,1)} pts."

    return {
        "micro": state.get("micro", {}),
        "ticks": ticks,
        "decision": {
            "confirmed_key": SIGNAL_LOCK["confirmed_key"],
            "shift_time_str": SIGNAL_LOCK["shift_time_str"],
            "is_blinking_1min": is_blinking_1min,
            "blink_remaining": blink_remaining,
            "regime": regime,
            "regime_color": regime_color,
            "action": action,
            "sub": sub,
            "trade_card": trade_card,
            "banner_bg": banner_bg,
            "border_c": border_c,
            "lead_alert": lead_alert,
            "green_cnt": green_cnt,
            "total_hws": total_hws,
            "nifty_pts": round(nifty_pts, 1),
            "bn_pts": round(bn_pts, 1),
            "sensex_pts": round(sensex_pts, 1),
            "top_movers": top_movers,
            "pos_verdict": pos_verdict,
            "pos_reason": pos_reason,
            "basis_f1": basis_f1,
            "roll_spread": roll_spread,
            "top2_bull": top2_bull,
            "top2_bear": top2_bear
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
<title>GammaShield v2.6 — Institutional Options, Futures & 30-Stock Terminal</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Segoe UI', Roboto, Arial, sans-serif; }
  body { background: #070b14; color: #e2e8f0; padding: 12px 18px; font-size: 13px; }
  .topbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; border-bottom: 1px solid #1e293b; padding-bottom: 8px; }
  .brand { font-size: 18px; font-weight: 700; color: #f8fafc; letter-spacing: 0.4px; }
  .brand span { color: #38bdf8; }
  .status-pill { background: #064e3b; color: #34d399; border: 1px solid #10b981; padding: 4px 10px; border-radius: 4px; font-weight: 700; font-size: 11px; }
  .mute-btn { background: #1e293b; color: #cbd5e1; border: 1px solid #334155; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 11px; margin-right: 8px; font-weight: 700; }
  .tabs { display: flex; gap: 8px; margin-bottom: 10px; }
  .tab-btn { background: #0f172a; color: #94a3b8; border: 1px solid #1e293b; padding: 7px 14px; border-radius: 5px; cursor: pointer; font-weight: 600; font-size: 12px; }
  .tab-btn.active { background: #0284c7; color: #ffffff; border-color: #38bdf8; }
  .regime-strip { display: flex; justify-content: space-between; align-items: center; background: #0f172a; border: 1px solid #1e293b; padding: 6px 12px; border-radius: 5px; margin-bottom: 8px; font-size: 12px; }
  .action-banner { padding: 12px 16px; border-radius: 6px; margin-bottom: 10px; border-left: 6px solid #10b981; transition: 0.2s; }
  .blink-1min { animation: regimeBlink 1.1s infinite; box-shadow: 0 0 18px rgba(251, 191, 36, 0.55); border: 2px solid #fbbf24 !important; }
  @keyframes regimeBlink { 0% { opacity: 1; } 50% { opacity: 0.58; } 100% { opacity: 1; } }
  .action-hdr { display: flex; justify-content: space-between; align-items: center; }
  .action-title { font-size: 16px; font-weight: 800; color: #ffffff; letter-spacing: 0.3px; }
  .shift-tag { background: rgba(0,0,0,0.45); color: #fde047; padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: 800; }
  .action-sub { font-size: 12px; color: #e2e8f0; margin-top: 4px; }
  .trade-box { margin-top: 7px; background: rgba(0,0,0,0.35); border: 1px dashed rgba(255,255,255,0.35); padding: 6px 10px; border-radius: 4px; font-weight: 800; color: #fef08a; font-size: 12.5px; }
  .lead-bar { background: #1e1b4b; border: 1px solid #6366f1; color: #c7d2fe; padding: 6px 12px; border-radius: 5px; font-weight: 700; margin-bottom: 8px; font-size: 12px; }
  .idx-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 10px; }
  .top4-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-bottom: 12px; }
  .card { background: #0d1424; border: 1px solid #1e293b; border-radius: 6px; padding: 10px 12px; }
  .bull-card { background: linear-gradient(135deg, #06281e, #0d1424); border: 1px solid #10b981; border-left: 5px solid #10b981; border-radius: 6px; padding: 10px 12px; cursor: pointer; }
  .bear-card { background: linear-gradient(135deg, #2d0b14, #0d1424); border: 1px solid #ef4444; border-left: 5px solid #ef4444; border-radius: 6px; padding: 10px 12px; cursor: pointer; }
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
  .chain-wrap { max-height: 440px; overflow-y: auto; border: 1px solid #1e293b; border-radius: 6px; }
  table { width: 100%; border-collapse: collapse; font-size: 11.5px; }
  th { background: #111827; color: #94a3b8; padding: 7px 6px; position: sticky; top: 0; z-index: 2; border-bottom: 1px solid #334155; text-align: center; }
  td { padding: 5px 6px; border-bottom: 1px solid #172033; text-align: center; }
  tr.atm-row { background: #172554 !important; font-weight: 700; }
  tr:hover { background: #131d33; }
  .oi-cell { position: relative; min-width: 145px; text-align: right; padding-right: 8px; }
  .oi-cell-pe { position: relative; min-width: 145px; text-align: left; padding-left: 8px; }
  .oi-bar-ce { position: absolute; top: 2px; bottom: 2px; right: 0; background: rgba(239, 68, 68, 0.34); border-right: 3px solid #ef4444; z-index: 0; border-radius: 2px 0 0 2px; }
  .oi-bar-pe { position: absolute; top: 2px; bottom: 2px; left: 0; background: rgba(16, 185, 129, 0.34); border-left: 3px solid #10b981; z-index: 0; border-radius: 0 2px 2px 0; }
  .oi-txt { position: relative; z-index: 1; font-weight: 600; }
  .bottom-oi-bar { display: flex; justify-content: space-between; align-items: center; background: #0f172a; border: 1px solid #1e293b; padding: 8px 12px; border-radius: 6px; margin-top: 8px; font-size: 12px; }
  .pcr-track { flex: 1; height: 10px; background: #7f1d1d; border-radius: 5px; margin: 0 14px; overflow: hidden; display: flex; }
  .pcr-fill { height: 100%; background: #10b981; transition: width 0.4s; }
  .badge { padding: 2px 6px; border-radius: 3px; font-size: 10px; font-weight: 700; }
  .b-buy { background: #064e3b; color: #34d399; }
  .b-sell { background: #7f1d1d; color: #fca5a5; }
  .b-neu { background: #1e293b; color: #94a3b8; }
  .hidden { display: none; }
</style>
</head>
<body>

<div class="topbar">
  <div class="brand">&#9724; GammaShield <span>v2.6</span> — 80/20 Institutional Options, Futures &amp; 30-Stock Terminal</div>
  <div>
    <button id="muteBtn" class="mute-btn" onclick="toggleSound()">&#128266; CHIME ON</button>
    <span id="tpsBadge" style="color:#94a3b8; margin-right:10px; font-size:11.5px;">0 ticks/s</span>
    <span id="modeBadge" class="status-pill">CONNECTING...</span>
  </div>
</div>

<div class="tabs">
  <button class="tab-btn active" onclick="switchTab(1, this)">1. Intraday 80/20 Execution &amp; Gamma Shield</button>
  <button class="tab-btn" onclick="switchTab(2, this)">2. Positional Swing &amp; Term-Structure (3–15 Days)</button>
  <button class="tab-btn" onclick="switchTab(3, this)">3. Top 2 Bull / Top 2 Bear Radar + All 30 Stocks &amp; ITM Flow</button>
</div>

<div class="regime-strip">
  <div><strong>80/20 JUDGE:</strong> <span id="regimeText" class="amber">ANALYZING...</span></div>
  <div><strong>TOP DRIVERS:</strong> <span id="topMoversText" class="cyan">Loading...</span></div>
  <div><strong>3-INDEX LEAD:</strong> Nifty <span id="nImpact">0</span>p | BN <span id="bnImpact">0</span>p | Sensex <span id="sxImpact">0</span>p</div>
</div>

<div id="leadBar" class="lead-bar hidden"></div>

<div id="actionBanner" class="action-banner" style="background: linear-gradient(90deg, #064e3b, #047857);">
  <div class="action-hdr">
    <div class="action-title" id="actionTitle">INITIALIZING 80/20 INSTITUTIONAL DECISION ENGINE...</div>
    <div class="shift-tag" id="shiftTag">LOCKED @ --:--</div>
  </div>
  <div class="action-sub" id="actionSub">Waiting for tick stream...</div>
  <div class="trade-box" id="tradeCard">READY-TO-EXECUTE STRIKE BUILDER LOADING...</div>
</div>

<!-- TAB 1: INTRADAY EXECUTION & FULL 25-STRIKE OPTION CHAIN WITH OI BARS -->
<div id="tab1">
  <div class="idx-grid" id="idxCards"></div>

  <div class="section-hdr">
    <div>Top 10 Heavyweight Movers (Click Any Stock for Live Stock Option Chain &amp; Greeks)</div>
    <div id="breadthSummary" class="green">Breadth: --</div>
  </div>
  <div class="hw-strip" id="top10Strip"></div>

  <div class="section-hdr">
    <div>Microstructure Greeks, Net GEX, Gamma Flip &amp; Full ATM ±12 Option Chain (25 Strikes + Visual OI Walls)</div>
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
          <th style="text-align:right;">CALL OI WALL (ΔOI)</th><th>ACTION</th><th>CE IV%</th><th>CE Δ / Γ</th><th>CE Θ</th><th>CALL LTP</th>
          <th style="color:#fbbf24;">STRIKE</th>
          <th>PUT LTP</th><th>PE Θ</th><th>PE Δ / Γ</th><th>PE IV%</th><th>ACTION</th><th style="text-align:left;">PUT OI WALL (ΔOI)</th>
        </tr>
      </thead>
      <tbody id="chainBody"></tbody>
    </table>
  </div>

  <div class="bottom-oi-bar">
    <div><strong class="red">TOTAL CALL OI (RESISTANCE):</strong> <span id="totCeOi">0</span></div>
    <div class="pcr-track"><div id="pcrBarFill" class="pcr-fill" style="width:50%;"></div></div>
    <div><strong class="green">TOTAL PUT OI (SUPPORT):</strong> <span id="totPeOi">0</span></div>
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

<!-- TAB 3: TOP 2 BULL / TOP 2 BEAR RADAR + ALL 30 STOCKS + ITM FLOW + CLICK-ON-STOCK OPTION CHAIN -->
<div id="tab3" class="hidden">
  <div class="section-hdr" style="margin-top:0;">
    <div style="color:#fbbf24;">&#9889; INSTITUTIONAL 2%+ STOCK BREAKOUT &amp; EXIT RADAR (ANTI-FLICKER LOCKED — CLICK CARD FOR OPTION CHAIN)</div>
    <div class="cyan">Intraday Momentum + 3–15D Positional Carry</div>
  </div>
  <div class="top4-grid" id="top4Grid"></div>

  <div class="section-hdr">
    <div>All 30 Sensex + Nifty Heavyweights — 3-Index Attribution, Futures Buildup, ITM Institutional Flow &amp; Greeks</div>
    <div class="cyan">Click Any Stock Row Below &#8595;</div>
  </div>
  <div class="chain-wrap" style="max-height:280px; margin-bottom:12px;">
    <table>
      <thead>
        <tr>
          <th>STOCK</th><th>LTP</th><th>CHG %</th><th>NIFTY</th><th>BN</th><th>SENSEX</th>
          <th>FUTURES BUILDUP</th><th>ITM INSTITUTIONAL FLOW</th><th>ATM IV%</th><th>ACTIONABLE SIGNAL</th>
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
    <div class="chain-wrap" style="max-height:260px;">
      <table>
        <thead>
          <tr>
            <th style="text-align:right;">CALL OI WALL</th><th>CE IV%</th><th>CE Δ / Γ</th><th>CE Θ</th><th>CALL LTP</th>
            <th style="color:#fbbf24;">STRIKE</th>
            <th>PUT LTP</th><th>PE Θ</th><th>PE Δ / Γ</th><th>PE IV%</th><th style="text-align:left;">PUT OI WALL</th>
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
let LAST_CONFIRMED_KEY = '';
let SOUND_ON = true;

function toggleSound() {
  SOUND_ON = !SOUND_ON;
  document.getElementById('muteBtn').innerHTML = SOUND_ON ? '&#128266; CHIME ON' : '&#128263; MUTED';
}

function playChime() {
  if (!SOUND_ON) return;
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'triangle';
    osc.frequency.setValueAtTime(660, ctx.currentTime);
    osc.frequency.setValueAtTime(880, ctx.currentTime + 0.15);
    gain.gain.setValueAtTime(0.18, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.55);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.55);
  } catch (e) {}
}

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
    <div class="metric-box"><div class="metric-lbl">OI WALLS (CALL / PUT)</div><div class="metric-val">CE: ${fmtNum(ch.call_wall)} | PE: ${fmtNum(ch.put_wall)}</div></div>
  `;

  document.getElementById('totCeOi').innerText = fmtNum(ch.tot_ce_oi);
  document.getElementById('totPeOi').innerText = fmtNum(ch.tot_pe_oi);
  const totAll = (ch.tot_ce_oi || 0) + (ch.tot_pe_oi || 0);
  const peRatio = totAll > 0 ? Math.round((ch.tot_pe_oi / totAll) * 100) : 50;
  document.getElementById('pcrBarFill').style.width = peRatio + '%';

  let html = '';
  ch.rows.forEach(r => {
    const ceCls = r.ce_act === 'SHORT COVERING' ? 'b-buy' : (r.ce_act === 'CALL WRITING' ? 'b-sell' : 'b-neu');
    const peCls = r.pe_act === 'PUT WRITING' ? 'b-buy' : (r.pe_act === 'LONG UNWINDING' ? 'b-sell' : 'b-neu');
    html += `<tr class="${r.is_atm ? 'atm-row' : ''}">
      <td class="oi-cell"><div class="oi-bar-ce" style="width:${r.ce_pct}%;"></div><span class="oi-txt">${fmtNum(r.ce.oi)} <span class="${r.ce.doi>=0?'green':'red'}">(${r.ce.doi>=0?'+':''}${fmtNum(r.ce.doi)})</span></span></td>
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
      <td class="oi-cell-pe"><div class="oi-bar-pe" style="width:${r.pe_pct}%;"></div><span class="oi-txt">${fmtNum(r.pe.oi)} <span class="${r.pe.doi>=0?'green':'red'}">(${r.pe.doi>=0?'+':''}${fmtNum(r.pe.doi)})</span></span></td>
    </tr>`;
  });
  document.getElementById('chainBody').innerHTML = html;
}

function renderStockChain() {
  const sc = (STATE.stock_chains || {})[SELECTED_STOCK];
  const hw = (STATE.heavyweights || []).find(x => x.sym === SELECTED_STOCK);
  document.getElementById('stkChainTitle').innerText = `${SELECTED_STOCK} — Live Stock Option Chain & Greeks (Positional & Intraday)`;
  if (hw) {
    document.getElementById('stkChainMeta').innerText = `Spot: ₹${hw.lp} (${hw.chg_pct>=0?'+':''}${hw.chg_pct}%) | ${hw.buildup} | ${hw.itm_tag}`;
  }
  if (!sc || !sc.rows || !sc.rows.length) {
    document.getElementById('stkChainBody').innerHTML = `<tr><td colspan="11">Loading ${SELECTED_STOCK} option strikes...</td></tr>`;
    return;
  }
  let html = '';
  sc.rows.forEach(r => {
    html += `<tr class="${r.is_atm ? 'atm-row' : ''}">
      <td class="oi-cell"><div class="oi-bar-ce" style="width:${r.ce_pct}%;"></div><span class="oi-txt">${fmtNum(r.ce.oi)}</span></td>
      <td>${r.ce.iv}%</td>
      <td>${r.ce.delta} / ${r.ce.gamma}</td>
      <td class="red">${r.ce.theta}</td>
      <td class="cyan"><strong>${r.ce.lp.toFixed(2)}</strong></td>
      <td style="color:#fbbf24; font-weight:800;">${r.strike}</td>
      <td class="cyan"><strong>${r.pe.lp.toFixed(2)}</strong></td>
      <td class="red">${r.pe.theta}</td>
      <td>${r.pe.delta} / ${r.pe.gamma}</td>
      <td>${r.pe.iv}%</td>
      <td class="oi-cell-pe"><div class="oi-bar-pe" style="width:${r.pe_pct}%;"></div><span class="oi-txt">${fmtNum(r.pe.oi)}</span></td>
    </tr>`;
  });
  document.getElementById('stkChainBody').innerHTML = html;
}

function renderAll(data) {
  STATE = data;
  const d = data.decision;
  const t = data.ticks || {};

  if (LAST_CONFIRMED_KEY && LAST_CONFIRMED_KEY !== d.confirmed_key) {
    playChime();
  }
  LAST_CONFIRMED_KEY = d.confirmed_key;

  document.getElementById('modeBadge').innerText = data.micro.mode || 'LIVE';
  document.getElementById('tpsBadge').innerText = `${data.micro.ticks_per_sec || 0} ticks/s`;
  document.getElementById('regimeText').innerText = d.regime;
  document.getElementById('regimeText').style.color = d.regime_color;
  document.getElementById('topMoversText').innerText = (d.top_movers || []).join('  |  ');
  document.getElementById('nImpact').innerText = (d.nifty_pts >= 0 ? '+' : '') + d.nifty_pts;
  document.getElementById('bnImpact').innerText = (d.bn_pts >= 0 ? '+' : '') + d.bn_pts;
  document.getElementById('sxImpact').innerText = (d.sensex_pts >= 0 ? '+' : '') + d.sensex_pts;

  const banner = document.getElementById('actionBanner');
  banner.style.background = d.banner_bg;
  banner.style.borderLeftColor = d.border_c;
  if (d.is_blinking_1min) {
    banner.classList.add('blink-1min');
    document.getElementById('shiftTag').innerText = `NEW SHIFT @ ${d.shift_time_str} (BLINKING: ${d.blink_remaining}s)`;
  } else {
    banner.classList.remove('blink-1min');
    document.getElementById('shiftTag').innerText = `SIGNAL LOCKED SINCE ${d.shift_time_str}`;
  }
  document.getElementById('actionTitle').innerText = d.action;
  document.getElementById('actionSub').innerText = d.sub;
  document.getElementById('tradeCard').innerText = d.trade_card;

  const lb = document.getElementById('leadBar');
  if (d.lead_alert) {
    lb.innerText = d.lead_alert;
    lb.classList.remove('hidden');
  } else {
    lb.classList.add('hidden');
  }

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

  const hws = data.heavyweights || [];
  document.getElementById('breadthSummary').innerText = `Breadth: ${d.green_cnt}/${d.total_hws} Green | Nifty ${d.nifty_pts>=0?'+':''}${d.nifty_pts}p | BN ${d.bn_pts>=0?'+':''}${d.bn_pts}p | Sensex ${d.sensex_pts>=0?'+':''}${d.sensex_pts}p`;
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

  document.getElementById('posVerdict').innerText = d.pos_verdict;
  document.getElementById('posReason').innerText = d.pos_reason;
  document.getElementById('posGrid').innerHTML = `
    <div class="card"><div class="idx-title">NIFTY POSITIONAL (3–15D)</div><div class="metric-val cyan">Max Pain: ${data.chains.NIFTY.max_pain} | PCR: ${data.chains.NIFTY.pcr}</div><div style="margin-top:6px;">ATM IV: ${data.chains.NIFTY.atm_iv}% | F1–F2 Roll Spread: +${d.roll_spread} pts</div></div>
    <div class="card"><div class="idx-title">BANKNIFTY POSITIONAL (3–15D)</div><div class="metric-val amber">Max Pain: ${data.chains.BN.max_pain} | PCR: ${data.chains.BN.pcr}</div><div style="margin-top:6px;">ATM IV: ${data.chains.BN.atm_iv}% | Net GEX: ${data.chains.BN.net_gex_cr} Cr</div></div>
    <div class="card"><div class="idx-title">SENSEX POSITIONAL (3–15D)</div><div class="metric-val green">Max Pain: ${data.chains.SX.max_pain} | PCR: ${data.chains.SX.pcr}</div><div style="margin-top:6px;">ATM IV: ${data.chains.SX.atm_iv}% | Net GEX: ${data.chains.SX.net_gex_cr} Cr</div></div>
  `;

  let top4Html = '';
  (d.top2_bull || []).forEach((b, idx) => {
    top4Html += `<div class="bull-card" onclick="selectStock('${b.sym}')">
      <div style="display:flex; justify-content:space-between; font-weight:800; font-size:13px;">
        <span class="green">&#9650; TOP BULL #${idx+1}: ${b.sym} (₹${b.lp} | ${b.chg_pct>=0?'+':''}${b.chg_pct}%)</span>
        <span class="badge b-buy">${b.itm_tag}</span>
      </div>
      <div style="font-size:11.5px; color:#cbd5e1; margin-top:4px;"><strong>Horizon:</strong> ${b.horizon} | <strong>Futures:</strong> ${b.buildup}</div>
      <div style="font-size:12px; color:#fef08a; font-weight:800; margin-top:4px;">TRADE: ${b.stk_signal}</div>
      <div style="font-size:11px; color:#94a3b8; margin-top:3px;">${b.exit_rule}</div>
    </div>`;
  });
  (d.top2_bear || []).forEach((b, idx) => {
    top4Html += `<div class="bear-card" onclick="selectStock('${b.sym}')">
      <div style="display:flex; justify-content:space-between; font-weight:800; font-size:13px;">
        <span class="red">&#9660; TOP BEAR #${idx+1}: ${b.sym} (₹${b.lp} | ${b.chg_pct}%)</span>
        <span class="badge b-sell">${b.itm_tag}</span>
      </div>
      <div style="font-size:11.5px; color:#cbd5e1; margin-top:4px;"><strong>Horizon:</strong> ${b.horizon} | <strong>Futures:</strong> ${b.buildup}</div>
      <div style="font-size:12px; color:#fef08a; font-weight:800; margin-top:4px;">TRADE: ${b.stk_signal}</div>
      <div style="font-size:11px; color:#94a3b8; margin-top:3px;">${b.exit_rule}</div>
    </div>`;
  });
  document.getElementById('top4Grid').innerHTML = top4Html;

  let t3Html = '';
  hws.forEach(h => {
    const c = h.chg_pct >= 0 ? 'green' : 'red';
    t3Html += `<tr style="cursor:pointer;" onclick="selectStock('${h.sym}')">
      <td style="font-weight:800; color:#f8fafc;">${h.sym}</td>
      <td>₹${h.lp}</td>
      <td class="${c}"><strong>${h.chg_pct>=0?'+':''}${h.chg_pct}%</strong></td>
      <td class="${h.nifty_pts>=0?'green':'red'}">${h.nifty_pts>=0?'+':''}${h.nifty_pts}</td>
      <td class="${h.bn_pts>=0?'green':'red'}">${h.bn_pts>=0?'+':''}${h.bn_pts}</td>
      <td class="${h.sensex_pts>=0?'green':'red'}">${h.sensex_pts>=0?'+':''}${h.sensex_pts}</td>
      <td class="cyan">${h.buildup}</td>
      <td class="amber" style="font-weight:700;">${h.itm_tag}</td>
      <td>${h.atm_iv}%</td>
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
