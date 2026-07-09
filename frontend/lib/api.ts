// QuantFolio API 클라이언트 — HttpOnly 쿠키 기반 인증.
// 토큰은 JS 에서 접근하지 않으며, 모든 요청에 credentials: "include" 로 쿠키를 전송한다.

/** 백엔드 API 기본 주소. 빌드 환경변수가 없으면 로컬 개발 서버로 폴백한다. */
const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/** 외부(WS 등)에서 재사용할 수 있도록 노출한 API 기본 주소. */
export const API_BASE_URL = API_BASE;

/**
 * FastAPI 에러 응답(detail)을 사람이 읽을 수 있는 메시지로 변환한다.
 * 422 검증 오류는 detail 이 [{loc, msg, ...}] 배열이므로 msg 들을 결합한다.
 * @param detail 응답 본문의 detail 필드(문자열 또는 검증오류 배열)
 * @param status HTTP 상태 코드(메시지를 만들 수 없을 때 폴백에 사용)
 * @returns 사용자에게 보여줄 에러 메시지
 */
function formatError(detail: unknown, status: number): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((d) => (d && typeof d === "object" && "msg" in d ? String((d as { msg: unknown }).msg) : null))
      .filter(Boolean);
    if (msgs.length) return msgs.join(", ");
  }
  return `요청 실패 (${status})`;
}

/**
 * 공통 fetch 래퍼. 쿠키를 동봉(credentials:"include")하고, JSON 응답을 파싱하며,
 * 실패 시 정규화된 에러 메시지로 throw 한다.
 * @typeParam T 기대하는 응답 본문 타입
 * @param path API 경로(API_BASE 에 이어 붙는다)
 * @param init fetch 옵션(method/body/headers 등)
 * @returns 파싱된 응답 본문. 204 응답이면 undefined
 * @throws Error 응답이 2xx 가 아닐 때
 */
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init.headers as Record<string, string>),
  };

  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers,
    credentials: "include",
  });
  if (res.status === 204) return undefined as T;
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(formatError(body?.detail, res.status));
  }
  return res.json() as Promise<T>;
}

// ─────────────────────────── 타입 ───────────────────────────
/** 증권사 식별자. kis=국내(한국투자증권), toss=해외(토스증권). */
export type Broker = "kis" | "toss";

/** 브로커 표시 정보(주문 브로커 표기 등 화면 공통 사용). */
export const BROKER_INFO: Record<Broker, { name: string; market: string }> = {
  kis: { name: "한국투자증권", market: "국내주식" },
  toss: { name: "토스증권", market: "국내·해외주식" },
};

export interface UserOut {
  id: number;
  email: string;
  /** 공유 전략 목록에 표시할 닉네임. 미설정이면 null. */
  display_name: string | null;
  broker: Broker;
  kis_account_no: string | null;
  has_kis_credentials: boolean;
  /** 통합 시세(토스) 연동 여부. true면 워치리스트가 국내+해외를 토스로 조회. */
  has_toss_quote: boolean;
}

/** 현재가 시세(브로커 공통). 토스 등 일부 브로커는 가격 외 항목이 0일 수 있다. */
export interface Quote {
  symbol: string;
  price: number;
  change: number;
  change_rate: number;
  volume: number;
  high: number;
  low: number;
  open: number;
  /** 통화 코드(KRW/USD 등). 해외주식 소수 가격 표시에 사용. */
  currency: string;
}

/** 전략 유형 식별자. */
export type StrategyType =
  | "sma_crossover"
  | "ema_crossover"
  | "rsi"
  | "macd"
  | "bollinger"
  | "breakout"
  | "momentum"
  | "zscore"
  | "disparity"
  | "donchian_squeeze"
  | "trix"
  | "obv_trend"
  | "atr_trailing"
  | "volatility_breakout"
  | "keltner"
  | "stochastic"
  | "custom"
  | "rebalance";

/** 모든 전략 유형이 공유하는 공통 파라미터(종목·자본·수수료·리스크 청산). */
export interface BaseConfig {
  symbol: string;
  cash: number;
  /** 위탁수수료율 0~0.01 (매수·매도 양방향). 예: 0.00015 = 0.015%. */
  fees: number;
  /** 증권거래세율 0~0.01 (매도 시에만). 예: 0.002 = 0.20%. */
  tax: number;
  /** 손절 비율 0~1 (예: 0.05 = 5%). null/미설정이면 비활성. */
  stop_loss_pct?: number | null;
  /** 익절 비율 0~1. null/미설정이면 비활성. */
  take_profit_pct?: number | null;
  /** 트레일링 스탑 비율 0~1. null/미설정이면 비활성. */
  trailing_stop_pct?: number | null;
  /** 체결 시점: "next_close"=익일 종가(기본) / "same_close"=당일 종가. */
  fill_mode?: "next_close" | "same_close";
  /** 편도 슬리피지(bps, 0~200). 5=0.05%. */
  slippage_bps?: number;
  /** 변동성 비례 슬리피지 스케일(0~5, 0=고정 bps). */
  slippage_vol_scale?: number;
  /** 위험조정지표(Sharpe·Sortino)용 무위험수익률(연, 0~0.2). */
  risk_free_rate?: number;
}

