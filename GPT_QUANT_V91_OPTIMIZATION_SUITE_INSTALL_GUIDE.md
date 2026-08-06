# GPT Quant V9.1 Optimization Suite v1.0

1. 在 Supabase 執行 `supabase/GPT_QUANT_V91_OPTIMIZATION_SUITE_FOUNDATION.sql`。
2. 解壓後覆蓋到 GPT 專案根目錄。
3. Commit：`Upgrade to GPT Quant V9.1 Optimization Suite v1.0`
4. Push origin。
5. 執行 GitHub Actions：`GPT Quant V9.1 Optimization Suite`
6. 執行 `supabase/GPT_QUANT_V91_OPTIMIZATION_SUITE_VERIFY.sql`。

預設參數：
- limit = 100
- starting_equity = 1000000
- base_risk_budget = 0.60
- max_positions = 10
- max_single_weight = 0.10

安全模式：
- Paper Only
- Live Trading = false
- Broker Submission = false
