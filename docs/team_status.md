# Team Status Board
Updated: 2026-03-28 12:42 KST
Deadline: 2026-03-29 16:00 KST (remaining: ~27 hours)

## System Status
- Mode: Paper Trading (RUNNING, PID=2075830)
- Equity: 20.00 USDT (fresh start after Cycle 1 config change)
- Trades: 0 (DB cleared for clean measurement)
- Grids: awaiting first scan cycle
- Iteration Loop: RUNNING (PID=2077063, 30-min interval)

## Current Iteration: Cycle 1 (Grid Tightening)

### Change Made
**Reduced `range_atr_multiplier` from 2.5 to 1.5** in `config/strategy/grid.yaml`
- Also reduced `min_range_pct` from 1.0% to 0.8%

### Rationale
The single biggest bottleneck was **grid utilization: only 2.0% of levels were active** (1 FILLED out of 49 total). The grid range was 2.5x the 1h ATR, spreading levels so far apart that the 5-minute price movement could not reach most of them. Reducing to 1.5x concentrates levels in the zone where price actually trades, which should dramatically increase fill rate.

This was chosen over other options because:
- Lowering min_spacing_pct (option A): only adds ~2-3 marginal symbols, does not fix the core utilization problem
- Increasing max_symbols (option C): already have 4 grids with max_symbols=5, not the binding constraint
- Bias adjustment (option E): bias is already at 0.15 threshold, and most grids show NEUTRAL -- more data needed before tuning further

### Expected Impact
- Grid utilization: 2% -> 5-10% (2-5x improvement)
- More completed trades per hour (same profitability per trade)
- Spacing remains at ~0.60% (adaptive_levels maintains target)
- No change to risk profile (same leverage, sizing, exposure limits)

### Actual Impact
- TBD -- monitoring from 2026-03-28 12:42 KST onwards

### Next Recommended Change
If utilization is still low after 1-2 hours:
- Further reduce range_atr_multiplier to 1.0
- Or reduce adaptive_levels target_spacing_pct from 0.60% to 0.50%

If utilization improves but few symbols qualify:
- Lower min_spacing_pct from 0.60% to 0.50% (VVVUSDT at 0.59%, GRASSUSDT at 0.5995%, ZROUSDT at 0.5983% are all being filtered just barely)

## Team Tasks Queue
### Completed
- [x] Strategy Analysis: Collect baseline data (Cycle 0)
- [x] Strategy Analysis: Identify grid utilization bottleneck (2.0%)
- [x] Strategy Design: Evaluate range_atr_multiplier reduction
- [x] Implementation: Apply config change (range_atr_multiplier 2.5->1.5)
- [x] QA: All 39 tests passing
- [x] System Monitor: Clean restart verified

### Pending
- [ ] Strategy Analysis: Measure Cycle 1 impact after 1-2 hours
- [ ] Strategy Design: Evaluate min_spacing_pct reduction (0.60% -> 0.50%)
- [ ] Strategy Design: Evaluate recenter_interval optimization
- [ ] Implementation: Add grid performance metrics to GUI API
- [ ] Parity: Document all paper-vs-live gaps
- [ ] QA: Write integration test for margin accounting

## Iteration Log
| Cycle | Duration | Trades | PnL | WR | Changes |
|-------|----------|--------|-----|----|---------|
| 0 | ~15h | 95* | +2.945* | 92.6%* | baseline (*instant-TP artifact, pre-fix) |
| 0.5 | ~12h | 1 | +0.0039 | 100% | exposure fix, bias threshold, min_spacing 0.60% |
| 1 | starting | -- | -- | -- | range_atr_multiplier 2.5->1.5, min_range_pct 1.0->0.8 |

## Key Observations
- 664 exposure rejections logged (from Cycle 0 when qty_per_level_pct was 5%)
- 224 symbol-skip events due to spacing filter
- Symbols just below 0.60% cutoff: VVVUSDT (0.59%), GRASSUSDT (0.5995%), ZROUSDT (0.5983%)
- All bias readings are NEUTRAL despite 0.15 threshold -- may need further investigation
- SIRENUSDT positions show large unrealized swings (+/-40%) -- high volatility symbol