export interface SmaConfig extends BaseConfig {
  type: "sma_crossover";
  fast: number;
  slow: number;
}
export interface EmaConfig extends BaseConfig {
  type: "ema_crossover";
  fast: number;
  slow: number;
}
export interface RsiConfig extends BaseConfig {
  type: "rsi";
  period: number;
  lower: number;
  upper: number;
}
export interface MacdConfig extends BaseConfig {
  type: "macd";
  fast: number;
  slow: number;
  signal: number;
}
export interface BollingerConfig extends BaseConfig {
  type: "bollinger";
  period: number;
  num_std: number;
}
export interface BreakoutConfig extends BaseConfig {
  type: "breakout";
  period: number;
}
export interface MomentumConfig extends BaseConfig {
  type: "momentum";
  lookback: number;
}
export interface ZScoreConfig extends BaseConfig {
  type: "zscore";
  period: number;
  entry: number;
}
export interface DisparityConfig extends BaseConfig {
  type: "disparity";
  period: number;
  lower: number;
  upper: number;
}
export interface DonchianSqueezeConfig extends BaseConfig {
  type: "donchian_squeeze";
  period: number;
  bb_mult: number;
  kc_mult: number;
}
export interface TrixConfig extends BaseConfig {
  type: "trix";
  period: number;
  signal_period: number;
}
export interface ObvTrendConfig extends BaseConfig {
  type: "obv_trend";
  period: number;
}
export interface AtrTrailingConfig extends BaseConfig {
  type: "atr_trailing";
  period: number;
  atr_period: number;
  k: number;
}
export interface VolatilityBreakoutConfig extends BaseConfig {
  type: "volatility_breakout";
  k: number;
}
export interface KeltnerConfig extends BaseConfig {
  type: "keltner";
  ema_period: number;
  atr_period: number;
  mult: number;
}
export interface StochasticConfig extends BaseConfig {
  type: "stochastic";
  k_period: number;
  d_period: number;
  lower: number;
  upper: number;
}

/** 사용자 정의 전략 규칙의 피연산자(kind 에 따라 필요한 필드만 사용). */
export type OperandKind =
  | "price"
  | "const"
  | "sma"
  | "ema"
  | "rsi"
  | "macd_line"
  | "macd_signal";

export interface Operand {
  kind: OperandKind;
  /** price 일 때 */
  source?: "close" | "open" | "high" | "low" | "volume" | null;
  /** const 일 때 */
  value?: number | null;
  /** sma/ema/rsi 일 때 */
  period?: number | null;
  /** macd_line/macd_signal 일 때 */
  fast?: number | null;
  slow?: number | null;
  signal?: number | null;
}

/** 비교 연산자. */
export type CompareOp = ">" | "<" | ">=" | "<=";

/** 좌·우 피연산자를 비교 연산자로 묶은 단일 조건(논리식의 잎 노드). */
export interface Condition {
  left: Operand;
  op: CompareOp;
  right: Operand;
}

/** 논리 결합자: AND(모두 충족) / OR(하나라도 충족). */
export type Combinator = "and" | "or";

/** 조건/하위 그룹을 AND·OR 로 결합하는 논리 그룹(중첩 가능). */
export interface ConditionGroup {
  combinator: Combinator;
  children: RuleNode[];
}

/** 논리식 노드 = 단일 조건 또는 (중첩) 그룹. */
export type RuleNode = Condition | ConditionGroup;

/** 노드가 그룹인지 판별(잎 조건은 children 이 없다). */
export function isGroup(node: RuleNode): node is ConditionGroup {
  return (node as ConditionGroup).children !== undefined;
}

/**
 * 사용자 정의 전략(no-code 룰 빌더). 진입/청산은 각각 하나의 논리식(AND/OR 중첩 그룹).
 */
export interface CustomConfig extends BaseConfig {
  type: "custom";
  entry: ConditionGroup;
  exit: ConditionGroup;
}

/**
 * 종합점수 카테고리 가중치(합=1.0). method="score" 에서 사용.
 * quality(ROE·저부채·FCF)는 OpenDART 재무데이터가 필요하며, 키가 없으면 중립 처리된다.
 */
export interface FactorWeights {
  momentum: number;
  value: number;
  lowvol: number;
  quality: number;
  growth: number;
}

/** 리밸런싱 종목 선정 규칙. */
export interface RebalanceSelection {
  /**
   * "momentum": 룩백 수익률 상위 top_n / "all": universe 전체 /
   * "score": 멀티팩터 종합점수 상위 top_n(factor_weights 로 가중) /
   * "custom": 각 종목에 사용자 정의 진입·청산 규칙을 적용해 현재 편입 국면인 종목만 동일비중.
   */
  method: "momentum" | "all" | "score" | "custom";
  /** 모멘텀 측정 봉 수. */
  lookback: number;
  /** 선정 종목 수(momentum/score). */
  top_n: number;
  /** method="score": 종합점수 카테고리 가중치(합=1.0). */
  factor_weights?: FactorWeights;
  /** method="custom": 편입(매수) 논리식. 각 종목에 독립 적용. */
  entry?: ConditionGroup;
  /** method="custom": 청산(매도) 논리식. 각 종목에 독립 적용. */
  exit?: ConditionGroup;
  /** 후보풀 규칙. 지수 소스(PIT) 사용 시 생존편향 제거. 미설정이면 fixed(아래 universe). */
  universe_rule?: UniverseRule;
}

