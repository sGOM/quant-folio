// 전략 유형별 메타데이터·기본값·요약 — 생성/편집 폼과 목록·상세 화면이 공유한다.

import {
  Condition,
  ConditionGroup,
  Operand,
  RuleNode,
  StrategyConfig,
  StrategyType,
  isGroup,
} from "@/lib/api";

/** 전략 유형 → 한글 라벨(드롭다운·요약 표시용). */
export const STRATEGY_TYPE_LABELS: Record<StrategyType, string> = {
  sma_crossover: "SMA 크로스",
  ema_crossover: "EMA 크로스",
  rsi: "RSI",
  macd: "MACD",
  bollinger: "볼린저밴드",
  breakout: "돌파(Donchian)",
  momentum: "절대 모멘텀",
  zscore: "Z-스코어 회귀",
  disparity: "이격도",
  donchian_squeeze: "변동성 스퀴즈",
  trix: "TRIX 모멘텀",
  obv_trend: "OBV 거래량 추세",
  atr_trailing: "ATR 트레일링",
  volatility_breakout: "변동성 돌파",
  keltner: "켈트너 채널",
  stochastic: "스토캐스틱",
  custom: "사용자 정의",
  rebalance: "리밸런싱(포트폴리오)",
};

/** 드롭다운 노출 순서. OHLC 전용 전략은 라벨 뒤에 표기. */
export const STRATEGY_TYPES: StrategyType[] = [
  "sma_crossover",
  "ema_crossover",
  "rsi",
  "macd",
  "bollinger",
  "breakout",
  "momentum",
  "zscore",
  "disparity",
  "donchian_squeeze",
  "trix",
  "obv_trend",
  "atr_trailing",
  "volatility_breakout",
  "keltner",
  "stochastic",
  "custom",
  "rebalance",
];

/** OHLC(고/저/시가) 또는 거래량 입력이 필요한 전략 유형. (백엔드 signals.OHLC_TYPES 와 일치) */
export const OHLC_STRATEGY_TYPES: ReadonlySet<StrategyType> = new Set([
  "obv_trend",
  "atr_trailing",
  "volatility_breakout",
  "keltner",
  "stochastic",
]);

/** 공통 필드(종목·자본·수수료·리스크 청산)의 기본값. */
const COMMON_DEFAULTS = {
  symbol: "005930",
  cash: 10_000_000,
  fees: 0.00015, // 위탁수수료 0.015%
  tax: 0.002, // 증권거래세 0.20%(2026 KRX, 매도 시)
  stop_loss_pct: null,
  take_profit_pct: null,
  trailing_stop_pct: null,
};

/**
 * 지정한 유형의 기본 설정을 만든다. 공통 필드(종목·자본·리스크)는 인계받아 유지한다.
 * @param type 전략 유형
 * @param common 유지할 공통 필드(유형 전환 시 입력값 보존용)
 */
export function defaultConfig(
  type: StrategyType,
  common: Partial<StrategyConfig> = {},
): StrategyConfig {
  // 리밸런싱은 단일 종목 공통 필드(symbol·cash·리스크)를 쓰지 않는 별도 형태.
  if (type === "rebalance") {
    return {
      type,
      universe: ["005930", "000660", "035720"],
      selection: { method: "momentum", lookback: 120, top_n: 2 },
      weighting: "equal",
      cadence: "monthly",
      rebalance_weekday: null,
      rebalance_dom: 1,
      rebalance_time: "14:30",
      drift_band_pct: 0.05,
      capital: 10_000_000,
      fees: COMMON_DEFAULTS.fees,
      tax: COMMON_DEFAULTS.tax,
    };
  }
  const base = { ...COMMON_DEFAULTS, ...common };
  switch (type) {
    case "sma_crossover":
      return { ...base, type, fast: 5, slow: 20 };
    case "ema_crossover":
      return { ...base, type, fast: 12, slow: 26 };
    case "rsi":
      return { ...base, type, period: 14, lower: 30, upper: 70 };
    case "macd":
      return { ...base, type, fast: 12, slow: 26, signal: 9 };
    case "bollinger":
      return { ...base, type, period: 20, num_std: 2 };
    case "breakout":
      return { ...base, type, period: 20 };
    case "momentum":
      return { ...base, type, lookback: 120 };
    case "zscore":
      return { ...base, type, period: 20, entry: 2 };
    case "disparity":
      return { ...base, type, period: 20, lower: 95, upper: 105 };
    case "donchian_squeeze":
      return { ...base, type, period: 20, bb_mult: 1, kc_mult: 1.3 };
    case "trix":
      return { ...base, type, period: 15, signal_period: 9 };
    case "obv_trend":
      return { ...base, type, period: 20 };
    case "atr_trailing":
      return { ...base, type, period: 22, atr_period: 22, k: 3 };
    case "volatility_breakout":
      return { ...base, type, k: 0.5 };
    case "keltner":
      return { ...base, type, ema_period: 20, atr_period: 10, mult: 2 };
    case "stochastic":
      return { ...base, type, k_period: 14, d_period: 3, lower: 20, upper: 80 };
    case "custom":
      // 기본 예시: SMA(5)/SMA(20) 골든·데드 크로스(사용자가 자유롭게 편집).
      return {
        ...base,
        type,
        entry: {
          combinator: "and",
          children: [
            {
              left: { kind: "sma", period: 5 },
              op: ">",
              right: { kind: "sma", period: 20 },
            },
          ],
        },
        exit: {
          combinator: "and",
          children: [
            {
              left: { kind: "sma", period: 5 },
              op: "<",
              right: { kind: "sma", period: 20 },
            },
          ],
        },
      };
  }
}

