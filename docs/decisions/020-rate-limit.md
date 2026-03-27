# ADR-020: Bybit API Rate Limit 관리

## 상태

`결정됨`

## 날짜

2026-03-25

## 맥락

스캐너가 REST API를 사용하여 30개 심볼의 캔들 데이터를 조회한다.
Bybit V5 API의 Rate Limit을 초과하지 않도록 관리해야 한다.

## 결정

### Bybit V5 Rate Limit

```
Market 엔드포인트: 분당 120회 (인증 불필요)
Trade 엔드포인트: 분당 10회 (주문 관련)
Position 엔드포인트: 분당 10회
```

### 스캐너 1회 실행 시 호출량

```
GET /v5/market/instruments-info  ×1  (전체 심볼 정보)
GET /v5/market/tickers           ×1  (전체 심볼 티커)
GET /v5/market/kline             ×30 (후보 캔들)
GET /v5/market/kline             ×2  (BTC/ETH — 캐시 미스 시)
─────────────────────────────────────
합계: 최대 34회 / 분당 한도 120회 → 여유 충분
```

### 관리 방식: 간단한 슬롯 기반 제한

```python
class RateLimiter:
    """슬롯 기반 Rate Limiter"""
    def __init__(self, max_calls_per_minute=100):  # 여유분 두고 100으로 설정
        self.max_calls = max_calls_per_minute
        self.calls = []  # 타임스탬프 리스트

    async def acquire(self):
        now = time.time()
        # 1분 이전 기록 제거
        self.calls = [t for t in self.calls if now - t < 60]
        if len(self.calls) >= self.max_calls:
            wait = 60 - (now - self.calls[0])
            await asyncio.sleep(wait)
        self.calls.append(time.time())
```

분당 120회 한도에서 100회로 제한하여 20회 여유분 확보.
스캐너가 최대 34회 사용하므로 나머지 66회는 주문/포지션 조회 등에 사용.

### 캔들 데이터 캐싱

BTC/ETH는 상시 WebSocket으로 수집하므로 REST 호출 불필요.
후보 30개 심볼의 캔들 조회 시 1초 간격으로 호출하여 부하 분산.

```python
for symbol in candidates:
    candles = await fetch_kline(symbol)
    await asyncio.sleep(0.3)  # 호출 간 300ms 대기
```

## 영향

- Data Collector: RateLimiter를 통해 모든 REST 호출 관리
- 스캐너 1회 실행 소요 시간: 30 × 0.3초 = ~10초 (허용 범위)
- 주문 API(Trade 엔드포인트)는 별도 Rate Limit이므로 분리 관리