/**
 * 후보풀(universe) 소스 규칙.
 * - source="fixed"(기본): 아래 universe 목록을 고정 후보풀로 사용.
 * - source=지수명: 각 리밸런싱 시점의 실제 지수 구성종목(PIT)을 후보풀로 사용 → 생존편향 제거.
 *   type 을 지정하면 그 후보풀에서 상대강도 상위 pick 종목만 추려 선정에 넘긴다.
 */
export interface UniverseRule {
  source: "fixed" | "KOSPI200" | "KOSPI100" | "KRX300";
  /** 동적 축소 방식. "momentum"=상대강도. 미설정이면 축소 없이 전체 구성종목 사용. */
  type?: "momentum";
  /** 상대강도 측정 봉 수. */
  lookback?: number;
  /** 상대강도 상위 몇 종목을 후보로 남길지. */
  pick?: number;
  /** 유동성 필터: 시가총액(억 원) 하한. 미만 종목을 후보풀에서 제외. 미설정이면 필터 없음. */
  min_market_cap?: number | null;
}

/** 현금화 오버레이(레짐 필터): 기준지수가 이동평균 아래면 전량 청산·매수 중단. */
export interface RegimeFilter {
  /** 오버레이 사용 여부. */
  enabled: boolean;
  /** 추세 판정 기준지수. */
  index: "KOSPI" | "KOSDAQ";
  /** 추세 판정 이동평균 기간(거래일). */
  ma_period: number;
  /** 재진입 히스테리시스 버퍼: 지수가 MA×(1+이 값) 이상 회복해야 재편입(휩쏘 억제). 0 이면 즉시. */
  reentry_buffer_pct?: number;
  /** 청산 히스테리시스 버퍼: 지수가 MA×(1−이 값) 미만일 때 청산. 0 이면 MA 하회 즉시. */
  exit_buffer_pct?: number;
}

/**
 * 포트폴리오 리스크 레이어(P1-2). 선정·비중 산정 이후 목표비중에 적용되는 위험 통제.
 * 각 하위 항목은 개별 optional(미설정=해당 통제 비활성).
 */
export interface RiskLayer {
  /** 단일 종목 목표비중 상한(0~1). 초과분은 상한 미만 종목에 비례 재분배(여력 없으면 현금). drift_band 보다 커야 함. */
  max_position_pct?: number | null;
  /** 목표 연율 변동성(예: 0.15=15%). 실현변동성이 넘으면 총투자비중 디레버리징. */
  target_vol?: number | null;
  /** 변동성 타겟팅용 실현변동성 측정 거래일. */
  vol_lookback?: number;
  /** 변동성 타겟팅 총투자비중 상한(≤1.0=차입 없음). */
  max_leverage?: number;
  /** 고점 대비 낙폭 킬스위치 임계(예: 0.25=−25%). 초과 시 전량 청산·현금화. */
  mdd_kill_pct?: number | null;
  /** 킬스위치 발동 후 재가동까지 쿨다운 거래일. */
  mdd_rearm_days?: number;
}

/**
 * 주기적 포트폴리오 리밸런싱 전략. 단일 종목 전략과 달리 다종목 universe 를
 * 운용하며 BaseConfig(종목·리스크 청산)를 상속하지 않는다.
 */
export interface RebalanceConfig {
  type: "rebalance";
  /** 후보 종목코드 목록. */
  universe: string[];
  selection: RebalanceSelection;
  /** 선정 종목 비중 방식. "equal"=동일비중 / "score"=순위 기반 선형 가중(method="score" 전용). */
  weighting: "equal" | "score";
  cadence: "daily" | "weekly" | "monthly" | "quarterly";
  /** weekly: 0=월~4=금. */
  rebalance_weekday?: number | null;
  /** monthly: 1~28(영업일 보정). */
  rebalance_dom?: number | null;
  /** 실행 시각 "HH:MM" (KST). */
  rebalance_time: string;
  /** 목표 대비 비중 편차 임계(0~1). 초과 종목만 매매. */
  drift_band_pct: number;
  /** 현금화 오버레이(레짐 필터). null/미설정이면 미적용(항상 100% 투자). */
  regime_filter?: RegimeFilter | null;
  /** 포트폴리오 리스크 레이어(집중 한도·변동성 타겟팅·MDD 킬스위치). null/미설정이면 미적용. */
  risk_layer?: RiskLayer | null;
  /** 이 전략이 운용할 배정 자본(원). */
  capital: number;
  /** 위탁수수료율. */
  fees: number;
  /** 증권거래세율(매도 시). */
  tax: number;
  /** 체결 시점: "next_close"=익일 종가(기본) / "same_close"=당일 종가. */
  fill_mode?: "next_close" | "same_close";
  /** 편도 슬리피지(bps, 0~200). 매수 +slip·매도 −slip. */
  slippage_bps?: number;
  /** 변동성 비례 슬리피지 스케일(0~5, 0=고정 bps). */
  slippage_vol_scale?: number;
  /** 위험조정지표(Sharpe·Sortino)용 무위험수익률(연, 0~0.2). */
  risk_free_rate?: number;
  /** 벤치마크 상대성과(alpha·beta·IR) 산출용 지수. 기본 KOSPI200. */
  benchmark_index?: "KOSPI200" | "KOSPI" | "KOSDAQ";
}

