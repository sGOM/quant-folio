// QuantFolio API 클라이언트 — HttpOnly 쿠키 기반 인증.
// 토큰은 JS 에서 접근하지 않으며, 모든 요청에 credentials: "include" 로 쿠키를 전송한다.

/** 백엔드 API 기본 주소. 빌드 환경변수가 없으면 로컬 개발 서버로 폴백한다. */
const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/** 외부(WS 등)에서 재사용할 수 있도록 노출한 API 기본 주소. */
export const API_BASE_URL = API_BASE;

/**
 * HTTP 상태 코드를 보존하는 에러. 대부분의 호출부는 `.message` 만 보면 되지만,
 * 409(충돌) 처럼 상태 코드로 분기해야 하는 흐름(예: 슬리피지 캘리브레이션 승인 시
 * 리포트가 그새 갱신돼 제안이 달라진 경우)에서 `instanceof ApiError` 로 구분한다.
 */
export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

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
    throw new ApiError(formatError(body?.detail, res.status), res.status);
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
  /**
   * method="score": 팩터 중립화 축(P1-3). "none"(기본) / "size"(시가총액 중립화 →
   * 각 팩터를 로그 시총 축에 직교화해 대형/소형주 베팅으로 변질되는 것을 막음).
   */
  neutralize?: "none" | "size";
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
  /** 섹터(업종) 합산 목표비중 상한(0~1). 섹터 매핑 조회 실패 시 조용히 미적용. */
  max_sector_pct?: number | null;
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
  /** 콜드 스타트 즉시 발화. true 면 첫 실행(보유 없음)에 한해 발화일/시각을 기다리지 않고 장중 즉시 1회 매수. */
  initial_fill_immediate?: boolean;
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
  /** 체결가를 KRX 호가단위로 라운딩하고 ±30% 상하한가 도달 방향 주문을 그날 체결
   * 불가(다음 리밸런싱 이월)로 막는다. false(기본)면 슬리피지에 근사 흡수(구 동작). */
  price_limit_model?: boolean;
  /** 퀄리티·성장 팩터(OpenDART)의 재무 반영 주기. "annual"(기본)=연간 사업보고서,
   * "ttm"=분기 트레일링 4분기(재무 신선도↑, 둘 다 미래참조 없음). */
  financial_period?: "annual" | "ttm";
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
  /** 평균 회전율 Σ|Δw|(리밸런싱 백테스트, ADV 캡·절사·상하한가 반영 이전 드리프트 보정치). */
  avg_turnover?: number | null;
  /** 실체결 기준 평균 회전율(§7, 항상 avg_turnover 이하) — ADV 캡·정수주 절사·상하한가
   * 체결불가 반영 이후 실제 체결된 거래대금 기준. 둘의 괴리가 곧 유동성 제약으로 못
   * 채운 물량이다. */
  avg_turnover_actual?: number | null;
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

/**
 * Deflated Sharpe Ratio(DSR, P2-1) 분석 결과. 같은 전략·같은 기간의 백테스트 이력
 * (파라미터 탐색 시행들)을 동질 집합으로 삼아 다중검정(selection bias)을 보정한다.
 * status="unavailable"이면 dsr 등 핵심 필드가 없고 reason 만 채워진다.
 */
