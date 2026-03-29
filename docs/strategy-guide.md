# Grid + Directional Bias 전략 가이드

## 전략 개요

변동성 자체를 수익원으로 활용하는 그리드 트레이딩 전략.
가격이 오르든 내리든 그리드 안에서 왕복하면 수익이 발생한다.

```
            Sell +5 ──────── 1.030
            Sell +4 ──────── 1.025
            Sell +3 ──────── 1.020
            Sell +2 ──────── 1.015
            Sell +1 ──────── 1.010
     CENTER ════════════════ 1.005  ← 현재가 기준
            Buy  -1 ──────── 1.000
            Buy  -2 ──────── 0.995
            Buy  -3 ──────── 0.990
            Buy  -4 ──────── 0.985
            Buy  -5 ──────── 0.980

    가격이 0.990까지 하락 → Buy -3 FILL
    가격이 0.995로 반등  → Buy -3 TP HIT → +spacing 수익
    (레벨 -3은 PENDING으로 리사이클)
```

## 핵심 메커니즘

### 1. 그리드 생성
- **센터:** 현재 종가 (`center_method: last_close`)
- **범위:** 1h ATR × `range_atr_multiplier` (현재 1.2)
- **레벨 수:** adaptive_levels가 `target_spacing_pct`(0.55%)에 맞춰 자동 조정 (4~16개)
- **간격:** 범위 ÷ 레벨 수 (최소 `min_spacing_pct` 0.55%)

### 2. 바이어스 (방향 기울기)
1h EMA(20/50) + BTC/ETH 추세 + 펀딩비를 종합하여 방향 판단:
- **BULLISH:** Buy 레벨 더 많이, Sell 레벨 줄임
- **BEARISH:** 반대
- **NEUTRAL:** 대칭
- 임계값: `threshold: 0.15`

### 3. Fill 감지
5분봉 캔들의 high/low가 레벨 가격을 터치하면 FILL.
같은 캔들에서 Fill+TP 동시 발생은 차단 (비현실적 방지).

### 4. TP (Take Profit)
각 레벨의 TP = entry ± spacing. Buy면 위로, Sell이면 아래로.
셀당 이익 = spacing × qty - (수수료 × 2).

### 5. 리센터
조건 충족 시 그리드 센터를 현재가로 재배치:
- 가격이 센터에서 3% 이상 이탈 (`recenter_threshold_pct`)
- 180분 경과 (`recenter_interval_minutes`)
- 24시간 경과 (`grid_timeout_hours`)

**중요:** 리센터 시 오픈 포지션은 유지하고 PENDING 레벨만 재배치.
기존 포지션의 level_index와 충돌하는 새 레벨은 제거.

## 수익 구조

```
셀당 이익 = (spacing% - slippage×2 - fee×2) × qty × price

현재 설정:
  spacing:  0.55% (adaptive)
  slippage: 0.15% × 2 = 0.30%
  fee:      0.06% × 2 = 0.12%
  friction: 0.42%
  순마진:   0.55% - 0.42% = 0.13% (spacing의 24%)

셀당 순이익 ≈ 0.003 USDT (2 USDT 포지션 기준)
```

## 리스크 관리

| 파라미터 | 값 | 역할 |
|---------|-----|------|
| max_open_levels | 8 | 동시 오픈 레벨 한도 |
| max_total_exposure_pct | 80% | 총 노셔널 한도 |
| max_drawdown_pct | 20% | 심볼당 미실현 손실 한도 |
| hard_stop_loss_pct | 5% | 계좌 전체 손실 한도 |
| stale_fill_revert_seconds | 360 | P3 미확인 fill 복원 |

## 파라미터 튜닝 히스토리

78 사이클 최적화를 통해 수렴한 값:

| 파라미터 | 초기값 → 최종값 | 이유 |
|---------|----------------|------|
| range_atr_multiplier | 2.5 → 1.2 | 레벨 활용률 2%→10% |
| min_spacing_pct | 0.45 → 0.55 | 수수료 마진 확보 |
| recenter_threshold_pct | 1.5 → 3.0 | 리센터 손실 방지 |
| recenter_interval_minutes | 60 → 180 | 리센터 빈도 감소 |
| max_symbols | 5 → 8 | 거래 빈도 +88% |
| qty_per_level_pct | 5.0 → 2.0 | exposure limit 해소 |

## 그리드 레벨 상태머신

```
PENDING → FILLED → TP_SET → COMPLETED → (recycle to PENDING)
                                    ↗
                   CANCELLED ← recenter
```

- **PENDING:** 가격 대기
- **FILLED:** 캔들이 레벨 터치, P3에 주문 요청
- **TP_SET:** P3가 포지션 오픈 확인 (현재 Fill 즉시 전환)
- **COMPLETED:** TP 도달, 포지션 청산 → 자동 리사이클
- **CANCELLED:** 리센터로 제거

## 알려진 제약

1. **Paper-only:** Live 전환 시 12개 이슈 해결 필요 ([live_transition_plan.md](live_transition_plan.md))
2. **5분 지연:** 캔들 기반 fill 감지 — 실시간 대비 최대 5분 늦음
3. **고정 슬리피지:** 15bps — 실제 알트코인은 30-100bps
4. **넷포지션 미지원:** Paper는 마이크로포지션 독립 추적, Live는 넷포지션
5. **펀딩비 미반영:** 8시간마다 부과되는 펀딩비가 paper에 없음
