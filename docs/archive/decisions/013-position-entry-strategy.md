# ADR-013: 포지션 진입 전략 선택

## 상태

`결정됨`

## 날짜

2026-03-25

## 맥락

단타(최대 90분) + 신규상장 종목이라는 조건에서 진입 전략을 결정해야 한다.

제약:
- 5분봉 기준, 과거 데이터 최대 100개 (약 8시간)
- 신규상장 종목이므로 장기 지표(EMA200, 장기 지지/저항) 사용 불가
- 90분 내 수익 실현이 목표이므로 빠른 신호 생성 필요
- 스캐너가 `suggested_side` (LONG/SHORT/NEUTRAL)를 전달

## 결정

**모멘텀 스캘핑 전략 (MomentumScalper)** 채택.

거래량 동반 단기 모멘텀을 포착하여 빠르게 진입하고, 타이트한 SL/TP로 관리한다.

### 진입 로직

```
[방향 결정]
  스캐너 suggested_side를 기본 방향으로 채택.
  NEUTRAL인 경우 → 모멘텀 방향을 따름.

[LONG 진입 조건] (모든 조건 AND)
  1. EMA5 > EMA10 > EMA20          (단기 추세 정렬)
  2. RSI(14) > 50 and RSI(14) < 75  (상승 모멘텀, 과매수 아님)
  3. 현재 캔들 거래량 > 직전 5캔들 평균 × 1.5  (거래량 확인)
  4. 현재가 > VWAP                   (당일 평균가 위)
  5. 15분봉 EMA5 > EMA10            (상위 TF 방향 일치)

[SHORT 진입 조건] (모든 조건 AND)
  1. EMA5 < EMA10 < EMA20          (단기 추세 정렬)
  2. RSI(14) < 50 and RSI(14) > 25  (하락 모멘텀, 과매도 아님)
  3. 현재 캔들 거래량 > 직전 5캔들 평균 × 1.5  (거래량 확인)
  4. 현재가 < VWAP                   (당일 평균가 아래)
  5. 15분봉 EMA5 < EMA10            (상위 TF 방향 일치)
```

### 왜 이 전략인가

| 요소 | 이유 |
|------|------|
| **짧은 EMA (5/10/20)** | 신규상장 종목에서도 계산 가능. 5분봉 20개 = 100분이면 충분 |
| **거래량 확인** | 신규상장 종목은 수급 주도. 거래량 없는 움직임은 가짜 신호 |
| **VWAP** | 당일 데이터만으로 계산. 기관/대량 거래자의 평균 진입가 역할 |
| **RSI 50 기준** | 방향 확인용. 극단(25/75) 제외로 반전 구간 회피 |
| **15분봉 필터** | 5분봉 노이즈를 상위 TF로 필터링. 큰 흐름과 일치 시만 진입 |
| **스캐너 방향** | BTC/ETH 추세를 반영한 방향. 시장과 역행하는 진입 방지 |

### 스캐너 `suggested_side` 처리

```
suggested_side = LONG:
  → LONG 진입 조건만 평가 (SHORT 무시)
  → 시장 강세에서 롱만 진입하여 승률 향상

suggested_side = SHORT:
  → SHORT 진입 조건만 평가 (LONG 무시)

suggested_side = NEUTRAL:
  → LONG/SHORT 모두 평가, 먼저 충족되는 방향으로 진입
```

스캐너 방향을 **강제**로 따른다. 이유:
- 단타에서 시장 방향과 역행하면 손절 확률이 급등
- BTC/ETH 추세와 반대로 가는 단타는 리스크 대비 보상이 낮음
- NEUTRAL일 때만 양방향 열어두어 기회를 보존

### 설정 파라미터

```yaml
# config/strategy/position.yaml
active: momentum_scalper

strategies:
  momentum_scalper:
    # EMA 기간
    ema_fast: 5
    ema_mid: 10
    ema_slow: 20

    # RSI
    rsi_period: 14
    rsi_long_range: [50, 75]     # LONG: RSI 이 범위 내
    rsi_short_range: [25, 50]    # SHORT: RSI 이 범위 내

    # 거래량 확인
    volume_lookback: 5            # 직전 N캔들 평균
    volume_multiplier: 1.5        # 평균 대비 배수

    # VWAP
    vwap_enabled: true

    # 상위 TF 필터
    higher_tf:
      enabled: true
      timeframe: 15m
      ema_fast: 5
      ema_slow: 10

    # 스캐너 방향 강제
    follow_scanner_direction: true

    # 재진입 방지
    min_candles_between_trades: 3  # 같은 종목 청산 후 최소 3캔들(15분) 대기
```

## 영향

- Indicator Engine: EMA(5,10,20), RSI(14), VWAP, Volume MA 계산
- 5분봉 기준이므로 신호 평가가 5분마다 실행
- 스캐너 방향 강제 → Position Strategy가 단순해짐 (한 방향만 평가)
- 신규상장 종목에서도 모든 지표가 계산 가능 (최소 데이터: 20캔들 = 100분)