/** 비율(0~1)을 "N%" 문자열로(없으면 "-"). */
function pctLabel(x: number | null | undefined): string {
  if (x === null || x === undefined) return "-";
  return `${(x * 100).toFixed(2).replace(/\.?0+$/, "")}%`;
}

/**
 * 전략 설정을 카드/상세용 한 줄 요약으로 변환한다.
 * 예: "삼성전자 · SMA 5/20" 형태의 파라미터 요약.
 */
export function summarizeConfig(c: StrategyConfig): string {
  const label = STRATEGY_TYPE_LABELS[c.type];
  // 리밸런싱은 단일 종목이 아니므로 별도 요약(종목수·선정규칙·주기).
  if (c.type === "rebalance") {
    const cadenceLabel = { daily: "일간", weekly: "주간", monthly: "월간" }[c.cadence];
    const sel =
      c.selection.method === "momentum"
        ? `모멘텀 상위 ${c.selection.top_n}`
        : "전체 동일비중";
    return `${label} · ${c.universe.length}종목 · ${sel} · ${cadenceLabel} 리밸런싱`;
  }
  let params: string;
  switch (c.type) {
    case "sma_crossover":
    case "ema_crossover":
      params = `${c.fast}/${c.slow}`;
      break;
    case "rsi":
      params = `${c.period} (${c.lower}/${c.upper})`;
      break;
    case "macd":
      params = `${c.fast}/${c.slow}/${c.signal}`;
      break;
    case "bollinger":
      params = `${c.period}±${c.num_std}σ`;
      break;
    case "breakout":
      params = `${c.period}일`;
      break;
    case "momentum":
      params = `${c.lookback}일`;
      break;
    case "zscore":
      params = `${c.period} (±${c.entry}σ)`;
      break;
    case "disparity":
      params = `${c.period} (${c.lower}/${c.upper})`;
      break;
    case "donchian_squeeze":
      params = `${c.period} (BB ${c.bb_mult}/KC ${c.kc_mult})`;
      break;
    case "trix":
      params = `${c.period}/${c.signal_period}`;
      break;
    case "obv_trend":
      params = `MA ${c.period}`;
      break;
    case "atr_trailing":
      params = `${c.period}일 · ATR ${c.atr_period}×${c.k}`;
      break;
    case "volatility_breakout":
      params = `k=${c.k}`;
      break;
    case "keltner":
      params = `EMA ${c.ema_period} · ATR ${c.atr_period}×${c.mult}`;
      break;
    case "stochastic":
      params = `${c.k_period}/${c.d_period} (${c.lower}/${c.upper})`;
      break;
    case "custom":
      params = `진입 ${countConditions(c.entry)}개 · 청산 ${countConditions(c.exit)}개`;
      break;
  }
  const risk: string[] = [];
  if (c.stop_loss_pct) risk.push(`손절 ${pctLabel(c.stop_loss_pct)}`);
  if (c.take_profit_pct) risk.push(`익절 ${pctLabel(c.take_profit_pct)}`);
  if (c.trailing_stop_pct) risk.push(`트레일링 ${pctLabel(c.trailing_stop_pct)}`);
  const riskStr = risk.length ? ` · ${risk.join(" · ")}` : "";
  return `${c.symbol} · ${label} ${params}${riskStr}`;
}

// ───────────────── 사용자 정의 규칙 표시(수식) 헬퍼 ─────────────────