export type StrategyConfig =
  | SmaConfig
  | EmaConfig
  | RsiConfig
  | MacdConfig
  | BollingerConfig
  | BreakoutConfig
  | MomentumConfig
  | ZScoreConfig
  | DisparityConfig
  | DonchianSqueezeConfig
  | TrixConfig
  | ObvTrendConfig
  | AtrTrailingConfig
  | VolatilityBreakoutConfig
  | KeltnerConfig
  | StochasticConfig
  | CustomConfig
  | RebalanceConfig;

export interface Strategy {
  id: number;
  name: string;
  /** 전략 설명(자유 텍스트). 미설정이면 null. */
  description: string | null;
  config: StrategyConfig;
  status: "draft" | "backtested" | "live";
  /** 대표 백테스트 ID(공유 시 성과 표시용). 미지정이면 null. */
  featured_backtest_id: number | null;
  /** 공유 여부(켜면 다른 사용자가 공유 목록에서 열람·복사·좋아요 가능). */
  is_shared: boolean;
  /** 즐겨찾기(목록 상단 고정). */
  is_favorite: boolean;
  /** 사용자 지정 표시 순서(작을수록 위). */
  sort_order: number;
  created_at: string;
  updated_at: string;
}

/** 공유 시 노출되는 경량 백테스트 성과 요약. */
export interface BacktestSummary {
  id: number;
  total_return: number | null;
  mdd: number | null;
  sharpe: number | null;
  period_start: string;
  period_end: string;
}

/** 공유 전략 목록 항목(작성자·좋아요·설명·대표 백테스트 포함). */
export interface SharedStrategy {
  id: number;
  name: string;
  /** 전략 설명. 미설정이면 null. */
  description: string | null;
  config: StrategyConfig;
  /** 작성자 닉네임(미설정이면 "익명"). */
  author_name: string;
  like_count: number;
  /** 현재 사용자가 좋아요를 눌렀는지. */
  liked_by_me: boolean;
  /** 현재 사용자 본인의 전략인지(좋아요 비활성 처리용). */
  is_mine: boolean;
  /** 대표 백테스트 성과(미지정이면 null). */
  backtest: BacktestSummary | null;
  created_at: string;
}

/** 공유 목록 필터·정렬 옵션. */
export interface SharedQuery {
  /** 전략 제목 부분일치. */
  q?: string;
  /** 종목코드(단일종목·리밸런싱 universe). */
  symbol?: string;
  /** 정렬 기준(기본 likes). */
  sort?: "likes" | "name" | "recent";
}

/** 좋아요 토글 결과. */
export interface LikeResult {
  like_count: number;
  liked_by_me: boolean;
}

/** 리밸런싱 백테스트의 개별 체결 로그(매수/매도 + 그 순간 수익률). */
export interface BacktestTrade {
  /** 체결일(ISO). */
  t: string;
  /** 종목코드. */
  symbol: string;
  side: "buy" | "sell";
  /** 원화 거래대금(근사). */
  amount: number;
  /** 체결가(종가). */
  price: number | null;
  /** 그 순간의 포트폴리오 누적수익률(시작=0). */
  port_return: number | null;
  /** 매도 시 해당 포지션의 원가 대비 보유수익률(매수는 null). */
  position_return: number | null;
  /** 사유: 'rebalance'(정기) 또는 'regime_exit'(레짐 현금화). */
  reason: string;
}

/** 팩터 1개의 성과귀속 지표(P1-1). */
export interface FactorIC {
  /** 평균 IC(팩터 점수 vs 다음 구간 수익률의 순위상관). 예측력의 부호·강도. */
  ic_mean: number | null;
  /** IC IR = mean(IC)/std(IC) × √(연 리밸런싱 횟수). 예측의 일관성. */
  ic_ir: number | null;
  /** IC>0 비율(방향 적중률). */
  ic_hit: number | null;
  /** 팩터 상위 1/3 − 하위 1/3 롱숏 포트폴리오의 누적수익(이 구간 기여). */
  ls_return: number | null;
  /** 유효 구간(리밸런싱 인접쌍) 수. */
  n: number;
}

export interface BacktestResult {
  total_return: number | null;
  mdd: number | null;
  sharpe: number | null;
  /** 하방편차 기반 위험조정수익(무위험수익률 초과 기준). */
  sortino?: number | null;
  /** 연평균 복리수익률(리밸런싱 백테스트에서 제공). */
  cagr?: number | null;
  /** 벤치마크(KOSPI200) 대비 연율 초과수익(Jensen alpha). 리밸런싱 백테스트. */
  alpha?: number | null;
  /** 벤치마크 대비 민감도(시장 베타). 리밸런싱 백테스트. */
  beta?: number | null;
  /** 정보비율 = 초과수익 평균 / 추적오차. 리밸런싱 백테스트. */
  information_ratio?: number | null;
  /** 추적오차(초과수익의 연율 표준편차). 리밸런싱 백테스트. */
  tracking_error?: number | null;
  /** 벤치마크(KOSPI200) 구간 누적수익률. 리밸런싱 백테스트. */
  benchmark_return?: number | null;
  /** 벤치마크 대비 누적 초과수익률(total_return − benchmark_return). */
  excess_return?: number | null;
  /** 종목단위 승률(리밸런싱 전략에는 없음 → null). */
  win_rate: number | null;
  num_trades: number;
  /** 리밸런싱 실행 횟수(리밸런싱 백테스트). */
  num_rebalances?: number;
  /** MDD 킬스위치(risk_layer) 발동 횟수(리밸런싱 백테스트). */
  num_kills?: number;
  /**
   * 팩터별 성과귀속·IC/IR(P1-1, method="score" 전략만; 그 외 {} 또는 미포함).
   * 키: score_momentum/value/lowvol/quality/growth + 종합 score.
   */
  factor_ic?: Record<string, FactorIC>;
  /** 평균 회전율 Σ|Δw|(리밸런싱 백테스트). */
  avg_turnover?: number | null;
  equity_curve: { t: string; v: number }[];
  markers: { t: string; type: string; price?: number }[];
  /** 종료 시점 보유 비중(종목코드 → 비중). 리밸런싱 백테스트. */
  holdings?: Record<string, number>;
  /** 매수/매도 체결 로그. 리밸런싱 백테스트. */
  trades?: BacktestTrade[];
}

