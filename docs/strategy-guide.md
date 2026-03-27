# 전략 개발 가이드

## 3단계 전략 파이프라인

Auto Bit의 전략은 3가지 독립된 레이어로 구성된다.
각 레이어는 독립적으로 교체·조합할 수 있다.

```
┌──────────────────────────────────────────────┐
│         Strategy Pipeline                     │
│                                              │
│  [Scanner]  →  [Position]  →  [Asset]        │
│  "무엇을"      "언제"          "얼마나"        │
│  거래할까?     진입/청산할까?   투자할까?       │
└──────────────────────────────────────────────┘
```

---

## 1. Scanner Strategy (종목 선정)

> "전체 시장에서 **무엇을** 거래할 것인가?"

### 역할
- 수백 개의 심볼 중 현재 거래 기회가 있는 종목을 필터링
- 주기적으로 실행 (기본 5분 간격)
- 순위화된 심볼 리스트 출력

### 인터페이스

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class ScanResult:
    symbol: str
    score: float          # 0~100
    reason: str
    metadata: dict        # 전략별 추가 데이터

class BaseScannerStrategy(ABC):
    """종목 선정 전략 기본 클래스"""

    @abstractmethod
    def scan(self, market_data: dict) -> list[ScanResult]:
        """
        전체 시장 데이터를 받아 거래 대상 심볼을 선정한다.

        Args:
            market_data: {symbol: DataFrame} 형태의 시장 데이터

        Returns:
            ScanResult 리스트 (score 내림차순)
        """
        pass

    @abstractmethod
    def get_default_params(self) -> dict:
        """전략 기본 파라미터 반환 (config 오버라이드 가능)"""
        pass
```

### 구현 예시

| 전략 | 설명 | 주요 파라미터 |
|------|------|-------------|
| `VolumeMomentumScanner` | 거래량 급증 + 모멘텀 종목 | `min_volume_change`, `rsi_range` |
| `FundingRateScanner` | 극단적 펀딩비 종목 | `extreme_threshold`, `direction` |
| `BreakoutScanner` | 가격 돌파 임박 종목 | `consolidation_period`, `squeeze_threshold` |

---

## 2. Position Strategy (진입/청산)

> "선정된 종목에 **언제** 진입하고 **언제** 청산할 것인가?"

### 거래 스타일: 단타 (최대 90분 보유)

- 5분봉 기준, 캔들 완성 시마다 평가
- 15분봉을 상위 TF 필터로 사용
- 스캐너의 `suggested_side`를 방향으로 강제 (NEUTRAL 시 양방향)
- 90분 초과 시 시장가 강제 청산

### 인터페이스

```python
from enum import Enum