export interface DsrAnalysis {
  backtest_id: number;
  strategy_id: number;
  period_start: string;
  period_end: string;
  status: "ok" | "insufficient_trials" | "unavailable";
  /** strong(≥0.95) / marginal(≥0.90) / inconclusive(≥0.50) / overfit_suspected(<0.50) / insufficient_trials. */
  grade: "strong" | "marginal" | "inconclusive" | "overfit_suspected" | "insufficient_trials";
  /** Deflated Sharpe Ratio(0~1). 유효 시행 수(N_eff)<2 면 정의되지 않아 null. */
  dsr?: number | null;
  /** 다중검정 미보정 유의성(단일 시행 가정). dsr 이 없어도 참고용으로 제공. */
  psr0?: number | null;
  sharpe_annual?: number | null;
  /** 동질 집합에 잡힌 시행 수(원본/유효). */
  N?: number;
  N_eff?: number;
  /** 시행 간 평균 상관(유효 시행 수 축소에 반영). */
  rho_bar?: number | null;
  /** 시행별 샤프 분산 추정의 표본 신뢰도가 낮으면 true(참고용 경고). */
  v_low_confidence?: boolean;
  reason?: string;
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

// ─────────────────────────── 패닉셀(투매) 지표 타입 ───────────────────────────

/** 패닉셀 경보 단계. */
export type PanicLevel = "normal" | "caution" | "warning" | "panic";

/** 패닉셀 구성 시그널 1건. */
export interface PanicSignal {
  key: string;
  label: string;
  /** 원시 측정값(시그널별 단위 상이: 수익률·비율·배율 등). */
  value: number | null;
  /** 0~100 서브스코어(경계 0 · 패닉 100). */
  subscore: number | null;
  /** 종합점수 가중(0=비가중 참고 신호). */
  weight: number;
  /** 경계 임계값. */
  warn: number | null;
  /** 패닉 임계값. */
  panic: number | null;
}

/** 시장 1개(KOSPI/KOSDAQ)의 패닉셀 판정. */
export interface PanicMarket {
  market: string;
  /** 0~100 종합점수(dd60 국면 보정 후). */
  score: number;
  level: PanicLevel;
  /** 패닉 게이팅 충족 여부(가격급락+거래폭증/브레드스). */
  gated: boolean;
  /** 하드트리거 발동 여부(극단값 → 즉시 패닉). */
  hard_trigger: boolean;
  /** 가격축 서브스코어(0~100). */
  price_sub: number | null;
  /** 브레드스축 서브스코어(0~100). */
  breadth_sub: number | null;
  /** 60일 고점대비 낙폭(음수 소수). */
  dd60: number | null;
  /** 브레드스 유효 종목 수(거래정지 제외). */
  universe: number;
  signals: PanicSignal[];
}

/** 패닉셀 지표 응답 래퍼(백엔드 /api/metrics/panic). */
export interface PanicOut {
  /** 기준일(YYYY-MM-DD). */
  as_of: string;
  items: PanicMarket[];
  /** 조회/데이터 부족으로 계산하지 못한 시장 목록(예: ["KOSDAQ"]). */
  unavailable: string[];
  /**
   * 지표 방법론상 알려진 한계 고지 문구(예: "종가 확정 후 판정(장중 회복 미탐지)").
   * 백엔드 배선 시점에 따라 아직 응답에 없을 수 있어 옵셔널 — 없으면 UI에 배너를 노출하지 않는다.
   */
  caveats?: string[];
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

/** 엔진 실시간 알림(WS "alert" 이벤트)의 사유 코드. */
export type AlertCode =
  | "runner_failures"
  | "pit_fallback"
  | "mdd_kill"
  | "factor_outage"
  | "fill_quality_drift"
  | "slippage_calibration_proposed"
  | "ohlcv_ingest_failure_rate"
  | "db_backup_failed"
  | "db_backup_stale"
  | "db_backup_s3_upload_failed"
  | "live_gate_blocked";

/** 알림 심각도. critical=파국/치명, warning=주의(자동 폴백 등). */
export type AlertSeverity = "warning" | "critical";

/** GET /api/alerts 응답 항목 — DB 영속화된 알림(§17). */
export interface AlertRecord {
  id: number;
  user_id: number | null;
  strategy_id: number;
  severity: AlertSeverity;
  code: AlertCode | string | null;
  message: string;
  is_read: boolean;
  created_at: string;
}

export interface AlertListOut {
  items: AlertRecord[];
  unread_count: number;
}

/** 본인 + 전역(user_id=NULL) 알림 목록(최신순). */
export function listAlerts(unreadOnly = false, limit = 50): Promise<AlertListOut> {
  const params = new URLSearchParams({ unread_only: String(unreadOnly), limit: String(limit) });
  return request<AlertListOut>(`/api/alerts?${params.toString()}`);
}

export function markAlertRead(alertId: number): Promise<AlertRecord> {
  return request<AlertRecord>(`/api/alerts/${alertId}/read`, { method: "POST" });
}

export function markAllAlertsRead(): Promise<{ updated: number }> {
  return request<{ updated: number }>(`/api/alerts/read-all`, { method: "POST" });
}

/** 전략 러너 1개의 헬스 상태(백엔드 /api/engine/strategies/health 응답 항목). */
export interface StrategyHealth {
  strategy_id: number;
  name: string;
  status: Strategy["status"];
  /** 마지막으로 틱을 정상 처리한 시각(ISO). 아직 한 번도 성공 못했으면 null. */
  last_success_ts: string | null;
  /** 연속 실패 횟수(threshold 이상이면 unhealthy). */
  consecutive_failures: number;
  /** 마지막 실패 사유(성공 시 null). */
  last_error: string | null;
  updated_at: string | null;
  /** 러너가 최소 한 번 이상 상태를 보고했는지(false면 방금 시작해 아직 첫 틱 전). */
  reported: boolean;
  /** consecutive_failures < threshold. */
  healthy: boolean;
}

/** 전략별 헬스 조회 응답 래퍼. */
export interface StrategiesHealthOut {
  /** unhealthy 판정 연속 실패 임계값. */
  threshold: number;
  strategies: StrategyHealth[];
}

/** 분포 요약 통계(n·평균·표준편차·중앙값·p90·p95). 표본 없으면 n=0·나머지 null. */
export interface MetricStats {
  n: number;
  mean: number | null;
  std: number | null;
  median: number | null;
  p90: number | null;
  p95: number | null;
}

/** 방향별(전체/매수/매도) 분포 요약 묶음. */
export interface MetricBlock {
  all: MetricStats;
  buy: MetricStats;
  sell: MetricStats;
}

/**
 * 실거래–백테스트 체결 정합 실측(P2-3, GET /api/backtest/fill-quality) 응답.
 * M1=실행 슬리피지(주문가 대비 실제 체결가) · M2=시점 규약 표류(당일종가 vs 익일종가) ·
 * M3=총 정합 괴리(실제 체결가 vs 백테스트 가정가). 등급은 표본(min_sample) 미달 시
 * "INSUFFICIENT"(판정 보류)가 정상 경로 — 모의투자 초기이거나 매매가 드문 전략에서 흔하다.
 */
export interface FillQualityReport {
  sample: {
    n_orders: number;
    n_skipped: number;
    n_clusters: number;
    n_arrival_fallback: number;
    min_sample: number;
    note: string;
  };
  assumptions: {
    backtest_slip_bps: number;
    slip_base_bps: number;
    slip_vol_scale: number;
    annual_turnover: number | null;
  };
  m1_exec: MetricBlock;
  m2_time: MetricBlock;
  m3_total: MetricBlock;
  checksum: { mean_abs_residual_bp: number | null; note: string };
  annualized_drag: {
    drag_abs_pct_per_yr: number | null;
    drag_diff_pct_per_yr: number | null;
  };
  /** "INSUFFICIENT" | "GREEN" | "AMBER" | "RED". */
  grades: { m1_exec: string; m3_total: string };
  window: {
    date_from: string;
    date_to: string;
    strategy_id: number | null;
    turnover_estimated: boolean;
  };
  /**
   * 사람이 승인 가능한 slippage_bps 재조정 제안(읽기 전용, config 는 자동 변경되지
   * 않는다). 표본 부족·유의 변화 없음이면 null — UI 는 이 경우 섹션 자체를 숨긴다.
   */
  calibration: CalibrationProposal | null;
}

/** GET /api/backtest/fill-quality 응답에 포함되는 슬리피지 캘리브레이션 제안. */
export interface CalibrationProposal {
  /** 전략 config 에 현재 반영돼 있는 slippage_bps. */
  current_bps: number;
  /** 실측 근거로 산출한 신규 제안 bps(클램프 적용 후 값). */
  proposed_bps: number;
  /** 실측 M1 실행 슬리피지 중앙값(bp). */
  observed_median_bps: number;
  /** 제안 산출에 쓰인 표본 수(방향별 min_sample 이상). */
  sample_size: number;
  /** 극단값 방지 클램프가 적용됐는지 여부. */
  clamped: boolean;
  /** 제안 근거·클램프 사유 등 설명 문구. */
  reason: string;
}

/** POST /api/backtest/slippage-calibration/{strategy_id}/apply 응답. */
export interface SlippageCalibrationApplyResult {
  strategy_id: number;
  previous_bps: number;
  applied_bps: number;
  observed_median_bps: number;
  sample_size: number;
  clamped: boolean;
  reason: string;
}

export interface OrderRow {
  id: number;
  symbol: string;
  side: "buy" | "sell";
  qty: number;
  price: number | null;
  status: string;
  /** 감사 로그 상세 사유 — 어떤 신호·공식·리스크·리밸런싱 기준으로 주문했는지. 과거/수동 주문은 null. */
  reason: string | null;
  created_at: string;
}

/**
 * 전략 실측(모의투자/라이브) ↔ 백테스트 기대곡선 괴리 추적(improvements.md §6) 응답.
 * GET /api/strategies/{id}/tracking. 체결이 하나도 없으면 404(잡히면 "아직 실측 데이터
 * 없음"으로 처리), 비교 백테스트가 없으면 200이되 backtest_curve=[]·warning 채움.
 */
export interface TrackingResponse {
  strategy_id: number;
  backtest_id: number | null;
  window: { date_from: string; date_to: string };
  initial_capital: number;
  /** 실측 일별 NAV(원). */
  realized_curve: { t: string; v: number }[];
  /** 백테스트 equity_curve(원). 비교 대상 없으면 []. */
  backtest_curve: { t: string; v: number }[];
  /** 실측 곡선을 자기 첫 값 기준 100으로 재기준. */
  realized_index: { t: string; v: number }[];
  /** 백테스트 곡선을 자기 첫 값 기준 100으로 재기준. */
  backtest_index: { t: string; v: number }[];
  metrics: {
    /** 일수익률 차이의 연율화 표준편차(소수, 0.08=8%/yr). null=공통 거래일 부족. */
    tracking_error: number | null;
    /** 공통구간 첫날 대비 마지막날 실측/백테스트 상대 성과 격차(%). 양수=실측 초과. */
    cumulative_divergence_pct: number | null;
    realized_return_pct: number | null;
    backtest_return_pct: number | null;
    correlation: number | null;
    n_common_days: number;
  };
  sample: {
    n_executions: number;
    n_symbols: number;
    n_realized_days: number;
    n_backtest_days: number;
  };
  /** 예: 비교할 백테스트가 없음. null이면 정상. */
  warning: string | null;
  notes: string[];
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