/** price 피연산자 source → 한글 라벨. */
export const PRICE_SOURCE_LABELS: Record<string, string> = {
  close: "종가",
  open: "시가",
  high: "고가",
  low: "저가",
  volume: "거래량",
};

/** 피연산자 종류 → 드롭다운 라벨(룰 빌더·수식 공용). */
export const OPERAND_KIND_LABELS: Record<Operand["kind"], string> = {
  price: "가격",
  const: "상수",
  sma: "SMA",
  ema: "EMA",
  rsi: "RSI",
  macd_line: "MACD선",
  macd_signal: "시그널선",
};

/** 비교 연산자 → 사람이 읽는 라벨. */
export const COMPARE_OP_LABELS: Record<Condition["op"], string> = {
  ">": "> (상향 돌파)",
  "<": "< (하향 돌파)",
  ">=": "≥",
  "<=": "≤",
};

/** 단일 피연산자를 수식 문자열로 변환한다(예: "SMA(5)", "종가", "30"). */
export function formatOperand(op: Operand): string {
  switch (op.kind) {
    case "price":
      return PRICE_SOURCE_LABELS[op.source ?? "close"] ?? op.source ?? "종가";
    case "const":
      return op.value === null || op.value === undefined ? "?" : String(op.value);
    case "sma":
    case "ema":
    case "rsi":
      return `${OPERAND_KIND_LABELS[op.kind]}(${op.period ?? "?"})`;
    case "macd_line":
      return `MACD(${op.fast ?? "?"},${op.slow ?? "?"},${op.signal ?? "?"})`;
    case "macd_signal":
      return `Signal(${op.fast ?? "?"},${op.slow ?? "?"},${op.signal ?? "?"})`;
  }
}

/** 단일 조건을 수식 문자열로(예: "SMA(5) > SMA(20)"). */
export function formatCondition(c: Condition): string {
  return `${formatOperand(c.left)} ${c.op} ${formatOperand(c.right)}`;
}

/** 논리식 노드(조건/그룹)의 모든 잎 조건을 평탄화해 배열로 반환한다. */
export function flattenConditions(node: RuleNode): Condition[] {
  if (!isGroup(node)) return [node];
  return node.children.flatMap(flattenConditions);
}

/** 논리식 노드(조건/그룹)에 포함된 잎 조건 개수를 센다. */
export function countConditions(node: RuleNode): number {
  return flattenConditions(node).length;
}

/**
 * 논리식 노드를 수식 문자열로 변환한다(중첩 그룹은 괄호로 묶어 AND/OR 결합).
 * 예: "(RSI(14) < 30) OR (SMA(5) > SMA(20) AND MACD(12,26,9) > Signal(12,26,9))".
 */
export function formatNode(node: RuleNode, top = false): string {
  if (!isGroup(node)) return formatCondition(node);
  const sep = node.combinator === "or" ? "  OR  " : "  AND  ";
  const inner = node.children.length
    ? node.children.map((c) => formatNode(c)).join(sep)
    : "(없음)";
  // 최상위 그룹은 괄호 생략, 자식이 1개뿐이면 불필요한 괄호 생략.
  return top || node.children.length <= 1 ? inner : `(${inner})`;
}

/** 사용자 정의 전략의 진입/청산 논리식을 수식 문자열로 변환한다. */
export function formatCustomFormula(cfg: {
  entry: ConditionGroup;
  exit: ConditionGroup;
}): { entry: string; exit: string } {
  return { entry: formatNode(cfg.entry, true), exit: formatNode(cfg.exit, true) };
}

/** 전략 알고리즘 설명 + 수식(유형 선택 시 표시). custom 은 규칙에서 동적 생성. */
export const STRATEGY_INFO: Record<
  Exclude<StrategyType, "custom" | "rebalance">,
  { description: string; formula: string[] }