## Cycle 1 Check (12:52 KST)
- **결정: 변경 없음 (관찰 모드)**
- range_atr_multiplier 1.5 적용 후 10분 경과
- Fill 13건 성공, Rejection 0건, TP no-position 0건 — 모두 개선
- TP 아직 0건 — spacing(0.6%) 되돌림 대기 중
- 20분 후 TP 발생 여부 확인 예정

## Cycle 2 (13:00 KST)
- **변경: min_spacing_pct 0.60%→0.50%, target_spacing_pct 0.60%→0.50%**
- 사유: TP 도달률 4.2% (24 fills, 1 TP). Spacing이 넓어서 가격이 TP까지 못 감
- 예상: TP 도달률 상승 + 더 많은 심볼 진입 가능
- BEFORE: Fills=24, TPs=1, Util=4.8%, Trades=0
- AFTER 10min: Pos=8, grid_ops=8, system healthy
- 간극체크: spacing 0.50% vs friction 0.42% = 16% margin (타이트하지만 허용)

## Cycle 3 (13:30 KST)
- **변경: range_atr_multiplier 1.5→1.0, min_spacing 0.50%→0.55%, target_spacing 0.50%→0.55%**
- 사유: Cycle 2에서 spacing 0.50%가 수수료(0.42%)를 거의 못 커버 → 순이익 제로
- range를 더 줄여 레벨을 현재가에 집중, spacing은 0.55%로 올려 수수료 마진 확보
- BEFORE: 1 trade, PnL +0.0016 (fee 0.0024 > gross profit)
- AFTER 10min: 2 pos, system healthy, margin match

## Cycle 4 (14:00 KST)
- **변경: range_atr_multiplier 1.0→1.2, recenter_threshold 2.0→3.0**
- 사유: Cycle 3에서 range 1.0이 너무 타이트 → 리센터 2건 발생 → 강제 청산 -0.017 손실
- range를 1.2로 적당히 넓히고, recenter threshold를 3%로 올려 리센터 빈도 감소
- BEFORE: 3 trades (1 TP +0.003, 2 recenter -0.017), net PnL -0.014
- AFTER 10min: 3 pos, system healthy
- 파라미터 히스토리:
  - range: 2.5 → 1.5 → 1.0 → 1.2 (수렴 중)
  - spacing: 0.60 → 0.50 → 0.55 (수렴 중)
  - recenter: 1.5 → 2.0 → 3.0

## Cycle 5 (14:31 KST) — OBSERVATION
- **변경: 없음 (관찰 모드)**
- Cycle 4 결과가 양호: 2 TPs, WR 100%, recenters 0, PnL +0.0058
- 파라미터 수렴: range=1.2, spacing=0.55%, recenter=3.0
- 셀당 순이익이 작음 (0.0005/cell) — 더 많은 데이터 필요
- 1시간 관찰 후 다음 결정

## Cycle 6 (14:52 KST) — OBSERVATION
- **변경: 없음 (관찰 계속)**
- 50분간 결과: 5 trades, WR 80%, PnL +0.0035
- grid_tp 4건 (+0.0118), recenter 1건 (-0.0084)
- TP율 10.4% (67 fills → 7 TPs)
- 파라미터 수렴 확인: range=1.2, spacing=0.55%, recenter=3.0
- 이 파라미터를 "best so far"로 기록
- 다음 사이클: 1시간 더 관찰 후 통계적 의미 있는 결정

## Cycle 7 (15:13 KST)
- **변경: recenter_interval_minutes 60→180**
- 사유: 1.5시간 관찰에서 recenter 11건이 -0.093 손실 (grid_tp +0.027의 3.5배!)
- 시간 기반 recenter가 60분마다 포지션을 강제 청산하여 대부분의 손실 원인
- PTBUSDT(-0.026), KITEUSDT(-0.023): recenter 피해 심볼
- BEFORE: 20 trades, WR 45%, PnL -0.066
- Best params so far: range=1.2, spacing=0.55%, recenter_interval=180min, recenter_th=3.0
- 누적 파라미터 변경 히스토리:
  | Param | C1→C2→C3→C4→C7 |
  | range | 2.5→1.5→1.0→1.2 |
  | spacing | 0.60→0.50→0.55 |
  | recenter_th | 1.5→2.0→3.0 |
  | recenter_int | 60→180 |