class SignalType(Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    CLOSE = "CLOSE"
    HOLD = "HOLD"

@dataclass
class PositionSignal:
    symbol: str
    signal: SignalType
    entry_price: float
    stop_loss: float          # ATR(14) × 1.5 기반
    take_profit: float        # SL × R:R(2.0) 기반
    confidence: float         # 0.0 ~ 1.0
    strategy: str
    timeframe: str
    reason: str
    suggested_side: str       # 스캐너에서 전달받은 방향

class BasePositionStrategy(ABC):
    """진입/청산 전략 기본 클래스"""

    @abstractmethod
    def evaluate(
        self,
        symbol: str,
        indicators: dict,
        current_position: dict | None,
        scan_result: ScanResult | None
    ) -> PositionSignal:
        """
        Args:
            symbol: 거래 심볼
            indicators: 5분봉 + 15분봉 지표 값
            current_position: 현재 보유 포지션 (없으면 None)
            scan_result: 스캐너 결과 (suggested_side 포함)
        """
        pass
```

### 진입 전략: MomentumScalper

```
[LONG 진입] (모든 조건 AND)
  1. EMA5 > EMA10 > EMA20            단기 추세 정렬
  2. RSI(14) 50~75                    상승 모멘텀, 과매수 아님
  3. 현재 거래량 > 5캔들 평균 × 1.5   거래량 확인
  4. 현재가 > VWAP                    평균가 위
  5. 15분봉 EMA5 > EMA10             상위 TF 일치

[SHORT 진입] (모든 조건 AND — 위의 반대)
```

### 청산 전략 (우선순위순)

| 순위 | 청산 유형 | 실행 위치 | 조건 |
|:---:|----------|----------|------|
| 1 | SL/TP | Bybit 서버 | ATR×1.5 손절, R:R 2.0 익절 |
| 2 | 시간 초과 | 클라이언트 | 90분 경과 → 시장가 청산 |
| 3 | 트레일링 스탑 | 클라이언트 | 1R 수익 시 활성화, ATR×0.8 콜백 |
| 4 | 전략 신호 | 클라이언트 | EMA 역교차, RSI 꺾임, 거래량 급감 |
| 5 | 스캐너 탈락 | 클라이언트 | 수익→트레일링, 손실→SL 유지 |

---

## 3. Asset Strategy (자산관리)

> "승인된 신호에 **얼마나** 투자하고, **언제** 거래를 멈출 것인가?"

### 역할
- 포지션 크기 결정 (리스크 기반 역산 + 5% 캡)
- 레버리지 결정 (SL 거리 기반 동적)
- 일일 리스크 한도 관리
- 연속 손절 쿨다운
- 드로다운 단계별 대응
- 거래 승인/거부 최종 게이트

### 인터페이스

```python
@dataclass
class OrderRequest:
    approved: bool
    symbol: str
    side: str              # "Buy" | "Sell"
    size: float            # 주문 수량
    leverage: int
    order_type: str        # "Market"
    stop_loss: float
    take_profit: float
    risk_amount: float     # 리스크 금액 (USDT)
    reject_reason: str     # 거부 시 사유

@dataclass
class DailyStats:
    pnl: float                    # 오늘 실현 손익
    trade_count: int              # 오늘 거래 횟수
    consecutive_losses: int       # 연속 손절 횟수
    cooldown_until: float | None  # 쿨다운 해제 시간

class BaseAssetStrategy(ABC):
    """자산관리 전략 기본 클래스"""

    @abstractmethod
    def evaluate(
        self,
        signal: PositionSignal,
        initial_balance: float,
        current_balance: float,
        open_positions: list,
        daily_stats: DailyStats
    ) -> OrderRequest:
        """
        Args:
            signal: 포지션 전략의 매매 신호
            initial_balance: 초기 자본 (단리 기준)
            current_balance: 현재 잔고
            open_positions: 현재 열린 포지션 목록
            daily_stats: 오늘 거래 통계
        """
        pass
```

### 포지션 사이징 로직

```
risk_amount = initial_balance × 1%              # 거래당 리스크 고정
sl_distance = ATR(14) × 1.5                     # 손절 거리
position_size = risk_amount / sl_distance        # 리스크 기반 역산
position_size = min(position_size, initial_balance × 5%)  # 5% 캡
leverage = ceil(position_size / available_margin) # 동적 레버리지
leverage = clamp(leverage, 1, 5)                 # 1x~5x 범위
```

### 거부 조건

| 조건 | 사유 |
|------|------|
| 동시 포지션 ≥ 3 | 슬롯 없음 |
| 동일 심볼 기보유 | 중복 포지션 |
| 일일 손실 ≥ 3% | 일일 한도 초과 |
| 일일 거래 ≥ 15회 | 과잉 거래 방지 |
| 연속 손절 ≥ 2 (쿨다운 중) | 30분 대기 |
| 연속 손절 ≥ 3 | 당일 중단 |
| 드로다운 ≥ 15% | 거래 중단 (수동 재개) |
| 잔고 부족 | 마진 부족 |

### 드로다운 3단계

```
 5% → 경고 알림
10% → 포지션 크기 50% 축소 (회복 시 자동 원복)
15% → 거래 중단 (수동 재개만 가능)
```

---

## 현재 전략 조합

```yaml
scanner: new_listing           # 신규상장 + 거래량/변동성 + BTC·ETH 추세
position: momentum_scalper     # 단기 EMA + RSI + VWAP + 거래량 (5분봉, 최대 90분)
asset: fixed_ratio             # 리스크 1% + 5% 캡 + 단리 + 드로다운 3단계
```

---

## 새 전략 추가 방법

1. 해당 레이어의 `base.py`를 상속하는 클래스 작성
2. `config/strategy/` 해당 YAML에 파라미터 추가
3. 전략 레지스트리에 등록

```python
# 예: 새로운 Position 전략 추가
# src/strategy/position/my_strategy.py

class MyStrategy(BasePositionStrategy):
    def evaluate(self, symbol, indicators, current_position):
        # 전략 로직 구현
        ...
        return PositionSignal(...)

    def get_default_params(self):
        return {"param1": 10, "param2": 20}
```

```yaml
# config/strategy/position.yaml 에 추가
strategies:
  my_strategy:
    param1: 15
    param2: 25
```