export interface Backtest {
  id: number;
  strategy_id: number;
  period_start: string;
  period_end: string;
  total_return: number | null;
  mdd: number | null;
  sharpe: number | null;
  result: BacktestResult | null;
  created_at: string;
}

/** 종목 검색 결과 1건. */
export interface SymbolHit {
  code: string;
  name: string;
  name_en: string;
  market: string;
}

// ─────────────────────────── 지표(섹터/종목 스냅샷) 타입 ───────────────────────────

/** 지표 조회 시장 필터. */
export type MetricMarket = "ALL" | "KOSPI" | "KOSDAQ";

/** 섹터 지표 정렬 기준. */
export type SectorSort = "rs" | "mom_6m" | "mom_3m" | "value_trend";

/** 종목 지표 정렬 기준. */
export type StockSort = "score" | "mom_6m" | "mom_3m" | "per" | "pbr" | "div";

/** 종목 지표 조회 파라미터. */
export interface StocksQuery {
  market?: MetricMarket;
  sort?: StockSort;
  /** 최소 일평균거래대금(원). */
  min_value?: number;
  /** 최소 시가총액(원). */
  min_mcap?: number;
  /** 최대 반환 건수. */
  limit?: number;
}

/** 섹터별 퀀트 지표 스냅샷(백엔드 /api/metrics/sectors 응답). */
export interface SectorMetric {
  name: string;
  market: string;
  /** 1개월 모멘텀(소수 비율). */
  mom_1m: number | null;
  /** 3개월 모멘텀(소수 비율). */
  mom_3m: number | null;
  /** 6개월 모멘텀(소수 비율). */
  mom_6m: number | null;
  /** 상대강도(126일). */
  rs_126: number | null;
  /** 연환산 변동성(소수 비율). */
  vol_ann: number | null;
  /** 위험조정 모멘텀. */
  risk_adj_mom: number | null;
  /** 거래대금 추세. */
  value_trend: number | null;
  /** 52주 고가 대비 비율(소수 비율). */
  high_52w_ratio: number | null;
  /** 252일 최대 낙폭(소수 비율, 음수). */
  mdd_252: number | null;
  // 섹터 PER/PBR/배당 중앙값은 KRX 업종 구성종목 API 미동작으로 산출 불가하여 제외됨.
}

/** 섹터 지표 응답 래퍼. */
export interface SectorsOut {
  /** 기준일(YYYY-MM-DD). */
  as_of: string;
  items: SectorMetric[];
}

/** 종목별 퀀트 지표 스냅샷(백엔드 /api/metrics/stocks 응답). */
export interface StockMetric {
  code: string;
  name: string;
  market: string;
  /** 현재가(원). */
  price: number | null;
  /** 당일 등락률(소수 비율). */
  change_rate: number | null;
  /** 시가총액(원). */
  market_cap: number | null;
  /** 20일 평균 거래대금(원). */
  avg_value_20: number | null;
  per: number | null;
  pbr: number | null;
  /** 배당수익률(퍼센트 값, pykrx 원값. 예: 2.1 = 2.1%). */
  div: number | null;
  /** 1개월 모멘텀(소수 비율). */
  mom_1m: number | null;
  /** 3개월 모멘텀(소수 비율). */
  mom_3m: number | null;
  /** 6개월 모멘텀(소수 비율). */
  mom_6m: number | null;
  /** 12-1개월 모멘텀(소수 비율). */
  mom_12_1: number | null;
  /** 52주 고가 대비 비율(소수 비율). */
  high_52w_ratio: number | null;
  /** RSI 14일. */
  rsi14: number | null;
  /** 연환산 변동성(소수 비율). */
  vol_ann: number | null;
  /** 252일 최대 낙폭(소수 비율, 음수). */
  mdd_252: number | null;
  /** 이동평균 정배열 여부. */
  trend_aligned: boolean;
  /** 200일 이동평균 위 여부. */
  above_sma200: boolean;
  /** 종합 점수. */
  score: number | null;
  /** 밸류 세부 점수. */
  score_value: number | null;
  /** 모멘텀 세부 점수. */
  score_momentum: number | null;
  /** 저변동성 세부 점수. */
  score_lowvol: number | null;
}

/** 종목 지표 응답 래퍼. */
export interface StocksOut {
  /** 기준일(YYYY-MM-DD). */
  as_of: string;
  /** 전체 종목 수(필터 적용 전). */
  count: number;
  items: StockMetric[];
}

/** 소형주 턴어라운드 스크리너 후보 1건(백엔드 /api/screener/turnaround). */
export interface TurnaroundCandidate {
  code: string;
  name: string;
  market: string;
  market_cap: number;
  avg_value_20: number;
  turnover_surge: number | null;
  net_income: number | null;
  debt_ratio: number | null;
  turnaround: number | null;
  net_growth: number | null;
  f_score: number | null;
  loss_years_3: number | null;
  per: number | null;
  pbr: number | null;
  score: number;
}

