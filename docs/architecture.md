# 시스템 아키텍처

## 개요

Auto Bit은 3단계 전략 파이프라인 기반의 자동거래 시스템이다.
메인넷 실시간 데이터를 사용하며, paper/live 두 가지 모드로 실행된다.

---

## 핵심 설계 원칙

1. **메인넷 단일 데이터 소스** - 테스트넷이 아닌 메인넷 데이터만 사용. paper 모드도 실제 시장 데이터 기반.
2. **전략 3분리** - 종목 선정 / 진입·청산 / 자산관리를 독립적으로 교체·조합 가능.
3. **실행 모드 분리** - 전략 로직은 동일, 주문 실행 레이어만 paper/live 전환.
4. **Config 기반 운영** - 코드 수정 없이 YAML 설정만으로 전략 파라미터 조정.

---

## 데이터 흐름

```
Bybit Mainnet
     │
     ├── REST API ──── 초기 히스토리 로드, 계좌 조회
     │
     └── WebSocket ─── 실시간 캔들, 호가, 체결
            │
            ▼
     ┌─────────────┐
     │   DB/Cache   │  ← 수집된 원시 데이터 저장
     └──────┬──────┘
            │
            ▼
     ┌─────────────┐
     │  Indicators  │  ← 지표 계산 (EMA, RSI, MACD 등)
     └──────┬──────┘
            │
     ═══════╪═══════════════════════════════════
     ║  Strategy Pipeline                      ║
     ║      │                                  ║
     ║      ▼                                  ║
     ║  [Scanner] 전체 심볼 → 거래 대상 필터   ║
     ║      │                                  ║
     ║      ▼                                  ║
     ║  [Position] 대상 심볼 → 진입/청산 신호  ║
     ║      │                                  ║
     ║      ▼                                  ║
     ║  [Asset] 신호 → 포지션 크기/리스크 결정  ║
     ═══════╪═══════════════════════════════════
            │
            ▼
     ┌─────────────┐
     │   Executor   │
     │  ┌─────────┐ │
     │  │  paper   │ │  ← 가상 체결 엔진
     │  ├─────────┤ │
     │  │  live    │ │  ← Bybit API 주문
     │  └─────────┘ │
     └──────┬──────┘
            │
            ▼
     ┌─────────────┐
     │   Tracker    │  ← 포지션/P&L 추적, 거래 이력
     └──────┬──────┘
            │
            ▼
     ┌─────────────┐
     │ Notification │  ← 텔레그램 알림
     └─────────────┘
```

---

## 모듈 간 인터페이스

### DataCollector → IndicatorEngine

```python
# OHLCV DataFrame
{
    "symbol": "BTCUSDT",
    "timeframe": "15m",
    "data": pd.DataFrame  # columns: open, high, low, close, volume, timestamp
}
```

### Scanner → Position Strategy

```python
# 선정된 종목 리스트
[
    {"symbol": "BTCUSDT", "score": 85, "reason": "volume_spike"},
    {"symbol": "ETHUSDT", "score": 72, "reason": "momentum"},
]
```

### Position Strategy → Asset Strategy

```python
# 매매 신호
{
    "symbol": "BTCUSDT",
    "signal": "LONG",        # LONG | SHORT | CLOSE | HOLD
    "entry_price": 65000.0,
    "stop_loss": 63700.0,
    "take_profit": 67600.0,
    "timeframe": "15m",
    "strategy": "ema_cross",
    "confidence": 0.75,
    "reason": "EMA20 crossed above EMA50 with volume confirmation"
}
```

### Asset Strategy → Order Manager

```python
# 승인된 주문
{
    "approved": True,
    "symbol": "BTCUSDT",
    "side": "Buy",
    "size": 0.015,            # BTC 수량
    "leverage": 3,
    "order_type": "Market",
    "stop_loss": 63700.0,
    "take_profit": 67600.0,
    "risk_amount_usdt": 19.5, # 리스크 금액
    "reason": "2% risk, fixed_ratio sizing"
}
```

---

## Paper Trading 엔진 설계

### 가상 체결 로직

```
시장가 주문:
  → 현재 best ask(매수) / best bid(매도) 가격으로 즉시 체결
  → 슬리피지 설정 시 ±slippage_bps 적용

지정가 주문:
  → 미체결 주문 큐에 추가
  → 실시간 가격이 지정가 도달 시 체결
  → 타임아웃 설정 가능

SL/TP 주문:
  → 포지션 보유 중 가격이 SL/TP 도달 시 자동 체결
```

### 가상 계좌

```python
PaperAccount:
    initial_balance: float     # 초기 자본
    balance: float             # 현재 잔고
    positions: List[Position]  # 보유 포지션
    orders: List[Order]        # 미체결 주문
    trades: List[Trade]        # 체결 이력
    fees_paid: float           # 누적 수수료
```

### 수수료 시뮬레이션

