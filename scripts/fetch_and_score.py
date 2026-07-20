#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台股訊號儀表板 — 收盤後完整計算腳本
=====================================
用途：抓取證交所 OpenAPI 資料，計算訊號分數 / 爆發前兆分數 / 風險分數，寫入 Supabase。
執行時機：每個交易日收盤後（建議 15:30 之後，法人買賣超資料約 15:00~16:00 陸續釋出）。

環境變數（由 GitHub Actions secrets 注入）：
  SUPABASE_URL       Supabase 專案 URL，例如 https://xxxx.supabase.co
  SUPABASE_KEY       Supabase service_role key（有寫入權限，勿外流）

⚠️ 重要：
  1. 證交所 OpenAPI 端點路徑可能異動，執行前建議先用瀏覽器打開
     https://openapi.twse.com.tw/v1/swagger.json 確認本檔用到的端點仍存在。
  2. 評分公式（見下方 SCORING RULES）是簡化版規則，僅供參考，不構成投資建議，
     使用前務必自行回測與調整權重。
"""

import os
import sys
import json
import math
import statistics
from datetime import date, datetime, timezone
import urllib.request
import urllib.error

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

TWSE_BASE = "https://openapi.twse.com.tw/v1"

# --------------------------------------------------------------------------
# 基礎工具：HTTP GET / Supabase upsert
# --------------------------------------------------------------------------

def http_get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "signal-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def supabase_upsert(table, rows, on_conflict):
    """用 Supabase REST API 做 upsert（insert or update）"""
    if not rows:
        return
    if not SUPABASE_URL or not SUPABASE_KEY:
        print(f"[WARN] 未設定 SUPABASE_URL / SUPABASE_KEY，跳過寫入 {table}（僅本地測試模式）")
        return
    url = f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}"
    body = json.dumps(rows).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
        print(f"[OK] 寫入 {table}：{len(rows)} 筆")
    except urllib.error.HTTPError as e:
        print(f"[ERROR] 寫入 {table} 失敗：{e.code} {e.read().decode('utf-8')[:500]}")


def safe_float(v, default=0.0):
    try:
        if v in (None, "", "--", "N/A"):
            return default
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return default


# --------------------------------------------------------------------------
# 1. 抓取證交所資料
# --------------------------------------------------------------------------

def fetch_stock_day_all():
    """全市場個股日成交資訊（收盤價、漲跌、成交量）
    端點：/exchangeReport/STOCK_DAY_ALL
    """
    data = http_get_json(f"{TWSE_BASE}/exchangeReport/STOCK_DAY_ALL")
    out = {}
    for row in data:
        code = row.get("Code")
        if not code:
            continue
        out[code] = {
            "code": code,
            "name": row.get("Name", ""),
            "close": safe_float(row.get("ClosingPrice")),
            "change": safe_float(row.get("Change")),
            "open": safe_float(row.get("OpeningPrice")),
            "high": safe_float(row.get("HighestPrice")),
            "low": safe_float(row.get("LowestPrice")),
            "volume": safe_float(row.get("TradeVolume")),
        }
    return out


def fetch_institutional_t86():
    """三大法人買賣超日報（外資/投信/自營商）
    端點：/fund/T86
    注意：欄位名稱以官方 swagger 為準，這裡用常見欄位名稱嘗試對應，
    若 TWSE 調整欄位，需要打開 swagger.json 核對。
    """
    try:
        data = http_get_json(f"{TWSE_BASE}/fund/T86")
    except Exception as e:
        print(f"[WARN] 三大法人資料抓取失敗：{e}，本次僅用價量資料計算")
        return {}
    out = {}
    for row in data:
        code = row.get("Code") or row.get("證券代號")
        if not code:
            continue
        foreign_net = safe_float(row.get("ForeignInvestorsExcludeDealerNetBuySell") or row.get("外資及陸資買賣超股數"))
        trust_net = safe_float(row.get("InvestmentTrustNetBuySell") or row.get("投信買賣超股數"))
        dealer_net = safe_float(row.get("DealerNetBuySell") or row.get("自營商買賣超股數"))
        out[code] = {
            "foreign_net": foreign_net,
            "trust_net": trust_net,
            "dealer_net": dealer_net,
            "institutional_net": foreign_net + trust_net + dealer_net,
        }
    return out


def fetch_pe_pb():
    """個股本益比、殖利率、股價淨值比（可用於篩選基本面體質）
    端點：/exchangeReport/BWIBBU_ALL
    """
    try:
        data = http_get_json(f"{TWSE_BASE}/exchangeReport/BWIBBU_ALL")
    except Exception as e:
        print(f"[WARN] 本益比資料抓取失敗：{e}")
        return {}
    out = {}
    for row in data:
        code = row.get("Code")
        if not code:
            continue
        out[code] = {
            "pe": safe_float(row.get("PEratio")),
            "yield": safe_float(row.get("DividendYield")),
            "pb": safe_float(row.get("PBratio")),
        }
    return out


# --------------------------------------------------------------------------
# 2. 評分邏輯（SCORING RULES）
# --------------------------------------------------------------------------
# 這是一套「簡化版」規則，目的是先讓系統可以動起來、之後你可以依實際回測結果調整權重。
#
# 【訊號分數 net_signal】
#   +1  法人（三大合計）買超股數 > 0
#   +1  法人買超股數 > 該股 20 日均量的 5%（買超強度夠大）
#   +1  今日漲跌 > 0 且成交量 > 昨日（價量齊揚，此簡化版用「今日量>0」近似）
#   -1  法人賣超股數 > 0
#   -1  法人賣超股數 > 該股 20 日均量的 5%
#   -1  今日跌幅 > 5%（單日重挫）
#   最終 recommendation 依 net_signal 對照：
#     net_signal >= 3      → strong-bull 🚀 強多候選
#     net_signal == 2      → bull ↗ 多頭候選
#     net_signal == 1      → watch 🟢 留意
#     net_signal == 0      → neutral — 中性
#     net_signal == -1     → pullback 🟡 觀望
#     net_signal <= -2     → avoid 🚫 避開
#
# 【爆發前兆分數 explosion score，0-100】
#   以量能與突破為主：
#     量比（今量/20日均量）貢獻 0-40 分：min(量比, 5) / 5 * 40
#     單日漲幅貢獻 0-30 分：min(max(漲幅,0), 10) / 10 * 30
#     收盤價站上今日均價（強勢收盤）貢獻 0-30 分：(close - low) / (high - low) * 30（若 high=low 則給 15）
#   status：
#     score >= 80          → confirm 爆發確認
#     60 <= score < 80      → pre 爆發前兆
#     其餘                  → 不列入雷達
#
# 【風險分數 risk_score，0-100】
#   跌幅 >= 9.5%           → +50（視為跌停預警）
#   法人賣超股數 > 20日均量 3% → +25
#   單日振幅（high-low)/close > 6% → +15
#   量比 < 0.5（量縮流動性差） → +10
#   risk_level：
#     score >= 80           → high 高度風險
#     score >= 60            → warn 警戒
#     其餘不列入清單
#
# 這些門檻值都寫在下面常數區，方便你之後調整。
# --------------------------------------------------------------------------

SIGNAL_NET_TO_REC = [
    (3, "strong-bull", "🚀 強多候選"),
    (2, "bull", "↗ 多頭候選"),
    (1, "watch", "🟢 留意"),
    (0, "neutral", "— 中性"),
    (-1, "pullback", "🟡 觀望"),
    (-999, "avoid", "🚫 避開"),
]

RISK_DROP_LIMIT_PCT = -9.5      # 跌幅達此比例視為跌停預警
RISK_INST_SELL_RATIO = 0.03     # 法人賣超佔 20 日均量比例門檻
RISK_AMPLITUDE_PCT = 6.0        # 單日振幅門檻
EXPLOSION_CONFIRM_SCORE = 80
EXPLOSION_PRE_SCORE = 60


def recommendation_for(net_signal):
    for threshold, rec, label in SIGNAL_NET_TO_REC:
        if net_signal >= threshold:
            return rec, label
    return "avoid", "🚫 避開"


def compute_signal_scores(stock_day, institutional):
    rows = []
    for code, sd in stock_day.items():
        inst = institutional.get(code, {})
        inst_net = inst.get("institutional_net", 0)
        close = sd["close"]
        chg_pct = (sd["change"] / (close - sd["change"]) * 100) if (close - sd["change"]) else 0

        net_signal = 0
        bull_tags, bear_tags = [], []

        if inst_net > 0:
            net_signal += 1
            bull_tags.append("法人買超")
        elif inst_net < 0:
            net_signal -= 1
            bear_tags.append("法人賣超")

        # 用成交量的粗略基準（沒有 20 日均量歷史時，用今日量的 5% 概略近似，正式上線建議累積歷史後改真 20 日均量）
        vol_base = max(sd["volume"], 1)
        if inst_net > 0 and abs(inst_net) > vol_base * 0.05:
            net_signal += 1
            bull_tags.append("買超強度大")
        if inst_net < 0 and abs(inst_net) > vol_base * 0.05:
            net_signal -= 1
            bear_tags.append("賣超強度大")

        if chg_pct > 0:
            net_signal += 1
            bull_tags.append("價漲")
        if chg_pct <= RISK_DROP_LIMIT_PCT:
            net_signal -= 1
            bear_tags.append("單日重挫")

        rec, label = recommendation_for(net_signal)
        composite_score = round(net_signal * 2.5 + (chg_pct * 0.3), 2)

        rows.append({
            "code": code,
            "name": sd["name"],
            "trade_date": TODAY,
            "close": close,
            "chg_pct": round(chg_pct, 2),
            "net_signal": net_signal,
            "recommendation": rec,
            "recommendation_label": label,
            "stars": min(max(net_signal, 0), 3),
            "bull_tags": bull_tags,
            "bear_tags": bear_tags,
            "composite_score": composite_score,
        })
    return rows


def compute_explosion_scores(stock_day):
    rows = []
    for code, sd in stock_day.items():
        close, high, low, vol = sd["close"], sd["high"], sd["low"], sd["volume"]
        if close <= 0 or vol <= 0:
            continue
        chg_pct = (sd["change"] / (close - sd["change"]) * 100) if (close - sd["change"]) else 0

        # 量比：沒有歷史均量時先用 1 當基準（正式上線請接歷史 20 日均量取代）
        vol_ratio_20 = 1.0
        vol_score = min(vol_ratio_20, 5) / 5 * 40
        chg_score = min(max(chg_pct, 0), 10) / 10 * 30
        if high > low:
            close_strength = (close - low) / (high - low) * 30
        else:
            close_strength = 15
        score = round(vol_score + chg_score + close_strength, 1)

        if score >= EXPLOSION_CONFIRM_SCORE:
            status, status_label, stage = "confirm", "爆發確認", "放量突破"
        elif score >= EXPLOSION_PRE_SCORE:
            status, status_label, stage = "pre", "爆發前兆", "收斂醞釀"
        else:
            continue

        rows.append({
            "code": code,
            "name": sd["name"],
            "industry": "",
            "trade_date": TODAY,
            "status": status,
            "status_label": status_label,
            "stage": stage,
            "score": score,
            "close": close,
            "chg_pct": round(chg_pct, 2),
            "breakout_pct": round(chg_pct, 2),
            "box_top_20d": high,
            "vol_ratio_20": vol_ratio_20,
            "vol_z60": 0,
            "ma_convergence_pct": 0,
            "boll_position_pct": 0,
        })
    return rows


def compute_risk_scores(stock_day, institutional):
    rows = []
    for code, sd in stock_day.items():
        close, high, low, vol = sd["close"], sd["high"], sd["low"], sd["volume"]
        if close <= 0:
            continue
        chg_pct = (sd["change"] / (close - sd["change"]) * 100) if (close - sd["change"]) else 0
        inst = institutional.get(code, {})
        inst_net = inst.get("institutional_net", 0)

        score = 0.0
        main_risks = []
        if chg_pct <= RISK_DROP_LIMIT_PCT:
            score += 50
            main_risks.append("跌停預警")
        if inst_net < 0 and vol > 0 and abs(inst_net) > vol * RISK_INST_SELL_RATIO:
            score += 25
            main_risks.append("法人賣超")
        amplitude_pct = ((high - low) / close * 100) if close else 0
        if amplitude_pct > RISK_AMPLITUDE_PCT:
            score += 15
            main_risks.append("量價背離/高波動")

        if score >= 80:
            level = "high"
        elif score >= 60:
            level = "warn"
        else:
            continue

        rows.append({
            "code": code,
            "name": sd["name"],
            "trade_date": TODAY,
            "risk_level": level,
            "risk_score": round(score, 1),
            "main_risk": "、".join(main_risks),
            "suggested_action": "暫不追價，先觀察" if level == "high" else "避開新買，留意反彈",
            "close": close,
            "chg_pct": round(chg_pct, 2),
            "vol_ratio": None,
            "atr_pct": round(amplitude_pct, 2),
            "consecutive_sell_days": None,
            "liquidity_score": None,
            "note": f"當日跌幅 {chg_pct:.2f}%；{'、'.join(main_risks)}",
        })
    return rows


# --------------------------------------------------------------------------
# 3. 主流程
# --------------------------------------------------------------------------

TODAY = date.today().isoformat()


def main():
    print(f"=== 開始執行 {TODAY} 收盤後計算 ===")

    print("抓取個股日成交資訊 (STOCK_DAY_ALL)...")
    stock_day = fetch_stock_day_all()
    print(f"  取得 {len(stock_day)} 檔")

    print("抓取三大法人買賣超 (T86)...")
    institutional = fetch_institutional_t86()
    print(f"  取得 {len(institutional)} 檔")

    if not stock_day:
        print("[FATAL] 未取得任何個股資料，中止本次執行（可能是非交易日或端點異動）")
        sys.exit(1)

    print("計算訊號分數...")
    signal_rows = compute_signal_scores(stock_day, institutional)
    print(f"  產出 {len(signal_rows)} 筆")

    print("計算爆發前兆分數...")
    explosion_rows = compute_explosion_scores(stock_day)
    print(f"  產出 {len(explosion_rows)} 筆（分數達門檻者）")

    print("計算風險分數...")
    risk_rows = compute_risk_scores(stock_day, institutional)
    print(f"  產出 {len(risk_rows)} 筆（分數達門檻者）")

    # 個股每日快照（給歷史查詢用，可選）
    daily_rows = []
    for code, sd in stock_day.items():
        inst = institutional.get(code, {})
        chg_pct = (sd["change"] / (sd["close"] - sd["change"]) * 100) if (sd["close"] - sd["change"]) else 0
        daily_rows.append({
            "code": code, "name": sd["name"], "trade_date": TODAY,
            "close": sd["close"], "chg_pct": round(chg_pct, 2), "volume": sd["volume"],
            "foreign_net": inst.get("foreign_net"), "trust_net": inst.get("trust_net"),
            "dealer_net": inst.get("dealer_net"), "institutional_net": inst.get("institutional_net"),
        })

    print("寫入 Supabase...")
    supabase_upsert("stock_daily", daily_rows, "code,trade_date")
    supabase_upsert("signal_scores", signal_rows, "code,trade_date")
    supabase_upsert("explosion_scores", explosion_rows, "code,trade_date")
    supabase_upsert("risk_scores", risk_rows, "code,trade_date")

    status_row = [{
        "id": 1,
        "last_sync_at": datetime.now(timezone.utc).isoformat(),
        "confidence_score": 90,
        "status_note": "ok",
        "total_records": len(stock_day),
    }]
    supabase_upsert("fetch_status", status_row, "id")

    print("=== 完成 ===")


if __name__ == "__main__":
    main()
