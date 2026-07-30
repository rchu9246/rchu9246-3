#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
紙上交易模擬腳本
=====================================
用途：讀取 signal_scores 表裡的「強多候選」訊號，模擬進出場（完全不連線券商、
      不動用真錢），驗證訊號分數的實際交易表現，決定要不要接永豐 Shioaji 真實下單。

策略規則（已跟使用者確認）：
  進場：只買「強多候選」(recommendation == 'strong-bull')
  出場：停利 +5% / 停損 -3%
  部位：資金平均分配給當天所有入選股票，允許零股（不強制整張1000股，
       因為10萬元本金分配給多檔候選時，常常不夠買一整張）
  起始資金：100,000 元（純模擬數字）

執行時機與順序：這支腳本要排在 fetch_and_score.py 之後、同一次排程裡執行，
  因為它需要用到當天剛抓到的開盤價（STOCK_DAY_ALL 的 OpeningPrice，已經存進
  stock_daily 的 open 欄位）跟剛算好的 signal_scores，不能等到隔天才讀。

決策時序（避免用到未來資料，跟 backtest_stats 用同一套邏輯）：
  T 日收盤後：用 T 日的 signal_scores/收盤價，決定「T+1 要做什麼」
  T+1 日開盤：執行「T 日決定」的動作，用 T+1 的真實開盤價成交
  也就是說，這支腳本每次執行時，會先「執行昨天決定的動作」（用今天開盤價），
  再「用今天的資料做出新決定」（留給明天執行），兩個階段分開、不會互相污染。

⚠️ 這是紙上模擬，不會真的連線券商、不會動用真錢。要接永豐 Shioaji 下真實單，
   要等這裡跑出的績效經得起檢驗，且通過永豐要求的模擬環境測試審核之後再說。
   這裡的規則（停利5%/停損3%、只買strong-bull）是簡化版策略，不構成投資建議，
   純粹是驗證訊號系統實際交易表現用的框架。