  /**
   * 백테스트 1건의 DSR(Deflated Sharpe Ratio, P2-1)을 온디맨드 계산한다.
   * 같은 전략·같은 기간의 다른 백테스트 이력이 쌓일수록(파라미터 탐색 시행) 추정이
   * 정교해진다. 이력이 부족(N_eff<2)하면 status="insufficient_trials"로 dsr=null.
   * @param backtestId 백테스트 ID
   */
  getBacktestDsr: (backtestId: number) =>
    request<DsrAnalysis>(`/api/backtests/${backtestId}/dsr`),

  /**
   * 전략 실측(모의투자/라이브) NAV 곡선과 백테스트 기대곡선을 비교한다(improvements.md §6).
   * 체결이 하나도 없으면 404(호출부는 "아직 실측 데이터 없음"으로 처리해야 함).
   * @param strategyId 전략 ID
   * @param opts.dateFrom    조회 시작일(YYYY-MM-DD). 미지정 시 첫 체결일까지 개방.
   * @param opts.dateTo      조회 종료일(YYYY-MM-DD). 미지정 시 오늘.
   * @param opts.backtestId  비교할 특정 백테스트 id. 미지정 시 최신 백테스트 자동 매칭.
   */
  getStrategyTracking: (
    strategyId: number,
    opts?: { dateFrom?: string; dateTo?: string; backtestId?: number },
  ) => {
    const params = new URLSearchParams();
    if (opts?.dateFrom) params.set("date_from", opts.dateFrom);
    if (opts?.dateTo) params.set("date_to", opts.dateTo);
    if (opts?.backtestId !== undefined) params.set("backtest_id", String(opts.backtestId));
    const qs = params.toString();
    return request<TrackingResponse>(
      `/api/strategies/${strategyId}/tracking${qs ? `?${qs}` : ""}`,
    );
  },

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