## Cycle 8 (15:44 KST) — BEST RESULT
- **변경: 없음 (최적 파라미터 도달)**
- recenter_interval 180min 효과 확인: 4 trades, WR 100%, PnL +0.011, recenters 0
- C4 대비 3.7배 수익 향상
- **BEST CONFIG: range=1.2, spacing=0.55%, recenter_th=3.0, recenter_int=180min**
- 계속 관찰하여 장기 안정성 검증

## Cycle 9 (16:04 KST) — STABLE PROFIT
- 변경: 없음
- 누적 2.5h: 7 trades, WR 100%, PnL +0.019, recenters 0
- 연환산: ~0.18 USDT/day = 0.9%/day (paper, 슬리피지 고려 전)
- TRIAUSDT best performer (4 trades, +0.010)
- 셀당 순이익 0.0027 (fee ratio 47% — 개선 여지 있음)

## Cycle 10 (16:25 KST) — EQUITY ABOVE 20 USDT!
- 변경: 없음
- 누적 3h: 17 trades, **WR 100%**, PnL +0.047, recenters 0
- **Equity 20.03 — 최초 순이익 돌파!**
- 연환산: ~0.38 USDT/day = 1.9%/day
- 시스템 완전 안정. 장기 관찰 계속.

## Cycle 11 (16:45 KST)
- 변경: 없음. 23 trades, WR 100%, PnL +0.065. 시스템 안정.

## Cycle 12 (17:06 KST) — RECENTER FIX
- **변경: recenter 시 오픈 포지션 유지 (강제 청산 제거)**
- 사유: 30 trades 중 6 recenter가 -0.050 손실 (grid_tp +0.068의 73%)
- 이전: recenter → 모든 포지션 시장가 청산 → 손실
- 이후: recenter → PENDING만 재배치, 오픈 포지션은 TP 대기 유지
- 예상: recenter 손실 제거 → 순이익 대폭 개선
- AFTER 10min: 12 pos, system healthy, no recenters

## Cycle 13 (17:37 KST)
- 변경: 없음. Recenter fix 확인: 2 trades, WR 100%, PnL +0.006, recenters 0

## Cycle 14 (17:57 KST) — RECENTER FALLBACK FIX
- **변경: recenter fallback에서도 오픈 포지션 유지 (그리드 생성 실패 시)**
- 사유: _create_grid_for_symbol이 None(spacing 필터)일 때 오픈 포지션 강제 청산됨
- 수정: fallback에서도 포지션 유지, recenter timestamp만 갱신
- 8 trades 중 3 recenter(-0.025)가 제거될 것으로 예상

## Cycle 15 (18:19 KST) — OBSERVATION
- 변경: 없음. 0 trades, 9 pos, recenter 0건. TP 대기 중.

## Cycle 16 (18:39 KST) — RECENTER SKIP CONFIRMED
- 변경: 없음
- 2 trades, WR 100%, PnL +0.006
- **"recenter skipped" 2건 작동 확인** — 포지션 유지, 강제 청산 없음
- 파라미터 안정 수렴. 장기 관찰 모드 유지.

## Cycle 17 (19:00 KST) — STABLE
- 변경: 없음
- 2h 누적: 4 trades, WR 100%, PnL +0.012, 0 recenters
- 2.1 trades/h, 0.006 USDT/h = 0.15/day = 0.75%/day
- 안정 확인. 다음 변경 후보: max_symbols 5→8 (거래 빈도 증가)

## Cycle 18 (19:20 KST) — MAX SYMBOLS INCREASE
- **변경: max_symbols 5→8**
- 사유: 2.5h에 8 trades(WR 100%, PnL +0.024). 거래 빈도 증가 여지 있음.
- 5 active grids가 max에 걸려있었음. 8로 올려 더 많은 심볼 참여.
- BEFORE: 3.2 trades/h, 5 grids
- 예상: 거래 빈도 +50-60% (8심볼에서 더 많은 fill/TP)

## Cycle 19 (19:41 KST)
- 변경: 없음. max_symbols 8 효과 관찰.
- 8 grids active (이전 5), 31 pos, 1 TP, PnL +0.003
- 더 관찰 필요.