export interface TurnaroundScreenOut {
  as_of: string;
  market: string;
  scanned: number;
  count: number;
  opendart_enabled: boolean;
  items: TurnaroundCandidate[];
}

/** KOSPI200 추천 구성종목 1건(백엔드 /api/recommend/kospi200). */
export interface RecommendMember {
  code: string;
  name: string;
  /** 현재가(원). */
  price: number;
  /** 당일 등락률(소수 비율). */
  change_rate: number | null;
  /** 시가총액(원). */
  market_cap: number;
  /** 20일 평균 거래대금(원). */
  avg_value_20: number;
  per: number | null;
  pbr: number | null;
  /** 배당수익률(퍼센트 값, pykrx 원값). */
  div: number | null;
  /** 3개월 모멘텀(소수 비율). */
  mom_3m: number | null;
  /** 6개월 모멘텀(소수 비율). */
  mom_6m: number | null;
  /** 연환산 변동성(소수 비율). */
  vol_ann: number | null;
  /** 252일 최대 낙폭(소수 비율, 음수). */
  mdd_252: number | null;
  trend_aligned: boolean | null;
  above_sma200: boolean | null;
  // ── 팩터별 세부점수(KOSPI200 크로스섹션 z-score, 가중치 무관) ──
  score_momentum: number | null;
  score_value: number | null;
  score_lowvol: number | null;
  score_quality: number | null;
  score_growth: number | null;
}

/** KOSPI200 추천 응답 래퍼. */
export interface RecommendOut {
  /** 기준일(YYYY-MM-DD). */
  as_of: string;
  /** 지수명("KOSPI200"). */
  index: string;
  /** 구성종목 수. */
  count: number;
  /** 퀄리티·성장 팩터가 실제 데이터로 채워졌는지(OpenDART 연동 여부). */
  opendart_enabled: boolean;
  items: RecommendMember[];
}

export interface Position {
  symbol: string;
  qty: number;
  avg_price: number;
}

export interface OrderRow {
  id: number;
  symbol: string;
  side: "buy" | "sell";
  qty: number;
  price: number | null;
  status: string;
  created_at: string;
}