  /**
   * 매매 엔진 생존 여부(heartbeat) + 마지막 DB 백업 성공 시각(§9/§18) 조회.
   * `backup_last_success_at` 은 ISO 문자열(worker.backup_database 가 기록) 또는 백업이
   * 한 번도 성공한 적 없으면 null.
   */
  engineStatus: () =>
    request<{ engine_alive: boolean; backup_last_success_at: string | null }>(
      "/api/engine/status",
    ),

  /**
   * 현재 사용자 소유의 라이브 전략별 러너 헬스(마지막 성공 틱·연속 실패 횟수 등)를 조회한다.
   * 엔진 프로세스 전역 생존(engineStatus)과 달리, 전략 단위 러너 이상을 보여준다.
   */
  getStrategiesHealth: () =>
    request<StrategiesHealthOut>("/api/engine/strategies/health"),

  /**
   * 실거래–백테스트 체결 정합 실측 리포트(P2-3)를 조회한다. 전략을 지정하지 않으면
   * 본인의 모든 전략 리밸런싱 체결을 합산한다. 표본이 min_sample 미만이면 등급이
   * "INSUFFICIENT"(판정 보류)로 나오는 게 정상 — 매매 이력이 쌓일수록 신뢰도가 오른다.
   * @param days 조회 기간(오늘 기준 최근 N일, 기본 90)
   * @param strategyId 특정 전략만 볼 때 지정
   */
  getFillQuality: (days = 90, strategyId?: number) =>
    request<FillQualityReport>(
      `/api/backtest/fill-quality?days=${days}${
        strategyId !== undefined ? `&strategy_id=${strategyId}` : ""
      }`,
    ),

