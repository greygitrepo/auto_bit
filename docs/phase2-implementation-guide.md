# Phase 2 구현 가이드: 청산 구조 개선

- 작성일: 2026-03-26
- 근거: `docs/performance-analysis-2026-03-26.md`
- 상태: **Phase 2 검증 완료** (2026-03-26 20:15), 31건 거래 분석 결과 모든 검증 포인트 PASS

---

## 0. Phase 1 이후 현황 요약 (48건, 2.8시간 운영)

| 지표 | Phase 1 이전 (104건) | Phase 1 이후 (48건) | 판단 |
|------|---------------------|-------------------|------|
| 승률 | 31.7% | 22.9% | 악화 |
| Long 승률 | 38% | 23.8% | 악화 |
| Short 승률 | 26% | 22.2% | 미미한 변화 |
| SL 비율 | 40% (42건) | 39.6% (19건) | 변화 없음 |
| SL 총손실 | -3.31 USDT | -1.6136 USDT | 여전히 최대 손실원 |
| Trailing Stop | N/A | 16건, +0.1789 | 유일한 수익원 |
| TP 도달 | 8건 (7.7%) | 1건 (2.1%) | 악화 |

**핵심 판단**: Phase 1 진입 품질 개선은 효과 미확인. 그러나 Phase 2의 SL/트레일링 문제는 Phase 1과 독립적이며, 데이터에서 명확히 확인됨.

---

## 1. Task 2-1: SL/TP 비율 조정

### 문제

- SL(ATR×1.5)이 5분봉 스캘핑에서 너무 좁아 노이즈에 빈번히 걸림
- 48건 중 SL 19건(39.6%), 총손실 -1.6136 (전체 실현 손실의 112%)
- TP 도달률 2.1% (1건/48건) — R:R 1.5:1이지만 TP에 거의 도달 못함
- SL 평균 손실: Buy -0.087, Sell -0.082

### 변경 대상

**파일**: `config/strategy/position.yaml` (39~48행)

```yaml
# 현재값
exit:
  stop_loss:
    type: atr
    atr_period: 14
    atr_multiplier: 1.5    # ← 변경
    min_pct: 0.3            # ← 변경
    max_pct: 3.0

  take_profit:
    type: risk_reward
    risk_reward_ratio: 1.5  # ← 변경
```

### 변경 내용

```yaml
# 변경 후
exit:
  stop_loss:
    type: atr
    atr_period: 14
    atr_multiplier: 2.0     # 1.5 → 2.0 (노이즈 회피 폭 확대)
    min_pct: 0.5             # 0.3 → 0.5 (최소 SL 거리 확대)
    max_pct: 3.0

  take_profit:
    type: risk_reward
    risk_reward_ratio: 2.0   # 1.5 → 2.0 (SL 넓어진 만큼 TP도 확대)
```

### 변경 근거

| 파라미터 | 현재 | 변경 | 이유 |
|----------|------|------|------|
| `atr_multiplier` | 1.5 | 2.0 | ATR×1.5는 5분봉 노이즈 범위 내. ×2.0으로 확대하면 SL 발동 빈도 감소 기대 |
| `min_pct` | 0.3% | 0.5% | 변동성 낮은 종목에서 0.3% SL은 스프레드+수수료 수준. 최소 마진 확보 |
| `risk_reward_ratio` | 1.5 | 2.0 | SL 폭 확대에 따라 R:R을 유지하면 TP 거리도 자동 확대. 승률 22.9%에서는 R:R 2.0 이상 필요 |

### 적용 방법

1. `config/strategy/position.yaml` 수정 (위 값 3개)
2. 코드 변경 없음 — `MomentumScalper.calculate_sl_tp()`가 yaml에서 동적으로 읽음 (`momentum_scalper.py:512~515`)
3. 시스템 재시작 필요 (config는 시작 시 로드)

### 검증 포인트

