# Config 가이드

## 개요

모든 설정은 `config/` 디렉토리에서 YAML 파일로 관리된다.
목적에 따라 파일이 분리되어 있으며, 코드 수정 없이 설정만으로 운영 조건을 변경할 수 있다.

---

## 파일 구조

```
config/
├── app.yaml                  # 앱 전역 설정
├── credentials.yaml.example  # API 키 예시 (버전관리 포함)
├── credentials.yaml          # API 키 실제 파일 (.gitignore)
├── symbols.yaml              # 거래 심볼 및 타임프레임
├── notification.yaml         # 알림 설정
└── strategy/
    ├── scanner.yaml           # 종목 선정 전략 파라미터
    ├── position.yaml          # 진입/청산 전략 파라미터
    └── asset.yaml             # 자산관리 전략 파라미터
```

---

## 파일별 상세

### `app.yaml` — 앱 전역 설정

```yaml
# 실행 모드: paper | live
mode: paper

# 로깅
logging:
  level: INFO            # DEBUG | INFO | WARNING | ERROR
  file: logs/auto_bit.log
  rotation: 10MB
  retention: 30 days

# DB
database:
  type: sqlite
  path: data/auto_bit.db

# 메인 루프
loop:
  rescan_delay_sec: 900        # 스캔 실패 시 재스캔 대기 (15분)
  health_check_sec: 60         # 헬스체크 주기
```

### `credentials.yaml` — API 키 (gitignore 대상)

```yaml
bybit:
  api_key: "YOUR_API_KEY"
  api_secret: "YOUR_API_SECRET"

# Paper 모드에서도 메인넷 API 키 필요 (시세 조회용)
# Live 모드에서는 거래 권한이 있는 키 필요
# 주의: 출금 권한은 절대 부여하지 않을 것

telegram:
  bot_token: "YOUR_BOT_TOKEN"
  chat_id: "YOUR_CHAT_ID"
```

### `symbols.yaml` — 거래 심볼 설정

```yaml
# 거래 시장
market:
  category: linear         # linear (USDT 무기한)
  quote_currency: USDT

# 상시 수집 (BTC/ETH 추세 판단용)
base_symbols:
  - BTCUSDT
  - ETHUSDT

# 블랙리스트 (절대 거래 안 함)
blacklist:
  - USDCUSDT

# 데이터 수집 타임프레임
timeframes:
  primary: 5m           # 주 전략 타임프레임 (단타)
  secondary:
    - 15m              # 상위 TF 필터용
  btc_eth_trend: 1h     # BTC/ETH 추세 판단용 (별도)
  candle_history: 100   # 5분봉 100개 = 약 8시간

# 포지션 관리
positions:
  max_concurrent: 3           # 최대 동시 포지션
  capital_per_position_pct: 5 # 종목당 전체 자본의 5%
```

### `strategy/scanner.yaml` — 종목 선정 전략

```yaml
# 활성 스캐너 전략
active: new_listing

# 스캐너 실행 조건: 포지션 빈 슬롯 발생 시 (온디맨드)
trigger:
  rescan_delay_sec: 900         # 진입 실패 시 재스캔 대기 (15분)

strategies:
  new_listing:
    # 신규상장 기준
    listing:
      max_days_since_listed: 30   # 상장 후 최대 30일
      min_days_since_listed: 1    # 상장 당일 제외 (데이터 부족)

    # 최소 유동성
    liquidity:
      min_24h_turnover_usdt: 30000000  # 최소 3000만 USDT

    # 후보 풀
    pool:
      max_candidates: 30          # 1차 필터 후보 수

    # 스코어링 가중치
    scoring:
      volume_weight: 0.30         # 거래량 점수
      volatility_weight: 0.25     # 변동성 점수
      momentum_weight: 0.20       # 모멘텀 점수
      listing_recency_weight: 0.10 # 신규상장 신선도
      market_env_weight: 0.15     # BTC/ETH 추세 반영
      min_score: 55               # 최소 스코어

    # 변동성 적정 범위
    volatility:
      min_atr_pct: 0.5
      max_atr_pct: 5.0

    # 모멘텀
    momentum:
      rsi_period: 14
      rsi_exclude_below: 20       # RSI 극단 제외
      rsi_exclude_above: 80

    # 진입 필터
    entry_filter:
      cooldown_after_sl_hours: 4  # 손절 후 동일 종목 쿨다운
      volume_decline_threshold: 0.5 # 거래대금 전일 대비 50% 감소 시 제외

    # BTC/ETH 추세 판단
    market_env:
      ema_periods: [20, 50, 200]
      timeframe: 1h
```

