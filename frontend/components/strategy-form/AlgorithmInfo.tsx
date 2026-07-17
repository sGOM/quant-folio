"use client";

import { StrategyConfig } from "@/lib/api";
import { STRATEGY_INFO, formatCustomFormula } from "@/lib/strategy";

/**
 * 전략 알고리즘 설명·수식 카드(유형 선택 시 표시).
 * custom 은 현재 규칙으로부터 수식을 동적 생성한다.
 */
export function AlgorithmInfo({ config }: { config: StrategyConfig }) {
  if (config.type === "custom") {
    const { entry, exit } = formatCustomFormula(config);
    return (
      <div className="rounded-md border border-border bg-muted/30 p-3 text-xs">
        <p className="text-foreground">사용자가 진입·청산 규칙을 직접 조합하는 전략입니다.</p>
        <div className="mt-2 space-y-1 font-mono text-[12px] text-muted-foreground">
          <p>진입: {entry}</p>
          <p>청산: {exit}</p>
        </div>
      </div>
    );
  }

  if (config.type === "rebalance") {
    return (
      <div className="rounded-md border border-border bg-muted/30 p-3 text-xs">
        <p className="text-foreground">
          여러 종목(universe)을 운용하며 주기적으로 목표 비중을 재산정해, 목표 대비
          편차가 드리프트 밴드를 넘는 종목만 매매(리밸런싱)합니다.
        </p>
        <div className="mt-2 space-y-1 font-mono text-[12px] text-muted-foreground">
          <p>선정: 모멘텀(룩백 수익률) 상위 N 종목 또는 전체</p>
          <p>비중: 선정 종목 동일비중(1/N)</p>
          <p>매매: |현재비중 − 목표비중| &gt; 드리프트 밴드 인 종목만</p>
        </div>
      </div>
    );
  }

  const info = STRATEGY_INFO[config.type];
  return (
    <div className="rounded-md border border-border bg-muted/30 p-3 text-xs">
      <p className="text-foreground">{info.description}</p>
      <div className="mt-2 space-y-1 font-mono text-[12px] text-muted-foreground">
        {info.formula.map((line, i) => (
          <p key={i}>{line}</p>
        ))}
      </div>
    </div>
  );
}