- [ ] SL 발동 비율이 39.6% 이하로 감소하는지
- [ ] TP 도달률이 2.1%에서 개선되는지
- [ ] 개별 SL 손실 크기가 커져도 총 SL 손실이 줄어드는지 (빈도 감소 > 단건 손실 증가)

---

## 2. Task 2-2: 트레일링 스탑 Entry Price 보호 로직

### 문제

- `activation_r: 0.6` → 수익 0.6R에서 트레일링 활성화
- 활성화 시 `trailing_sl = current_price - callback_distance`로 설정
- `callback_distance = ATR × 0.7`이 0.6R보다 클 수 있음 → **trailing_sl이 entry price 아래로 내려감**
- 결과: "수익 보호"가 아닌 "손실 발생" 상태에서 청산
- 현재 데이터: trailing stop 16건 중 8건 손실 (50%)

### 수치 예시 (현재 문제)

```
Entry: 100.00 (LONG)
SL distance: 0.50 (ATR×1.5 기준)
activation_r: 0.6 → activation_price: 100.30 (entry + 0.30)

가격이 100.30 도달 → 트레일링 활성화
callback_distance = ATR × 0.7 = 0.35 (예시)
trailing_sl = 100.30 - 0.35 = 99.95 ← entry(100.00) 아래!

→ 가격이 99.95까지 하락하면 -0.05 손실로 청산
```

### 변경 대상

**파일**: `src/strategy/position/momentum_scalper.py`
- `TrailingStopManager.update()` 메서드 (42~122행)

**파일**: `config/strategy/position.yaml` (50~52행)
- `activation_r` 값 조정

### 변경 내용

#### 2-2a. 코드 변경: trailing_sl의 entry price 보호 (momentum_scalper.py)

`TrailingStopManager.update()` 메서드에서 trailing_sl 설정/갱신 시 entry price를 floor/ceiling으로 사용.

**LONG 측 (65~93행)** — trailing_sl 설정하는 3곳에 보호 추가:

```python
# 현재 (72행)
state.trailing_sl = current_price - callback_distance

# 변경 — entry_price를 floor로 사용
state.trailing_sl = max(current_price - callback_distance, state.entry_price)
```

```python
# 현재 (84행, high-water mark 갱신 시)
state.trailing_sl = current_price - callback_distance

# 변경
state.trailing_sl = max(current_price - callback_distance, state.entry_price)
```

**SHORT 측 (94~120행)** — 동일하게 보호 추가:

```python
# 현재 (101행)
state.trailing_sl = current_price + callback_distance

# 변경 — entry_price를 ceiling으로 사용
state.trailing_sl = min(current_price + callback_distance, state.entry_price)
```

```python
# 현재 (112행, low-water mark 갱신 시)
state.trailing_sl = current_price + callback_distance

# 변경
state.trailing_sl = min(current_price + callback_distance, state.entry_price)
```

**TrailingStopState에 entry_price 필드 추가 필요:**

**파일**: `src/strategy/position/base.py` (68~83행)

```python
# 추가 필드
entry_price: float = 0.0
```

**create_initial_state()에서 entry_price 저장 (momentum_scalper.py:124~151행):**

```python
# 현재 (148행)
return TrailingStopState(
    active=False,
    activation_price=activation_price,
)

# 변경
return TrailingStopState(
    active=False,
    activation_price=activation_price,
    entry_price=entry_price,
)
```

#### 2-2b. 설정 변경: activation_r 상향 (position.yaml)

```yaml
# 현재
trailing_stop:
  activation_r: 0.6
  callback_atr_multiplier: 0.7

# 변경
trailing_stop:
  activation_r: 1.0              # 0.6 → 1.0 (1R 수익 확보 후 활성화)
  callback_atr_multiplier: 0.5   # 0.7 → 0.5 (보호 폭 축소하여 수익 더 보전)
```

