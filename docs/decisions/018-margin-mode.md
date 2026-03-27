# ADR-018: 마진 모드 선택

## 상태

`결정됨`

## 날짜

2026-03-25

## 맥락

USDT 무기한 선물에서 포지션별 마진 모드를 결정해야 한다.

## 결정

**Isolated Margin (격리 마진)** 채택.

- 포지션별로 마진이 격리되어, 한 종목이 청산되어도 다른 포지션에 영향 없음
- 3개 독립 포지션 운영에 적합
- 포지션 진입 시 Bybit API로 해당 심볼의 마진 모드를 Isolated로 설정

```python
# 포지션 진입 전 마진 모드 설정
client.set_margin_mode(category="linear", symbol=symbol, tradeMode=1)  # 1=Isolated
client.set_leverage(category="linear", symbol=symbol, buyLeverage=str(leverage), sellLeverage=str(leverage))
```

## 영향

- Order Manager: 진입 전 마진 모드 + 레버리지 설정 API 호출 추가
- Paper Engine: Isolated 마진 시뮬레이션 (포지션별 마진 분리)
- 청산가 계산이 포지션별 마진 기준으로 결정됨
