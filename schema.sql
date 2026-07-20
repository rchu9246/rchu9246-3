-- =====================================================
-- 台股訊號儀表板 Supabase Schema
-- 在 Supabase 專案的 SQL Editor 貼上整段執行即可建表
-- =====================================================

-- 1. 個股每日快照（收盤價、法人買賣超、基本面標記）
create table if not exists stock_daily (
  code text not null,
  name text,
  trade_date date not null,
  close numeric,
  chg_pct numeric,
  volume bigint,
  vol_ratio_20 numeric,       -- 量比20：今日量 / 20日均量
  vol_z60 numeric,            -- 量能 Z-score（60日）
  ma_convergence_pct numeric, -- 均線收斂百分比
  boll_position_pct numeric,  -- 布林通道位階 0-100
  foreign_net bigint,         -- 外資買賣超（股）
  trust_net bigint,           -- 投信買賣超（股）
  dealer_net bigint,          -- 自營商買賣超（股）
  institutional_net bigint,   -- 三大法人合計買賣超（股）
  consecutive_sell_days int,  -- 法人連續賣超天數（負值代表連買）
  primary key (code, trade_date)
);

-- 2. 即時報價快照（盤中每 5 分鐘更新一次，只留最新一筆）
create table if not exists stock_realtime (
  code text primary key,
  name text,
  close numeric,
  chg_pct numeric,
  updated_at timestamptz default now()
);

-- 3. 訊號中心計算結果
create table if not exists signal_scores (
  code text not null,
  name text,
  trade_date date not null,
  close numeric,
  chg_pct numeric,
  net_signal int,             -- 淨訊號分數（多頭訊號數 - 空頭訊號數）
  recommendation text,        -- strong-bull / bull / watch / pullback / trial / neutral / avoid
  recommendation_label text,  -- 顯示文字，如「🚀 強多候選」
  stars int,
  bull_tags text[],           -- 多頭訊號標籤陣列
  bear_tags text[],           -- 空頭訊號標籤陣列
  composite_score numeric,    -- 綜合分數
  primary key (code, trade_date)
);

-- 4. 爆發前兆雷達計算結果
create table if not exists explosion_scores (
  code text not null,
  name text,
  industry text,
  trade_date date not null,
  status text,                -- confirm / pre / retest
  status_label text,
  stage text,
  score numeric,
  close numeric,
  chg_pct numeric,
  breakout_pct numeric,
  box_top_20d numeric,
  vol_ratio_20 numeric,
  vol_z60 numeric,
  ma_convergence_pct numeric,
  boll_position_pct numeric,
  primary key (code, trade_date)
);

-- 5. 風險示警計算結果
create table if not exists risk_scores (
  code text not null,
  name text,
  trade_date date not null,
  risk_level text,            -- high / warn
  risk_score numeric,
  main_risk text,
  suggested_action text,
  close numeric,
  chg_pct numeric,
  vol_ratio numeric,
  atr_pct numeric,
  consecutive_sell_days int,
  liquidity_score numeric,
  note text,
  primary key (code, trade_date)
);

-- 6. 全球資產對照（每日一次）
create table if not exists global_factors (
  factor_code text not null,
  trade_date date not null,
  category text,
  factor_name text,
  value numeric,
  chg_pct numeric,
  direction text,             -- 偏多 / 偏空 / 中性
  impact_score numeric,
  note text,
  primary key (factor_code, trade_date)
);

-- 7. 抓取狀態紀錄（給前端顯示「資料信心」「同步時間」用）
create table if not exists fetch_status (
  id int primary key default 1,
  last_sync_at timestamptz,
  confidence_score int,
  status_note text,
  total_records bigint
);
insert into fetch_status (id, last_sync_at, confidence_score, status_note, total_records)
values (1, now(), 90, 'ok', 0)
on conflict (id) do nothing;

-- 開放給前端匿名讀取（RLS）
alter table stock_daily enable row level security;
alter table stock_realtime enable row level security;
alter table signal_scores enable row level security;
alter table explosion_scores enable row level security;
alter table risk_scores enable row level security;
alter table global_factors enable row level security;
alter table fetch_status enable row level security;

create policy "public read" on stock_daily for select using (true);
create policy "public read" on stock_realtime for select using (true);
create policy "public read" on signal_scores for select using (true);
create policy "public read" on explosion_scores for select using (true);
create policy "public read" on risk_scores for select using (true);
create policy "public read" on global_factors for select using (true);
create policy "public read" on fetch_status for select using (true);