  /**
   * 체결 정합 실측을 근거로 산출된 슬리피지 캘리브레이션 제안을 승인·반영한다.
   * 서버가 최신 리포트로 제안값을 재계산해 대조하므로, 화면에 보이던 값이 오래돼
   * 최신 제안과 어긋나면(리포트가 그새 갱신됨) 409 를 던진다 — 이 경우 리포트를
   * 다시 불러온 뒤 재시도해야 한다.
   * @param strategyId  반영 대상 전략 id
   * @param proposedBps 승인할 slippage_bps(리포트의 calibration.proposed_bps)
   * @throws Error 전략 미존재(404) 또는 제안 불일치·표본 부족(409) 시
   */
  applySlippageCalibration: (strategyId: number, proposedBps: number) =>
    request<SlippageCalibrationApplyResult>(
      `/api/backtest/slippage-calibration/${strategyId}/apply`,
      {
        method: "POST",
        body: JSON.stringify({ proposed_bps: proposedBps }),
      },
    ),

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
   * 패닉셀(투매) 시장 지표를 조회한다.
   * @param market ALL|KOSPI|KOSDAQ (ALL이면 KOSPI·KOSDAQ 둘 다 반환)
   */
  getPanic: (market: MetricMarket = "ALL") => {
    const sp = new URLSearchParams({ market });
    return request<PanicOut>(`/api/metrics/panic?${sp.toString()}`);
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
