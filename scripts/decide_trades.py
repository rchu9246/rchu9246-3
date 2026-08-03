#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
收盤後交易決策腳本（decide_trades.py）
=====================================
用途：收盤後用今天的資料，決定「明天開盤要做什麼」——寫進 Supabase 的
      paper_pending_buys（待買清單）跟把 paper_positions 標記 pending_exit
      （待出場），不實際下單。真正的下單動作交給隔天早上執行的
      scripts/shioaji_execute.py 處理（那時候市場才真的開著）。

這支腳本取代了原本 paper_trade.py 的 STEP2 部分。原本 paper_trade.py 的
STEP1（用資料庫模擬「假裝」成交）已經不再需要——現在改成真的透過永豐 Shioaji
在隔天開盤時送出委託單，所以「決定」跟「執行」拆成兩支腳本、兩個排程：
  下午 3:40（這支腳本）：用今天收盤價，決定「多頭候選/停利停損」名單
  隔天早上（shioaji_execute.py）：讀這份名單，在市場真的開盤時送出真實委託

策略規則（已跟使用者確認）：
  進場：只買「強多候選」(recommendation == 'strong-bull')
  出場：停利 +5% / 停損 -3%
  部位：資金平均分配給當天所有入選股票，允許零股

執行時機：要排在 fetch_and_score.py 之後、同一次排程裡執行，因為需要用到
  當天剛算好的 signal_scores 跟收盤價。
"""

import os
import json
import urllib.request
import urllib.error
from datetime import date

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

TODAY = date.today().isoformat()

TAKE_PROFIT_PCT = 5.0
STOP_LOSS_PCT = -3.0


def safe_float(v, default=0.0):
    try:
        if v in (None, "", "--", "N/A"):
            return default
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return default


def supabase_select(table, query, timeout=60, page_size=1000, max_pages=100):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    url = f"{SUPABASE_URL}/rest/v1/{table}?{query}"
    all_rows = []
    for page in range(max_pages):
        start = page * page_size
        end = start + page_size - 1
        req = urllib.request.Request(url, headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Range-Unit": "items",
            "Range": f"{start}-{end}",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                chunk = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"[WARN] 讀取 {table} 失敗：{e}")
            break
        all_rows.extend(chunk)
        if len(chunk) < page_size:
            break
    return all_rows


def supabase_upsert(table, rows, on_conflict):
    if not rows:
        return
    if not SUPABASE_URL or not SUPABASE_KEY:
        print(f"[WARN] 未設定 SUPABASE_URL/SUPABASE_KEY，跳過寫入 {table}")
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


def get_account():
    rows = supabase_select("paper_account", "id=eq.1&limit=1")
    return rows[0] if rows else None


def get_positions():
    return supabase_select("paper_positions", "status=in.(open,pending_exit)&order=entry_date.asc")


def get_pending_buys():
    return supabase_select("paper_pending_buys", "order=trade_date.asc")


def fetch_today_close(codes):
    if not codes:
        return {}
    code_filter = ",".join(codes)
    rows = supabase_select("stock_daily", f"code=in.({code_filter})&trade_date=eq.{TODAY}")
    return {r["code"]: safe_float(r.get("close")) for r in rows}


def fetch_latest_strong_bull():
    return supabase_select(
        "signal_scores",
        f"select=code,name,close&trade_date=eq.{TODAY}&recommendation=eq.strong-bull",
    )


def main():
    print(f"=== 開始執行 {TODAY} 收盤後交易決策 ===")
    account = get_account()
    if account is None:
        print("[WARN] 找不到 paper_account 紀錄，這代表還沒有任何一次真實下單初始化過帳戶，"
              "本次先只做決策，不記錄權益快照")

    positions = get_positions()
    print(f"目前持有部位：{len([p for p in positions if p['status']=='open'])} 檔"
          f"；待出場：{len([p for p in positions if p['status']=='pending_exit'])} 檔")

    open_codes = [p["code"] for p in positions if p["status"] == "open"]
    close_prices = fetch_today_close(open_codes)

    # 檢查現有部位是否觸發停利/停損（用今天收盤價評估，明天開盤才會真的送單出場）
    flagged_exit = 0
    for p in positions:
        if p["status"] != "open":
            continue
        close_price = close_prices.get(p["code"])
        if not close_price:
            continue
        entry_price = safe_float(p["entry_price"])
        if not entry_price:
            continue
        unrealized_pct = round((close_price - entry_price) / entry_price * 100, 2)
        if unrealized_pct >= TAKE_PROFIT_PCT or unrealized_pct <= STOP_LOSS_PCT:
            supabase_upsert("paper_positions", [{
                "code": p["code"], "entry_date": p["entry_date"], "name": p.get("name"),
                "entry_price": entry_price, "quantity": p["quantity"], "status": "pending_exit",
                "exit_date": None, "exit_price": None, "pnl_pct": None,
            }], "code,entry_date")
            reason = "停利" if unrealized_pct >= TAKE_PROFIT_PCT else "停損"
            print(f"[FLAG-{reason}] {p['code']} 未實現損益 {unrealized_pct:+.2f}%，"
                  f"標記明天開盤送出真實賣單")
            flagged_exit += 1
    if flagged_exit == 0:
        print("今天沒有部位觸發停利/停損")

    # 排除已持有跟已經在待買清單裡的股票，避免重複加入（同一支股票被連續標記兩次
    # 候選，會導致隔天早上被當成兩筆獨立買單處理）
    held_codes = set(p["code"] for p in positions if p["status"] in ("open", "pending_exit"))
    still_pending_codes = set(b["code"] for b in get_pending_buys())
    excluded_codes = held_codes | still_pending_codes
    strong_bull_today = fetch_latest_strong_bull()
    new_buys = [r for r in strong_bull_today if r["code"] not in excluded_codes]
    if new_buys:
        supabase_upsert("paper_pending_buys", [
            {"code": r["code"], "trade_date": TODAY, "name": r.get("name")} for r in new_buys
        ], "code,trade_date")
        print(f"[FLAG-BUY] 今天新增 {len(new_buys)} 檔強多候選，標記明天開盤送出真實買單")
    else:
        print("今天沒有新的強多候選（或都已經持有/已在待買清單中）")

    print("=== 完成 ===")


if __name__ == "__main__":
    main()
