import { describe, expect, it } from "vitest";
import { RebalanceConfig, StrategyConfig } from "@/lib/api";
import { defaultConfig } from "@/lib/strategy";
import {
  customUsesOhlc,
  validateOperand,
  validateStrategyForm,
} from "../validate";

/** 기본값에서 일부만 덮어쓴 단일종목 설정을 만든다. */
function single(over: Partial<StrategyConfig> = {}): StrategyConfig {
  return { ...defaultConfig("sma_crossover"), ...over } as StrategyConfig;
}

/** 기본값에서 일부만 덮어쓴 리밸런싱 설정을 만든다. */
function rebal(over: Partial<RebalanceConfig> = {}): RebalanceConfig {
  return { ...defaultConfig("rebalance"), ...over } as RebalanceConfig;
}

describe("validateStrategyForm — 공통", () => {
  it("이름이 비면 거부한다", () => {
    expect(validateStrategyForm("  ", single())).toMatch(/이름/);
  });

  it("기본 설정은 통과한다", () => {
    expect(validateStrategyForm("전략", single())).toBeNull();
  });

  it("종목코드·초기자본·거래비용 범위를 검증한다", () => {
    expect(validateStrategyForm("s", single({ symbol: " " }))).toMatch(/종목코드/);
    expect(validateStrategyForm("s", single({ cash: 0 }))).toMatch(/초기자본/);
    expect(validateStrategyForm("s", single({ fees: 0.02 }))).toMatch(/수수료/);
    expect(validateStrategyForm("s", single({ tax: -0.1 }))).toMatch(/거래세/);
  });

  it("손절/익절/트레일링은 0 초과 100% 이하만 허용한다", () => {
    expect(validateStrategyForm("s", single({ stop_loss_pct: 0 }))).toMatch(/비율/);
    expect(validateStrategyForm("s", single({ stop_loss_pct: 1.5 }))).toMatch(/비율/);
    expect(validateStrategyForm("s", single({ stop_loss_pct: 0.05 }))).toBeNull();
  });
});

describe("validateStrategyForm — 유형별 파라미터", () => {
  it("이평 크로스는 fast < slow 여야 한다", () => {
    expect(validateStrategyForm("s", single({ fast: 20, slow: 5 }))).toMatch(/fast/);
  });

  it("RSI 는 lower < upper 여야 한다", () => {
    const cfg = { ...defaultConfig("rsi"), lower: 70, upper: 30 } as StrategyConfig;
    expect(validateStrategyForm("s", cfg)).toMatch(/lower/);
  });

  it("custom 은 진입·청산 조건이 각 1개 이상이어야 한다", () => {
    const cfg = defaultConfig("custom") as Extract<StrategyConfig, { type: "custom" }>;
    const empty = { ...cfg, entry: { combinator: "and" as const, children: [] } };
    expect(validateStrategyForm("s", empty)).toMatch(/진입 조건/);
  });
});

describe("validateStrategyForm — 리밸런싱", () => {
  it("기본 설정은 통과한다", () => {
    expect(validateStrategyForm("s", rebal())).toBeNull();
  });

  it("고정 목록에서 universe 가 비면 거부한다", () => {
    expect(validateStrategyForm("s", rebal({ universe: [] }))).toMatch(/1개 이상/);
  });

  it("PIT 소스면 universe 를 비워도 된다", () => {
    const cfg = rebal({ universe: [] });
    cfg.selection = { ...cfg.selection, universe_rule: { source: "KOSPI200" } };
    expect(validateStrategyForm("s", cfg)).toBeNull();
  });

  it("중복 종목코드를 거부한다", () => {
    expect(
      validateStrategyForm("s", rebal({ universe: ["005930", "005930"] })),
    ).toMatch(/중복/);
  });

  it("top_n 이 universe 크기를 넘으면 거부한다", () => {
    const cfg = rebal({ universe: ["005930"] });
    cfg.selection = { ...cfg.selection, top_n: 3 };
    expect(validateStrategyForm("s", cfg)).toMatch(/top_n/);
  });

  it("멀티팩터 가중치 합이 1이 아니면 거부한다", () => {
    const cfg = rebal();
    cfg.selection = {
      ...cfg.selection,
      method: "score",
      factor_weights: { momentum: 0.5, value: 0.5, lowvol: 0.5, quality: 0, growth: 0 },
    };
    expect(validateStrategyForm("s", cfg)).toMatch(/가중치 합/);
  });

  it("점수 가중은 멀티팩터 선정일 때만 허용한다", () => {
    expect(validateStrategyForm("s", rebal({ weighting: "score" }))).toMatch(/멀티팩터/);
  });

  it("종목 집중 한도가 드리프트 밴드 이하면 거부한다", () => {
    expect(
      validateStrategyForm(
        "s",
        rebal({ drift_band_pct: 0.05, risk_layer: { max_position_pct: 0.05 } }),
      ),
    ).toMatch(/집중 한도/);
  });

  it("실행 시각 형식(HH:MM)을 검증한다", () => {
    expect(validateStrategyForm("s", rebal({ rebalance_time: "25시" }))).toMatch(/HH:MM/);
  });
});

describe("validateOperand", () => {
  it("상수는 값이 필수", () => {
    expect(validateOperand({ kind: "const", value: null })).toMatch(/상수/);
    expect(validateOperand({ kind: "const", value: 30 })).toBeNull();
  });

  it("지표는 기간 1 이상", () => {
    expect(validateOperand({ kind: "sma", period: 0 })).toMatch(/기간/);
    expect(validateOperand({ kind: "sma", period: 5 })).toBeNull();
  });

  it("MACD 는 fast<slow 와 세 값 모두 필수", () => {
    expect(validateOperand({ kind: "macd_line", fast: 12, slow: 26 })).toMatch(/모두/);
    expect(
      validateOperand({ kind: "macd_line", fast: 26, slow: 12, signal: 9 }),
    ).toMatch(/작아야/);
  });
});

describe("customUsesOhlc", () => {
  it("종가만 쓰면 false, 고가 등 참조 시 true", () => {
    const cfg = defaultConfig("custom") as Extract<StrategyConfig, { type: "custom" }>;
    expect(customUsesOhlc(cfg)).toBe(false);
    const withHigh = {
      ...cfg,
      entry: {
        combinator: "and" as const,
        children: [
          {
            left: { kind: "price" as const, source: "high" as const },
            op: ">" as const,
            right: { kind: "sma" as const, period: 20 },
          },
        ],
      },
    };
    expect(customUsesOhlc(withHigh)).toBe(true);
  });

  it("rebalance 등 custom 이외 유형은 항상 false", () => {
    expect(customUsesOhlc(rebal())).toBe(false);
  });
});