> = {
  sma_crossover: {
    description: "단기·장기 단순이동평균(SMA)의 골든/데드 크로스를 추종한다.",
    formula: [
      "진입: SMA(fast) 가 SMA(slow) 를 상향 돌파(골든크로스)",
      "청산: SMA(fast) 가 SMA(slow) 를 하향 돌파(데드크로스)",
    ],
  },
  ema_crossover: {
    description: "단기·장기 지수이동평균(EMA) 크로스를 추종한다(SMA보다 최근값 가중).",
    formula: [
      "진입: EMA(fast) 가 EMA(slow) 를 상향 돌파",
      "청산: EMA(fast) 가 EMA(slow) 를 하향 돌파",
    ],
  },
  rsi: {
    description: "RSI 과매도/과매수 복귀를 이용한 평균회귀 전략.",
    formula: [
      "진입: RSI 가 lower(과매도)를 상향 복귀",
      "청산: RSI 가 upper(과매수)를 하향 이탈",
    ],
  },
  macd: {
    description: "MACD선과 시그널선의 교차로 추세 전환을 포착한다.",
    formula: [
      "MACD = EMA(fast) − EMA(slow), Signal = EMA(MACD, signal)",
      "진입: MACD 가 Signal 상향 돌파 / 청산: 하향 돌파",
    ],
  },
  bollinger: {
    description: "볼린저밴드 평균회귀: 밴드 이탈 후 복귀를 매매한다.",
    formula: [
      "Mid = SMA(period), 밴드 = Mid ± num_std·σ",
      "진입: 종가가 하단밴드 상향 복귀 / 청산: 상단밴드 하향 이탈",
    ],
  },
  breakout: {
    description: "Donchian 채널 돌파 추종(당일 종가 제외해 미래참조 방지).",
    formula: [
      "진입: 종가가 직전 N봉 최고가 상향 돌파",
      "청산: 종가가 직전 N봉 최저가 하향 돌파",
    ],
  },
  momentum: {
    description: "절대(타임시리즈) 모멘텀: 과거 수익률 부호 전환을 매매한다.",
    formula: [
      "mom = close / close(t−lookback) − 1",
      "진입: mom 이 0 상향 돌파 / 청산: 0 하향 돌파",
    ],
  },
  zscore: {
    description: "이동평균 대비 표준화 편차(z-score) 평균회귀.",
    formula: [
      "z = (close − SMA) / σ",
      "진입: z 가 −entry 상향 복귀 / 청산: z 가 0 상향 돌파",
    ],
  },
  disparity: {
    description: "이격도(종가/이동평균 %) 평균회귀.",
    formula: [
      "disparity = 100 × close / SMA(period)",
      "진입: lower 상향 복귀 / 청산: upper 하향 이탈",
    ],
  },
  donchian_squeeze: {
    description: "변동성 스퀴즈(저변동 압축) 해제 시 추세 방향으로 진입한다.",
    formula: [
      "squeeze: bb_mult·std(Δ) < kc_mult·mean|Δ|",
      "진입: 스퀴즈 해제 & 종가>중심선 / 청산: 종가가 중심선 하향 돌파",
    ],
  },
  trix: {
    description: "TRIX(삼중지수평활 변화율) 모멘텀: 시그널선 교차로 매매.",
    formula: [
      "TRIX = 삼중 EMA(period) 의 변화율(%)",
      "진입: TRIX 가 시그널선 상향 돌파 / 청산: 하향 돌파",
    ],
  },
  obv_trend: {
    description: "OBV(누적 거래량) 추세 확인: OBV가 자기 이동평균을 교차할 때 매매.",
    formula: [
      "OBV = Σ sign(Δclose)·volume, OBV_MA = SMA(OBV, period)",
      "진입: OBV 가 OBV_MA 상향 돌파 / 청산: 하향 돌파",
    ],
  },
  atr_trailing: {
    description: "Donchian 돌파 진입 + 샹들리에(ATR 트레일링) 청산.",
    formula: [
      "진입: 종가가 직전 N봉 고가 상향 돌파",
      "청산: 종가 < 최고가(N봉) − k·ATR(atr_period)",
    ],
  },
  volatility_breakout: {
    description: "변동성 돌파(래리 윌리엄스): 시가 + k·전일 변동폭 돌파 시 진입, 당일 종가 청산.",
    formula: [
      "Target = Open + k·(전일 High − 전일 Low)",
      "진입: 당일 High ≥ Target / 청산: 진입 다음 봉 종가",
    ],
  },
  keltner: {
    description: "켈트너 채널 돌파 추종(EMA 중심선 ± ATR 밴드).",
    formula: [
      "Mid = EMA(ema_period), Upper = Mid + mult·ATR(atr_period)",
      "진입: 종가가 Upper 상향 돌파 / 청산: 종가가 Mid 하향 돌파",
    ],
  },
  stochastic: {
    description: "스토캐스틱 %K/%D 교차로 과매도 매수·과매수 매도.",
    formula: [
      "%K = 100×(close − 최저저)/(최고고 − 최저저), %D = SMA(%K, d_period)",
      "진입: 과매도(%K<lower)에서 %K 상향교차 / 청산: 과매수(%K>upper)에서 하향교차",
    ],
  },
};