실제 Bybit 수수료율 적용:
- Maker: 0.01%
- Taker: 0.06%
- 펀딩비: 8시간마다 실제 펀딩비율 적용

---

## 동시성 모델 (멀티프로세스)

> ADR-001 결정에 따라 멀티프로세스 아키텍처를 채택한다.

```
Main Process (Orchestrator)
  │
  │  역할: 자식 프로세스 생성/감시, 헬스체크, 재시작
  │
  ├── [P1] Data Collector Process
  │     └── asyncio event loop
  │           ├── WebSocket 연결 관리
  │           ├── 캔들 데이터 수신/저장
  │           └── 오더북/체결 데이터 수신
  │     → (Queue) → P2로 시장 데이터 전달
  │
  ├── [P2] Strategy Engine Process
  │     └── 동기 메인 루프
  │           ├── 지표 계산 (CPU 집약)
  │           ├── Scanner 전략 실행
  │           └── Position 전략 실행
  │     ← (Queue) ← P1에서 시장 데이터 수신
  │     ← (Queue) ← P3에서 포지션 상태 수신
  │     → (Queue) → P3로 매매 신호 전달
  │
  ├── [P3] Order Manager Process
  │     └── asyncio event loop
  │           ├── Asset 전략 (포지션 크기 결정)
  │           ├── 주문 실행 (paper/live)
  │           └── 포지션 추적 / P&L 계산
  │     ← (Queue) ← P2에서 매매 신호 수신
  │     → (Queue) → P2로 포지션 상태 피드백
  │     → (Queue) → P4로 알림 이벤트 전달
  │
  └── [P4] Notification Process
        └── asyncio event loop
              └── 텔레그램 봇 알림 전송
        ← (Queue) ← P3에서 알림 이벤트 수신
```

### IPC 메시지 포맷

프로세스 간 통신은 `multiprocessing.Queue`를 사용하며, 메시지는 직렬화 가능한 dataclass로 정의한다.

```python
@dataclass
class MarketDataMessage:
    """P1 → P2: 시장 데이터"""
    msg_type: str = "market_data"
    symbol: str
    timeframe: str
    candle: dict          # {open, high, low, close, volume, timestamp}
    timestamp: float

@dataclass
class SignalMessage:
    """P2 → P3: 매매 신호"""
    msg_type: str = "signal"
    symbol: str
    signal: str           # LONG | SHORT | CLOSE | HOLD
    entry_price: float
    stop_loss: float
    take_profit: float
    strategy: str
    confidence: float
    reason: str
    timestamp: float

@dataclass
class PositionUpdateMessage:
    """P3 → P2: 포지션 상태 피드백"""
    msg_type: str = "position_update"
    positions: list       # 현재 열린 포지션 목록
    daily_pnl: float
    balance: float
    timestamp: float

@dataclass
class NotificationMessage:
    """P3 → P4: 알림 이벤트"""
    msg_type: str = "notification"
    event_type: str       # order_filled, sl_hit, tp_hit, risk_warning, ...
    title: str
    body: str
    timestamp: float
```

### Watchdog (프로세스 감시)

```
Main Process:
  - 각 자식 프로세스의 is_alive() 주기적 확인
  - 비정상 종료 감지 시:
    1. 로그 기록
    2. 알림 전송 (P4가 살아있으면)
    3. 프로세스 재시작
  - P3(Order Manager) 장애 시:
    → 서버사이드 SL/TP 주문이 포지션 보호
    → 재시작 후 Bybit에서 현재 포지션 동기화
  - 전체 시스템 종료 시:
    → graceful shutdown (SIGTERM → 각 프로세스 정리 → 종료)
```

---

## DB 스키마 (초기 SQLite)

```sql
-- 캔들 데이터
CREATE TABLE candles (
    symbol TEXT,
    timeframe TEXT,
    timestamp INTEGER,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, timeframe, timestamp)
);

-- 거래 이력 (paper/live 공통)
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT,             -- 'paper' | 'live'
    symbol TEXT,
    side TEXT,             -- 'Buy' | 'Sell'
    size REAL,
    entry_price REAL,
    exit_price REAL,
    pnl REAL,
    fee REAL,
    strategy TEXT,
    entry_time INTEGER,
    exit_time INTEGER,
    entry_reason TEXT,
    exit_reason TEXT
);

-- 현재 포지션
CREATE TABLE positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT,
    symbol TEXT,
    side TEXT,
    size REAL,
    entry_price REAL,
    leverage INTEGER,
    stop_loss REAL,
    take_profit REAL,
    unrealized_pnl REAL,
    opened_at INTEGER
);

-- 일별 성과
CREATE TABLE daily_performance (
    date TEXT,
    mode TEXT,
    starting_balance REAL,
    ending_balance REAL,
    pnl REAL,
    trade_count INTEGER,
    win_count INTEGER,
    PRIMARY KEY (date, mode)
);
```