## Cycle 20 (20:01 KST) — MAX SYMBOLS EFFECT CONFIRMED
- 변경: 없음
- 40min: 4 trades, WR 100%, PnL +0.012, recenters 0
- **6.0 trades/h (이전 3.2에서 +88% 증가!)**
- **연환산: 0.43 USDT/day = 2.1%/day**
- max_symbols 8 효과 확인. 시스템 안정.
- CURRENT BEST: range=1.2, spacing=0.55%, recenter_th=3.0, recenter_int=180min, max_sym=8

## Cycle 21 (20:22 KST) — PEAK PERFORMANCE
- 변경: 없음
- 1h: **13 trades, WR 100%, PnL +0.037, recenters 0**
- **13.0 trades/h, 연환산 4.5%/day**
- Cycle 4(5심볼) 대비 4배 개선
- 모든 파라미터 안정 수렴. 장기 관찰 전환.
- **FINAL BEST CONFIG:**
  range_atr_multiplier=1.2
  min_spacing_pct=0.55%
  target_spacing_pct=0.55%
  recenter_threshold_pct=3.0
  recenter_interval_minutes=180
  max_symbols=8
  max_open_levels=8
  leverage=5
  qty_per_level_pct=2.0
  + recenter 시 오픈 포지션 유지 (코드 수정)

## Cycle 22 (20:42 KST) — EQUITY > 20.00!
- 변경: 없음. 19 trades, WR 100%, PnL +0.055
- Equity 20.004. 모든 시스템 정상.

## Cycle 23 (21:02 KST) — ISSUE DETECTED
- WR 85%, PnL -0.014
- BEATUSDT 3건 대손실 (각 -0.063) — grid_tp인데 손실 → 조사 필요
- RIVERUSDT #653 비정상 이익 (+0.114) — 정상 셀 이익의 40배
- 가능 원인: close_position_by_key가 잘못된 포지션을 닫거나, 리센터 후 잘못된 TP 매칭
- 다음 사이클에서 조사 + 수정

## Cycle 24 (21:23 KST) — RECENTER INDEX CONFLICT FIX
- **변경: recenter 시 kept_levels와 새 levels의 level_index 충돌 방지**
- 사유: BEATUSDT Buy @ 0.4986이 recenter 후 새 그리드의 TP(0.4835)로 닫혀 -0.063 손실 3건
- 원인: recenter 후 같은 index(-2)에 기존 포지션과 새 레벨이 겹침
- 수정: 새 pending 레벨에서 kept_levels의 index와 겹치는 것 제거
- CRITICAL FIX — 이전 recenter 유지 로직의 핵심 버그

## Cycle 25 (21:44 KST)
- 변경: 없음. 0 trades, 11 pos, recenter 6건 skipped/keeping. TP 대기.

## Cycle 26 (22:05 KST) — INDEX CONFLICT FIX CONFIRMED
- 변경: 없음
- 5 trades, WR 100%, PnL +0.016, **large losses 0건!**
- Recenter index conflict fix 효과 확인
- **모든 핵심 버그 해결 완료:**
  1. level_id=0 매핑 → (symbol, level_index) 키 사용
  2. recenter 강제 청산 → 오픈 포지션 유지
  3. recenter index 충돌 → 겹치는 index 제거
  4. margin 회계 → close_by_key + entry fee 추적
