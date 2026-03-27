# ADR-012: 거래 타임프레임 및 최대 보유 시간

## 상태

`결정됨`

## 날짜

2026-03-25

## 맥락

단타 거래를 수행하며, 한 종목의 포지션 보유 시간이 최대 1시간 30분(90분)을 넘지 않아야 한다.
이 제약에 맞는 캔들 타임프레임과 시간 기반 청산 규칙을 결정해야 한다.

## 결정

### Primary 타임프레임: 5분봉

- 90분 = 5분봉 18개. 진입 판단 ~ 청산까지 충분한 캔들 수.
- 1분봉은 노이즈가 과다하고, 15분봉은 90분 내에 캔들 6개뿐이라 신호 생성이 부족.
- 5분봉 기준 지표 계산(EMA, RSI 등)이 단기 모멘텀을 잘 반영.

### Secondary 타임프레임: 15분봉

- 상위 TF 필터로 사용. 90분 내 진입 방향의 큰 흐름 확인.
- 1h, 4h는 단타에서 과도하게 긴 시야. 15분이면 충분.

### 시간 기반 강제 청산

```
포지션 진입 후:
  경과 시간 < 90분  → 정상 운영 (SL/TP/전략 신호에 의한 청산)
  경과 시간 = 90분  → 시장가 강제 청산 (시간 초과)
```

### 타임프레임 설정 변경

```yaml
# config/symbols.yaml
timeframes:
  primary: 5m           # 주 전략 타임프레임 (기존 15m → 5m)
  secondary:
    - 15m               # 상위 TF 필터 (기존 1h → 15m)
  candle_history: 100   # 5분봉 100개 = 약 8시간 (신규상장 종목에 충분)

# config/strategy/position.yaml
time_limit:
  max_holding_minutes: 90       # 최대 보유 시간
  warning_minutes: 75           # 75분 경과 시 알림
  force_close_type: market      # 시간 초과 시 시장가 청산
```

### BTC/ETH 추세 판단 타임프레임

스캐너의 BTC/ETH 추세 판단은 별도 유지:
```yaml
# config/strategy/scanner.yaml
market_env:
  timeframe: 1h    # BTC/ETH 추세는 1h 유지 (단기 시장 방향)
```

단타 종목의 거래 타임프레임(5m)과 시장 환경 판단 타임프레임(1h)은 목적이 다르므로 분리.

## 영향

- Data Collector: 5분봉 + 15분봉 수집 (BTC/ETH는 1h도 수집)
- Indicator Engine: 5분봉 기준 지표 계산, candle_history 100개면 EMA50까지 안정적
- Position Strategy: 90분 타이머 관리 필요
- Order Manager: 시간 초과 강제 청산 로직 구현
- 알림: 75분 경고 + 90분 강제 청산 알림