"""

import os
import json
import urllib.request
import urllib.error
from datetime import date, datetime, timezone

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

TODAY = date.today().isoformat()

TAKE_PROFIT_PCT = 5.0        # 停利門檻（未實現損益 >= 這個值就標記出場）
STOP_LOSS_PCT = -3.0         # 停損門檻（未實現損益 <= 這個值就標記出場）
INITIAL_CAPITAL = 100000.0   # 起始資金，純模擬數字，不是真錢


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


def supabase_insert(table, rows):
    """純新增（不 upsert），用於 append-only 的交易紀錄表"""
    if not rows:
        return
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    body = json.dumps(rows).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
        print(f"[OK] 寫入 {table}：{len(rows)} 筆")
    except urllib.error.HTTPError as e:
        print(f"[ERROR] 寫入 {table} 失敗：{e.code} {e.read().decode('utf-8')[:500]}")


def supabase_delete(table, query):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    url = f"{SUPABASE_URL}/rest/v1/{table}?{query}"
    req = urllib.request.Request(url, method="DELETE", headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        print(f"[WARN] 刪除 {table} 失敗：{e.code} {e.read().decode('utf-8')[:300]}")


def get_account():
    """讀取模擬帳戶現況，第一次執行時如果沒有資料就用起始資金初始化"""
    rows = supabase_select("paper_account", "id=eq.1&limit=1")
    if rows:
        return rows[0]
    account = {
        "id": 1, "cash": INITIAL_CAPITAL, "initial_capital": INITIAL_CAPITAL,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    supabase_upsert("paper_account", [account], "id")
    print(f"[INIT] 第一次執行，用起始資金 {INITIAL_CAPITAL:,.0f} 元初始化模擬帳戶")
    return account


def get_positions():
    return supabase_select("paper_positions", "status=in.(open,pending_exit)&order=entry_date.asc")


def get_pending_buys():
    return supabase_select("paper_pending_buys", "order=trade_date.asc")


def fetch_prices(codes):
    """從 stock_daily 撈今天的開盤價/收盤價（fetch_and_score.py 這次執行應該已經先寫入了）。
    開盤價用來執行「昨天決定」的動作，收盤價用來評估「今天要不要標記明天出場/進場」。
    """
    if not codes:
        return {}
    rows = supabase_select("stock_daily", f"select=code,open,close&trade_date=eq.{TODAY}")
    return {
        r["code"]: {"open": safe_float(r.get("open")), "close": safe_float(r.get("close"))}
        for r in rows if r.get("code") in codes
    }


def fetch_latest_strong_bull():
    """撈今天剛算好的 signal_scores 裡，recommendation='strong-bull' 的股票清單"""
    return supabase_select(
        "signal_scores",
        f"select=code,name,close&trade_date=eq.{TODAY}&recommendation=eq.strong-bull",
    )


def main():
    print(f"=== 開始執行 {TODAY} 紙上交易模擬 ===")
    account = get_account()
    cash = safe_float(account.get("cash"), INITIAL_CAPITAL)
    print(f"目前現金：{cash:,.0f} 元")

    positions = get_positions()
    pending_buys = get_pending_buys()
    print(f"目前持有部位：{len(positions)} 檔；待買清單（昨天決定的）：{len(pending_buys)} 檔")

    all_codes = set(p["code"] for p in positions) | set(b["code"] for b in pending_buys)
    prices = fetch_prices(all_codes)

    trade_log = []

    # ------------------------------------------------------------------
    # STEP 1：執行「昨天決定」的動作，用今天的開盤價成交
    # ------------------------------------------------------------------
    print("--- STEP 1：執行昨天決定的動作（用今天開盤價）---")

    # 1a. 賣出被標記 pending_exit 的部位
    to_close = [p for p in positions if p["status"] == "pending_exit"]
    for p in to_close:
        price_info = prices.get(p["code"])
        if not price_info or not price_info["open"]:
            print(f"[WARN] {p['code']} 沒有今天的開盤價，這次跳過賣出（下次執行再試）")
            continue
        exit_price = price_info["open"]
        quantity = safe_float(p["quantity"])
        entry_price = safe_float(p["entry_price"])
        amount = exit_price * quantity
        pnl_pct = round((exit_price - entry_price) / entry_price * 100, 2) if entry_price else 0
        cash += amount
        trade_log.append({
            "trade_date": TODAY, "code": p["code"], "name": p.get("name"),
            "action": "sell", "price": exit_price, "quantity": quantity, "amount": round(amount, 0),
            "reason": f"停利/停損出場，損益 {pnl_pct:+.2f}%",
        })
        supabase_upsert("paper_positions", [{
            "code": p["code"], "entry_date": p["entry_date"], "name": p.get("name"),
            "entry_price": entry_price, "quantity": quantity, "status": "closed",
            "exit_date": TODAY, "exit_price": exit_price, "pnl_pct": pnl_pct,
        }], "code,entry_date")
        print(f"[SELL] {p['code']} {p.get('name')}：{quantity:.0f}股 @ {exit_price}，損益 {pnl_pct:+.2f}%")

    # 1b. 買進昨天標記的候選，資金平均分配給待買清單
    # 允許零股（不強制整張1000股）：10萬元本金分配給多檔候選時，常常不夠買一整張，
    # 用零股方式才能實際分散到多檔股票。真實下單零股有交易時段限制（盤中/盤後零股），
    # 跟整張不是同一個交易機制，這裡是簡化模擬，先不模擬這個交易機制上的差異。
    executed_buys = []  # 記錄這次真的成功買進的 (code, trade_date)，只有這些才會從待買清單刪除
    if pending_buys:
        capital_per_stock = cash / len(pending_buys)
        for b in pending_buys:
            price_info = prices.get(b["code"])
            if not price_info or not price_info["open"]:
                print(f"[WARN] {b['code']} 沒有今天的開盤價，這次跳過買進（留在待買清單，下次執行再試）")
                continue
            entry_price = price_info["open"]
            quantity = int(capital_per_stock // entry_price)  # 允許零股，最小單位1股
            if quantity <= 0:
                print(f"[WARN] {b['code']} 分配資金 {capital_per_stock:.0f} 元不夠買 1 股"
                      f"（{entry_price} 元/股），跳過（留在待買清單，下次執行再試）")
                continue
            amount = entry_price * quantity
            cash -= amount
            trade_log.append({
                "trade_date": TODAY, "code": b["code"], "name": b.get("name"),
                "action": "buy", "price": entry_price, "quantity": quantity, "amount": round(amount, 0),
                "reason": f"強多候選訊號（{b['trade_date']}）進場",
            })
            supabase_upsert("paper_positions", [{
                "code": b["code"], "entry_date": TODAY, "name": b.get("name"),
                "entry_price": entry_price, "quantity": quantity, "status": "open",
                "exit_date": None, "exit_price": None, "pnl_pct": None,
            }], "code,entry_date")
            print(f"[BUY] {b['code']} {b.get('name')}：{quantity:.0f}股 @ {entry_price}")
            executed_buys.append((b["code"], b["trade_date"]))

        # 只刪除「這次真的成功買進」的那幾筆，抓不到開盤價、或資金不夠買的那些
        # 繼續留在 paper_pending_buys 裡，下次執行時會自動再嘗試一次，不會被整批
        # 連坐刪除、白白遺失一個原本有效的訊號
        for code, trade_date_str in executed_buys:
            supabase_delete("paper_pending_buys", f"code=eq.{code}&trade_date=eq.{trade_date_str}")

    if trade_log:
        supabase_insert("paper_trade_log", trade_log)
    else:
        print("今天沒有任何買賣動作")

    # ------------------------------------------------------------------
    # STEP 2：用今天的資料，決定「明天要做的動作」
    # ------------------------------------------------------------------
    print("--- STEP 2：用今天資料決定明天動作 ---")
    positions_now = get_positions()
    open_codes = [p["code"] for p in positions_now if p["status"] == "open"]
    close_prices = fetch_prices(open_codes)

    for p in positions_now:
        if p["status"] != "open":
            continue
        price_info = close_prices.get(p["code"])
        if not price_info or not price_info["close"]:
            continue
        entry_price = safe_float(p["entry_price"])
        if not entry_price:
            continue
        unrealized_pct = round((price_info["close"] - entry_price) / entry_price * 100, 2)
        if unrealized_pct >= TAKE_PROFIT_PCT or unrealized_pct <= STOP_LOSS_PCT:
            supabase_upsert("paper_positions", [{
                "code": p["code"], "entry_date": p["entry_date"], "name": p.get("name"),
                "entry_price": entry_price, "quantity": p["quantity"], "status": "pending_exit",
                "exit_date": None, "exit_price": None, "pnl_pct": None,
            }], "code,entry_date")
            reason = "停利" if unrealized_pct >= TAKE_PROFIT_PCT else "停損"
            print(f"[FLAG-{reason}] {p['code']} 未實現損益 {unrealized_pct:+.2f}%，標記明天開盤出場")

    held_codes = set(p["code"] for p in positions_now if p["status"] in ("open", "pending_exit"))
    strong_bull_today = fetch_latest_strong_bull()
    new_buys = [r for r in strong_bull_today if r["code"] not in held_codes]
    if new_buys:
        supabase_upsert("paper_pending_buys", [
            {"code": r["code"], "trade_date": TODAY, "name": r.get("name")} for r in new_buys
        ], "code,trade_date")
        print(f"[FLAG-BUY] 今天新增 {len(new_buys)} 檔強多候選，標記明天開盤買進")
    else:
        print("今天沒有新的強多候選（或都已經持有）")

    # ------------------------------------------------------------------
    # STEP 3：記錄今天的權益快照
    # ------------------------------------------------------------------
    print("--- STEP 3：記錄權益快照 ---")
    final_positions = get_positions()
    holdings_value = 0.0
    for p in final_positions:
        price_info = close_prices.get(p["code"]) or prices.get(p["code"])
        px = price_info["close"] if price_info and price_info.get("close") else safe_float(p["entry_price"])
        holdings_value += px * safe_float(p["quantity"])

    total_equity = cash + holdings_value
    cumulative_return_pct = round((total_equity - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 2)

    supabase_upsert("paper_account", [{
        "id": 1, "cash": round(cash, 0), "initial_capital": INITIAL_CAPITAL,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }], "id")

    supabase_upsert("paper_daily_equity", [{
        "trade_date": TODAY, "cash": round(cash, 0), "holdings_value": round(holdings_value, 0),
        "total_equity": round(total_equity, 0), "num_positions": len(final_positions),
        "cumulative_return_pct": cumulative_return_pct,
    }], "trade_date")

    print(f"今日權益：現金 {cash:,.0f} + 持股市值 {holdings_value:,.0f} = 總權益 {total_equity:,.0f}")
    print(f"累積報酬率：{cumulative_return_pct:+.2f}%（相對起始資金 {INITIAL_CAPITAL:,.0f} 元）")
    print("=== 完成 ===")


if __name__ == "__main__":
    main()