### `strategy/position.yaml` — 진입/청산 전략

```yaml
# 활성 포지션 전략
active: momentum_scalper

strategies:
  momentum_scalper:
    # 진입: EMA 추세 정렬
    ema_fast: 5
    ema_mid: 10
    ema_slow: 20

    # 진입: RSI 모멘텀
    rsi_period: 14
    rsi_long_range: [50, 75]
    rsi_short_range: [25, 50]

    # 진입: 거래량 확인
    volume_lookback: 5
    volume_multiplier: 1.5

    # 진입: VWAP
    vwap_enabled: true

    # 진입: 상위 TF 필터
    higher_tf:
      enabled: true
      timeframe: 15m
      ema_fast: 5
      ema_slow: 10

    # 진입: 스캐너 방향
    follow_scanner_direction: true

    # 재진입 방지
    min_candles_between_trades: 3

# 청산 설정 (전략 공통)
exit:
  # 손절 (서버사이드)
  stop_loss:
    type: atr
    atr_period: 14
    atr_multiplier: 1.5
    min_pct: 0.3
    max_pct: 2.0

  # 익절 (서버사이드)
  take_profit:
    type: risk_reward
    risk_reward_ratio: 2.0

  # 트레일링 스탑 (클라이언트)
  trailing_stop:
    activation_r: 1.0
    callback_atr_multiplier: 0.8

  # 전략 청산 신호
  strategy_exit:
    ema_cross_exit: true
    rsi_reversal_exit: true
    volume_dry_exit: true
    volume_dry_threshold: 0.3

  # 시간 제한
  time_limit:
    max_holding_minutes: 90
    warning_minutes: 75
    force_close_type: market
```

### `strategy/asset.yaml` — 자산관리 전략

```yaml
# 활성 자산관리 전략
active: fixed_ratio

# 자본 설정
capital:
  mode: fixed                       # fixed(단리) | compounding(복리)
  initial_balance: 10000            # 초기 자본 (USDT) — Paper에서 사용, Live는 최초 잔고

strategies:
  fixed_ratio:
    # 포지션 사이징 (리스크 기반)
    capital_per_position_pct: 5.0    # 종목당 최대 투입 (캡)
    risk_per_trade_pct: 1.0          # 거래당 리스크 (초기 자본 대비 1%)

    # 레버리지 (SL 기반 동적)
    min_leverage: 1
    max_leverage: 5

    # 동시 포지션
    max_concurrent_positions: 3
    max_per_symbol: 1

  # 일일 리스크 관리
  daily_limits:
    max_daily_loss_pct: 3.0          # 일일 최대 손실 3%
    max_daily_trades: 15             # 일일 최대 거래 15회
    reset_time_utc: "00:00"

  # 연속 손절 대응
  consecutive_loss:
    cooldown_after: 2                # 2회 연속 손절 → 쿨다운
    cooldown_minutes: 30
    stop_after: 3                    # 3회 연속 손절 → 당일 중단

  # 드로다운 관리
  drawdown:
    warning_pct: 5                   # 1단계: 경고
    reduce_pct: 10                   # 2단계: 포지션 50% 축소
    reduce_factor: 0.5
    stop_pct: 15                     # 3단계: 거래 중단 (수동 재개)
    auto_recover: true               # 2단계 회복 시 자동 원복

# Paper 모드 전용
paper:
  fee_rate:
    maker: 0.0001                    # 0.01%
    taker: 0.0006                    # 0.06%
  slippage_bps: 5                    # 슬리피지 (basis points)
```

### `notification.yaml` — 알림 설정

```yaml
telegram:
  enabled: true
  # 알림 유형별 on/off
  events:
    scanner_signal: true
    entry_signal: true
    order_filled: true
    stop_loss_hit: true
    take_profit_hit: true
    daily_report: true
    risk_warning: true
    system_error: true

  # 일일 리포트 시간 (UTC)
  daily_report_time: "00:00"

  # 알림 쿨다운 (같은 종목 반복 알림 방지)
  cooldown_minutes: 5
```

---

## 설정 우선순위

1. 환경변수 (런타임 오버라이드)
2. `credentials.yaml` (시크릿)
3. 각 YAML 설정 파일
4. 코드 내 기본값 (fallback)

## 설정 검증

앱 시작 시 모든 설정 파일의 유효성을 검증한다:
- 필수 필드 존재 여부
- 값 범위 검증 (예: leverage 1~100)
- 파일 존재 여부
- API 키 형식 검증