| 파라미터 | 현재 | 변경 | 이유 |
|----------|------|------|------|
| `activation_r` | 0.6 | 1.0 | 0.6R 활성화는 callback_distance에 의해 entry 아래로 빠짐. 1.0R이면 entry 보호 로직과 함께 안전 마진 확보 |
| `callback_atr_multiplier` | 0.7 | 0.5 | activation_r을 높였으므로 callback을 줄여 수익 보전율 향상. ATR×0.5 = SL 폭의 ~25% 수준 |

### 변경 후 수치 예시

```
Entry: 100.00 (LONG)
SL distance: 1.00 (ATR×2.0, Task 2-1 반영)
activation_r: 1.0 → activation_price: 101.00

가격이 101.00 도달 → 트레일링 활성화
callback_distance = ATR × 0.5 = 0.25
trailing_sl = max(101.00 - 0.25, 100.00) = 100.75

→ 최악의 경우에도 +0.75 수익 확보 (entry 아래로 절대 안 내려감)
```

### 적용 순서

1. `src/strategy/position/base.py` — `TrailingStopState`에 `entry_price` 필드 추가
2. `src/strategy/position/momentum_scalper.py` — `create_initial_state()`에서 `entry_price` 전달
3. `src/strategy/position/momentum_scalper.py` — `update()`에서 `max()`/`min()` 보호 로직 4곳 적용
4. `config/strategy/position.yaml` — `activation_r`, `callback_atr_multiplier` 값 변경
5. 시스템 재시작

### 검증 포인트

- [ ] trailing stop 청산 시 PnL이 항상 >= 0 인지 (entry price 보호 동작 확인)
- [ ] trailing stop 승률이 50% (현재) → 80%+ 로 개선되는지
- [ ] trailing stop 평균 수익이 Buy +0.017 / Sell +0.005 에서 개선되는지

---

## 3. Task 2-1 + 2-2 연동 주의사항

두 변경은 반드시 **동시에 적용**해야 합니다.

| SL 넓힘 (2-1) 단독 | 트레일링 수정 (2-2) 단독 | 동시 적용 |
|-------|---------|---------|
| SL 넓어짐 → 1R 커짐 → activation_r=0.6에서 더 큰 callback → entry 아래 문제 악화 | SL 좁은 상태에서 activation_r=1.0이면 활성화 기회 감소 | SL 넓힘 + activation_r 상향 + entry 보호 = 정상 동작 |

---

## 4. 적용 체크리스트

```
[x] 1. base.py: TrailingStopState에 entry_price 필드 추가
[x] 2. momentum_scalper.py: create_initial_state()에서 entry_price 전달
[x] 3. momentum_scalper.py: update() LONG 측 trailing_sl에 max(_, entry_price) 적용 (2곳)
[x] 4. momentum_scalper.py: update() SHORT 측 trailing_sl에 min(_, entry_price) 적용 (2곳)
[x] 5. position.yaml: atr_multiplier 1.5 → 2.0
[x] 6. position.yaml: min_pct 0.3 → 0.5
[x] 7. position.yaml: risk_reward_ratio 1.5 → 2.0
[x] 8. position.yaml: activation_r 0.6 → 1.0
[x] 9. position.yaml: callback_atr_multiplier 0.7 → 0.5
[x] 10. 테스트 실행: pytest tests/ -v (11/11 passed)
[x] 11. 시스템 재시작 (2026-03-26, PID=2212967, paper mode)
[x] 12. 30건 이상 거래 후 검증 포인트 확인 (2026-03-26 20:15, 31건 검증 완료 → docs/phase2-verification-report.md)
```

---

## 5. 롤백 계획

문제 발생 시 즉시 롤백 가능:

- **설정만 롤백**: `position.yaml`의 5개 값을 원래대로 복원
- **코드+설정 롤백**: `git stash` 또는 `git checkout -- src/ config/`
- `base.py`의 `entry_price` 필드 추가는 기본값 0.0이므로 기존 동작에 영향 없음