// ─────────────────────────── API ───────────────────────────
export const api = {
  /**
   * 이메일/비밀번호로 신규 계정을 생성한다.
   * @param email    가입 이메일(서버에서 중복 검사)
   * @param password 비밀번호(8자 이상)
   * @returns 생성된 사용자 정보
   * @throws Error 이미 등록된 이메일이거나 검증 실패 시
   */
  register: (email: string, password: string) =>
    request<UserOut>("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),

  /**
   * 로그인. 성공 시 서버가 HttpOnly 쿠키(액세스/리프레시 토큰)를 발급하며,
   * 토큰 자체는 클라이언트 JS 로 노출되지 않는다.
   * @param email    이메일(OAuth2 form 의 username 필드로 전송)
   * @param password 비밀번호
   * @returns 로그인한 사용자 정보
   * @throws Error 이메일/비밀번호 불일치 시
   */
  login: async (email: string, password: string): Promise<UserOut> => {
    const body = new URLSearchParams({ username: email, password });
    const res = await fetch(`${API_BASE}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
      credentials: "include", // 서버가 HttpOnly 쿠키로 토큰을 발급
    });
    if (!res.ok) {
      const b = await res.json().catch(() => ({}));
      throw new Error(formatError(b?.detail, res.status));
    }
    return res.json() as Promise<UserOut>;
  },

  /** 로그아웃. 서버에서 현재 세션을 폐기(Redis 삭제)하고 쿠키를 제거한다. */
  logout: () => request<void>("/api/auth/logout", { method: "POST" }),

  /** 현재 로그인 세션의 사용자 정보. 미인증(쿠키 없음/만료)이면 401 로 실패한다. */
  me: () => request<UserOut>("/api/auth/me"),

  /**
   * 프로필(닉네임)을 갱신한다. 빈 값이면 미설정(null)으로 처리된다.
   * @param display_name 공유 목록에 표시할 닉네임
   */
  updateProfile: (display_name: string) =>
    request<UserOut>("/api/auth/me", {
      method: "PATCH",
      body: JSON.stringify({ display_name }),
    }),

  // --- 증권사 연동(KIS/토스) ---
  /** 등록된 자격증명으로 토큰 발급을 시도해 연동 상태를 확인한다. */
  kisHealth: () =>
    request<{
      broker: Broker;
      env: string;
      is_paper_trading: boolean;
      token_issued: boolean;
      message: string;
    }>("/api/kis/health"),

  /**
   * 증권사 API 자격증명을 등록·갱신한다. 키/시크릿은 서버에서 암호화 저장된다.
   * broker 에 따라 필드 의미가 달라진다(kis: App Key/Secret/계좌, toss: client_id/secret/accountSeq).
   * @param broker         증권사 식별자(kis|toss)
   * @param kis_app_key    App Key 또는 토스 client_id
   * @param kis_app_secret App Secret 또는 토스 client_secret
   * @param kis_account_no 계좌번호(예: "50012345-01") 또는 토스 accountSeq
   */
  registerKis: (
    broker: Broker,
    kis_app_key: string,
    kis_app_secret: string,
    kis_account_no: string,
  ) =>
    request<UserOut>("/api/kis/credentials", {
      method: "PUT",
      body: JSON.stringify({ broker, kis_app_key, kis_app_secret, kis_account_no }),
    }),

  /**
   * 현재가 시세 조회. 통합 시세(토스) 연동 시 국내·해외 모두, 아니면 주문 브로커로 조회.
   * @param symbol 종목코드(국내: "005930", 해외: "AAPL")
   */
  quote: (symbol: string) =>
    request<Quote>(`/api/kis/quote/${encodeURIComponent(symbol)}`),

  /**
   * 통합 시세(국내+해외)용 토스 자격증명을 등록·갱신한다. 주문 브로커와 독립이다.
   * @param client_id     토스 client_id
   * @param client_secret 토스 client_secret
   * @param account_seq   토스 accountSeq
   */
  registerTossQuote: (
    client_id: string,
    client_secret: string,
    account_seq: string,
  ) =>
    request<UserOut>("/api/kis/toss-quote", {
      method: "PUT",
      body: JSON.stringify({
        broker: "toss",
        kis_app_key: client_id,
        kis_app_secret: client_secret,
        kis_account_no: account_seq,
      }),
    }),

  // --- 전략 ---
  /** 로그인 사용자의 전략 목록을 조회한다. */
  listStrategies: () => request<Strategy[]>("/api/strategies"),

  /** 전략 단건 조회. @param id 전략 ID */
  getStrategy: (id: number) => request<Strategy>(`/api/strategies/${id}`),

  /**
   * 신규 전략을 생성한다.
   * @param name        전략 이름
   * @param config      전략 설정(종목·이평 기간·초기자본 등)
   * @param description 전략 설명(선택)
   */
  createStrategy: (name: string, config: StrategyConfig, description?: string) =>
    request<Strategy>("/api/strategies", {
      method: "POST",
      body: JSON.stringify({ name, config, description }),
    }),

  /**
   * 전략의 이름/설명/설정을 수정한다(전달된 필드만 갱신).
   * @param id          전략 ID
   * @param name        새 이름(미변경 시 생략)
   * @param config      새 설정(미변경 시 생략)
   * @param description 새 설명(미변경 시 생략, 빈 문자열이면 설명 제거)
   */
  updateStrategy: (
    id: number,
    name?: string,
    config?: StrategyConfig,
    description?: string,
  ) =>
    request<Strategy>(`/api/strategies/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ name, config, description }),
    }),

  /**
   * 대표 백테스트를 지정/해제한다(공유 시 성과 표시용).
   * @param id         전략 ID
   * @param backtestId 대표로 지정할 백테스트 ID, null 이면 해제
   */
  setFeaturedBacktest: (id: number, backtestId: number | null) =>
    request<Strategy>(`/api/strategies/${id}/featured-backtest`, {
      method: "PUT",
      body: JSON.stringify({ backtest_id: backtestId }),
    }),

  /** 전략을 삭제한다. @param id 전략 ID */
  deleteStrategy: (id: number) =>
    request<void>(`/api/strategies/${id}`, { method: "DELETE" }),

  // --- 공유/복사/좋아요/정렬·즐겨찾기 ---
  /** 전략을 공유 상태로 전환한다. @param id 전략 ID */
  shareStrategy: (id: number) =>
    request<Strategy>(`/api/strategies/${id}/share`, { method: "POST" }),

  /** 전략 공유를 해제한다. @param id 전략 ID */
  unshareStrategy: (id: number) =>
    request<Strategy>(`/api/strategies/${id}/share`, { method: "DELETE" }),

  /** 전략을 즐겨찾기로 표시한다. @param id 전략 ID */
  favoriteStrategy: (id: number) =>
    request<Strategy>(`/api/strategies/${id}/favorite`, { method: "POST" }),

  /** 전략 즐겨찾기를 해제한다. @param id 전략 ID */
  unfavoriteStrategy: (id: number) =>
    request<Strategy>(`/api/strategies/${id}/favorite`, { method: "DELETE" }),

  /**
   * 내 전략 표시 순서를 일괄 갱신한다(배열 순서대로 0..n).
   * @param orderedIds 원하는 표시 순서대로의 전략 ID 배열
   */
  reorderStrategies: (orderedIds: number[]) =>
    request<void>("/api/strategies/reorder", {
      method: "PATCH",
      body: JSON.stringify({ ordered_ids: orderedIds }),
    }),

  /**
   * 공유된 전체 사용자의 전략 목록을 조회한다(좋아요·작성자·설명·대표 백테스트 포함).
   * @param params 제목(q)·종목(symbol) 필터와 정렬(sort) 옵션
   */
  listSharedStrategies: (params: SharedQuery = {}) => {
    const sp = new URLSearchParams();
    if (params.q) sp.set("q", params.q);
    if (params.symbol) sp.set("symbol", params.symbol);
    if (params.sort) sp.set("sort", params.sort);
    const qs = sp.toString();
    return request<SharedStrategy[]>(
      `/api/strategies/shared${qs ? `?${qs}` : ""}`,
    );
  },

  /** 공유 전략(또는 본인 전략)을 내 전략으로 복사한다. @param id 원본 전략 ID */
  copyStrategy: (id: number) =>
    request<Strategy>(`/api/strategies/${id}/copy`, { method: "POST" }),

  /** 공유 전략에 좋아요를 누른다(인당 1회). @param id 전략 ID */
  likeStrategy: (id: number) =>
    request<LikeResult>(`/api/strategies/${id}/like`, { method: "POST" }),

  /** 좋아요를 취소한다. @param id 전략 ID */
  unlikeStrategy: (id: number) =>
    request<LikeResult>(`/api/strategies/${id}/like`, { method: "DELETE" }),

  // --- 백테스트 ---
  /**
   * 지정 기간으로 전략 백테스트를 실행한다(서버에서 동기 실행 후 결과 반환).
   * @param id           전략 ID
   * @param period_start 시작일(YYYY-MM-DD)
   * @param period_end   종료일(YYYY-MM-DD)
   */
  runBacktest: (id: number, period_start: string, period_end: string) =>
    request<Backtest>(`/api/strategies/${id}/backtest`, {
      method: "POST",
      body: JSON.stringify({ period_start, period_end }),
    }),

  /** 전략의 백테스트 실행 이력을 조회한다. @param id 전략 ID */
  listBacktests: (id: number) =>
    request<Backtest[]>(`/api/strategies/${id}/backtests`),

  // --- 매매 엔진 제어 ---
  /** 전략을 라이브(자동매매)로 전환하도록 엔진에 시작 명령을 보낸다. @param id 전략 ID */
  startStrategy: (id: number) =>
    request<{ status: string }>(`/api/engine/strategies/${id}/start`, {
      method: "POST",
    }),

  /** 전략 자동매매를 중지하도록 엔진에 중지 명령을 보낸다. @param id 전략 ID */
  stopStrategy: (id: number) =>
    request<{ status: string }>(`/api/engine/strategies/${id}/stop`, {
      method: "POST",
    }),

  // --- 잔고/포지션/주문 ---
  /** 보유 포지션 목록(수량 > 0). */
  positions: () => request<Position[]>("/api/trading/positions"),

  /** 최근 주문 내역(감사 로그). */
  orders: () => request<OrderRow[]>("/api/trading/orders"),

  /** 매매 엔진 생존 여부(heartbeat) 조회. */
  engineStatus: () =>
    request<{ engine_alive: boolean }>("/api/engine/status"),

  // --- 종목 검색 ---
  /**
   * 코드/한글명/영문명으로 KRX 종목을 검색한다.
   * @param q     검색어
   * @param limit 최대 개수(기본 20)
   */
  searchSymbols: (q: string, limit = 20) =>
    request<SymbolHit[]>(
      `/api/symbols/search?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),

  /** 종목코드 → 한글명 전체 매핑(체결/주문 로그에 종목명 표시용). */
  symbolNames: () => request<Record<string, string>>("/api/symbols/names"),

  // --- 지표(섹터/종목 스냅샷) ---
  /**
   * 섹터별 퀀트 지표 스냅샷을 조회한다.
   * @param market ALL|KOSPI|KOSDAQ
   * @param sort   정렬 기준(rs|mom_6m|mom_3m|value_trend)
   */
  getSectors: (market: MetricMarket = "ALL", sort: SectorSort = "rs") => {
    const sp = new URLSearchParams({ market, sort });
    return request<SectorsOut>(`/api/metrics/sectors?${sp.toString()}`);
  },

  /**
   * 종목별 퀀트 지표 스냅샷을 조회한다.
   * @param params 시장·정렬·최소거래대금·최소시총·표시개수
   */
  getStocks: (params: StocksQuery = {}) => {
    const sp = new URLSearchParams();
    if (params.market) sp.set("market", params.market);
    if (params.sort) sp.set("sort", params.sort);
    if (params.min_value != null) sp.set("min_value", String(params.min_value));
    if (params.min_mcap != null) sp.set("min_mcap", String(params.min_mcap));
    if (params.limit != null) sp.set("limit", String(params.limit));
    const qs = sp.toString();
    return request<StocksOut>(`/api/metrics/stocks${qs ? `?${qs}` : ""}`);
  },

  /**
   * 소형주 턴어라운드 스크리너: 전 시장을 스캔해 하드 필터(시총 하위·거래대금 급증·
   * 저부채·만성적자 제외·흑자전환)를 통과한 후보를 반환한다.
   */
  screenTurnaround: (
    params: {
      market?: MetricMarket;
      top_n?: number;
      smallcap_pct?: number;
      surge?: number;
      max_debt?: number;
    } = {},
  ) => {
    const sp = new URLSearchParams();
    if (params.market) sp.set("market", params.market);
    if (params.top_n != null) sp.set("top_n", String(params.top_n));
    if (params.smallcap_pct != null) sp.set("smallcap_pct", String(params.smallcap_pct));
    if (params.surge != null) sp.set("surge", String(params.surge));
    if (params.max_debt != null) sp.set("max_debt", String(params.max_debt));
    const qs = sp.toString();
    return request<TurnaroundScreenOut>(`/api/screener/turnaround${qs ? `?${qs}` : ""}`);
  },

  // --- 추천(KOSPI200 팩터 가중) ---
  /**
   * 현 시점 KOSPI200 구성종목의 팩터별 세부점수·메타데이터를 조회한다.
   * 종합점수 가중합·Top-N 선정은 클라이언트가 사용자 비중으로 수행한다.
   */
  getKospi200Recommend: () =>
    request<RecommendOut>("/api/recommend/kospi200"),
};