- 일요일 16:00까지 장기 관찰 모드 전환
C27 22:45 T:16 WR:100% PnL:+0.048782 Eq:20.0272 Bad:0 Rec:0
C28 23:05 T:25 WR:100% PnL:+0.076019 Eq:20.0586 Bad:0 Rec:0
C29 23:25 T:27 WR:100% PnL:+0.081355 Eq:20.0486 Bad:0 Rec:0
C30 23:45 T:34 WR:100% PnL:+0.103010 Eq:20.0732 Bad:0 Rec:0
C31 00:05 T:44 WR:100% PnL:+0.132771 Eq:20.1041 Bad:0 Rec:0
C32 00:25 T:48 WR:100% PnL:+0.145319 Eq:20.1142 Bad:0 Rec:0
C33 00:46 T:52 WR:100% PnL:+0.158588 Eq:20.1227 Bad:0
C34 01:06 T:58 WR:100% PnL:+0.193887 Eq:20.1652 Bad:0
C35 01:26 T:64 WR:100% PnL:+0.248497 Eq:20.2084 Bad:0
C36 01:46 T:67 WR:100% PnL:+0.257065 Eq:20.2176 Bad:0
C37 02:06 T:71 WR:100% PnL:+0.270199 Eq:20.2355 Bad:0
C38 02:26 T:71 WR:100% PnL:+0.270199 Eq:20.2301 Bad:0
C39 02:46 T:77 WR:97% PnL:+0.295871 Eq:20.2606 Bad:2
C40 03:06 T:79 WR:97% PnL:+0.301570 Eq:20.2597 Bad:2
C41 03:26 T:84 WR:98% PnL:+0.520901 Eq:20.4842 Bad:2
C42 03:46 T:87 WR:98% PnL:+0.529685 Eq:20.4930 Bad:2
C43 04:06 T:88 WR:98% PnL:+0.532527 Eq:20.4917 Bad:2
C44 04:26 T:94 WR:98% PnL:+0.549412 Eq:20.5097 Bad:2
C45 04:46 T:97 WR:97% PnL:+0.523189 Eq:20.4834 Bad:3
C46 05:06 T:99 WR:97% PnL:+0.529197 Eq:20.4887 Bad:3
C47 05:26 T:101 WR:97% PnL:+0.535204 Eq:20.4929 Bad:3
C48 05:46 T:102 WR:97% PnL:+0.537925 Eq:20.4945 Bad:3
C49 06:06 T:104 WR:97% PnL:+0.543487 Eq:20.4969 Bad:3
C50 06:26 T:107 WR:97% PnL:+0.553453 Eq:20.5107 Bad:3
C51 06:46 T:109 WR:97% PnL:+0.558817 Eq:20.5184 Bad:3
C52 07:06 T:116 WR:97% PnL:+0.615414 Eq:20.5713 Bad:3
C53 07:26 T:121 WR:98% PnL:+0.629890 Eq:20.5859 Bad:3
C54 07:46 T:123 WR:98% PnL:+0.636214 Eq:20.5939 Bad:3
C55 08:06 T:126 WR:98% PnL:+0.645770 Eq:20.5974 Bad:3
C56 08:26 T:129 WR:98% PnL:+0.655238 Eq:20.6099 Bad:3
C57 08:46 T:130 WR:98% PnL:+0.658110 Eq:20.6122 Bad:3
C58 09:06 T:132 WR:98% PnL:+0.664057 Eq:20.6170 Bad:3
C59 09:26 T:135 WR:98% PnL:+0.672836 Eq:20.6269 Bad:3
C60 09:46 T:140 WR:97% PnL:+0.719945 Eq:20.6733 Bad:4
C61 10:06 T:146 WR:97% PnL:+0.738849 Eq:20.6934 Bad:4
C62 10:26 T:147 WR:97% PnL:+0.741936 Eq:20.6941 Bad:4
C63 10:46 T:148 WR:97% PnL:+0.745585 Eq:20.6921 Bad:4
C64 11:06 T:153 WR:97% PnL:+0.904210 Eq:20.8569 Bad:4
C65 11:26 T:156 WR:97% PnL:+0.912498 Eq:20.8621 Bad:4
C66 11:46 T:160 WR:98% PnL:+1.064730 Eq:21.0131 Bad:4
C67 12:06 T:165 WR:98% PnL:+1.080471 Eq:21.0326 Bad:4
C68 12:26 T:171 WR:98% PnL:+1.099030 Eq:21.0491 Bad:4
C69 12:46 T:177 WR:98% PnL:+1.125549 Eq:21.0755 Bad:4
C70 13:06 T:177 WR:98% PnL:+1.125549 Eq:21.0718 Bad:4
C71 13:26 T:181 WR:97% PnL:+1.098405 Eq:21.0481 Bad:5
C72 13:46 T:182 WR:97% PnL:+1.101419 Eq:21.0499 Bad:5
C73 14:06 T:182 WR:97% PnL:+1.101419 Eq:21.0499 Bad:5
C74 14:26 T:184 WR:97% PnL:+1.107113 Eq:21.0561 Bad:5
C75 14:46 T:186 WR:97% PnL:+1.112618 Eq:21.0604 Bad:5
C76 15:06 T:186 WR:97% PnL:+1.112618 Eq:21.0604 Bad:5
C77 15:26 T:186 WR:97% PnL:+1.112618 Eq:21.0604 Bad:5
