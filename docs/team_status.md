# Optimization Cycle Log

78 사이클의 반복 최적화 기록. 핵심 변경만 요약.

## 최종 결과 (Cycle 78, 2026-03-29 15:46 KST)
- **187 trades, WR 97.3%, PnL +1.116 USDT (+5.32%)**
- 모든 파라미터 안정 수렴

## 파라미터 변경 이력

| Cycle | 시간 | 변경 | 결과 |
|-------|------|------|------|
| 1 | 03/28 12:42 | range 2.5→1.5, min_range 1.0→0.8 | Util 2%→5% |
| 2 | 03/28 12:56 | spacing 0.60→0.50, target 0.60→0.50 | TP율↑ but fee margin thin |
| 3 | 03/28 13:30 | range 1.5→1.0, spacing 0.50→0.55 | 리센터 과다 (-0.014) |
| 4 | 03/28 14:00 | range 1.0→1.2, recenter_th 2.0→3.0 | **WR 100%, recenters↓** |
| 7 | 03/28 15:13 | recenter_interval 60→180 | **PnL 3.7x 향상** |
| 12 | 03/28 17:06 | recenter: 오픈 포지션 유지 (코드) | 강제 청산 손실 제거 |
| 14 | 03/28 17:57 | recenter fallback: skip 대신 유지 | -0.025 반복 손실 제거 |
| 18 | 03/28 19:20 | max_symbols 5→8 | **거래 빈도 +88%** |
| 24 | 03/28 21:23 | recenter index 충돌 방지 (코드) | BEATUSDT -0.063 버그 수정 |
| 25-78 | 03/28 21:44 ~ 03/29 15:46 | 관찰 | WR 97%, 안정 수익 |

## 핵심 코드 변경 (Config 이외)

| Cycle | 파일 | 변경 |
|-------|------|------|
| 12 | grid_bias.py `_do_recenter` | 오픈 포지션 유지, PENDING만 교체 |
| 14 | grid_bias.py `_do_recenter` | fallback에서도 포지션 유지 |
| 24 | grid_bias.py `_do_recenter` | kept_indices와 새 레벨 index 충돌 제거 |

## 이전 버그 수정 (Cycle 이전)

| 수정 | 파일 | 설명 |
|------|------|------|
| level_id=0 매핑 | grid_manager.py | `(symbol, level_index)` 복합키 |
| Same-candle Fill+TP | grid_bias.py | `just_filled_indices` 필터 |
| Margin close_by_key | paper_executor.py | 정확한 포지션 닫기 |
| Balance DB sync | order/process.py | 그리드 모드 balance 동기화 |
| Entry fee 추적 | grid_manager.py | `_level_entry_fees` dict |
| DB 연결 스팸 | main.py | watchdog DB 캐싱 |

## 전체 로그
78사이클 상세 리포트: `docs/archive/iterations/`
