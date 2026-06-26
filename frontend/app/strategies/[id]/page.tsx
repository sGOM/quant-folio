"use client";

import { use, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Backtest, StrategyConfig } from "@/lib/api";
import { Nav } from "@/components/Nav";
import { LineChart } from "@/components/LineChart";
import { RequireAuth } from "@/components/RequireAuth";
import { StrategyForm } from "@/components/StrategyForm";
import { summarizeConfig } from "@/lib/strategy";

/**
 * 비율(0~1)을 백분율 문자열로 변환한다.
 * @param x 비율 값(null/undefined 면 "-")
 * @returns 예: 0.1234 → "12.34%"
 */
function pct(x: number | null | undefined): string {
  if (x === null || x === undefined) return "-";
  return `${(x * 100).toFixed(2)}%`;
}

/** 전략 상세 라우트. 동적 params 를 풀어 인증 게이트로 감싼 콘텐츠에 전달한다. */
export default function StrategyDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return (
    <RequireAuth>
      <StrategyDetailContent sid={Number(id)} />
    </RequireAuth>
  );
}

/**
 * 전략 상세 본문. 기간을 지정해 백테스트를 실행하고, 최신 결과(성과 지표·자산 곡선)와
 * 실행 이력을 보여준다.
 * @param sid 전략 ID
 */
function StrategyDetailContent({ sid }: { sid: number }) {
  const qc = useQueryClient();
  const router = useRouter();

  const today = new Date().toISOString().slice(0, 10);
  const [start, setStart] = useState("2023-01-01");
  const [end, setEnd] = useState(today);
  const [editing, setEditing] = useState(false);

  const strategy = useQuery({
    queryKey: ["strategy", sid],
    queryFn: () => api.getStrategy(sid),
  });
  const backtests = useQuery({
    queryKey: ["backtests", sid],
    queryFn: () => api.listBacktests(sid),
  });

  const run = useMutation({
    mutationFn: () => api.runBacktest(sid, start, end),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["backtests", sid] });
      qc.invalidateQueries({ queryKey: ["strategy", sid] });
    },
  });

  const edit = useMutation({
    mutationFn: ({
      name,
      config,
      description,
    }: {
      name: string;
      config: StrategyConfig;
      description: string;
    }) => api.updateStrategy(sid, name, config, description),
    onSuccess: () => {
      setEditing(false);
      qc.invalidateQueries({ queryKey: ["strategy", sid] });
    },
  });

  // 대표 백테스트 지정/해제(공유 시 성과 표시용).
  const setFeatured = useMutation({
    mutationFn: (backtestId: number | null) =>
      api.setFeaturedBacktest(sid, backtestId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["strategy", sid] }),
  });

  const remove = useMutation({
    mutationFn: () => api.deleteStrategy(sid),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["strategies"] });
      router.push("/strategies");
    },
  });

  /** 삭제 전 확인 후 전략을 제거한다. */
  const handleDelete = () => {
    if (
      window.confirm(
        `'${strategy.data?.name ?? "이 전략"}'을(를) 삭제할까요?\n백테스트 이력도 함께 삭제되며 되돌릴 수 없습니다.`,
      )
    ) {
      remove.mutate();
    }
  };

  // 서버 정렬에 의존하지 않고 created_at 최신을 선택.
  const latest: Backtest | undefined = backtests.data
    ? [...backtests.data].sort(
        (a, b) => Date.parse(b.created_at) - Date.parse(a.created_at),
      )[0]
    : undefined;

  return (
    <>
      <Nav />
      <main className="mx-auto max-w-4xl px-6 py-8">
        <Link
          href="/strategies"
          className="mb-4 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          ← 전략 목록으로
        </Link>
        <div className="flex items-center justify-between">
          <h1 className="text-xl font-semibold">{strategy.data?.name ?? "전략"}</h1>
          {strategy.data && (
            <div className="flex items-center gap-2">
              <button
                onClick={() => setEditing((v) => !v)}
                className="rounded-md border border-input px-3 py-1.5 text-sm hover:bg-accent"
              >
                {editing ? "닫기" : "편집"}
              </button>
              <button
                onClick={handleDelete}
                disabled={remove.isPending}
                className="rounded-md border border-destructive/40 px-3 py-1.5 text-sm text-destructive hover:bg-destructive/10 disabled:opacity-50"
              >
                {remove.isPending ? "삭제 중…" : "삭제"}
              </button>
            </div>
          )}
        </div>
        {remove.isError && (
          <p className="mt-2 text-sm text-destructive">
            삭제 실패: {(remove.error as Error).message}
          </p>
        )}
        {strategy.data && (
          <p className="mt-1 text-sm text-muted-foreground">
            {summarizeConfig(strategy.data.config)}
            {strategy.data.config.type === "rebalance"
              ? ` · 배정자본 ${strategy.data.config.capital.toLocaleString()}원`
              : ` · 초기자본 ${strategy.data.config.cash.toLocaleString()}원`}
          </p>
        )}
        {strategy.data?.description && (
          <p className="mt-2 whitespace-pre-wrap rounded-md border border-border bg-card/50 p-3 text-sm text-foreground/90">
            {strategy.data.description}
          </p>
        )}

        {editing && strategy.data && (
          <StrategyForm
            initialName={strategy.data.name}
            initialDescription={strategy.data.description ?? ""}
            initialConfig={strategy.data.config}
            submitLabel="변경 저장"
            pending={edit.isPending}
            error={edit.isError ? (edit.error as Error).message : null}
            onSubmit={(name, config, description) =>
              edit.mutate({ name, config, description })
            }
            onCancel={() => setEditing(false)}
          />
        )}

        {strategy.data?.config.type === "rebalance" && (
          <section className="mt-6 rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground">
            리밸런싱(포트폴리오) 전략은 백테스트를 지원하지 않습니다. 운용은 모니터링
            화면에서 전략을 시작하면 설정한 주기에 따라 자동 실행됩니다.
          </section>
        )}

        {strategy.data?.config.type !== "rebalance" && (
        <section className="mt-6 flex flex-wrap items-end gap-3 rounded-lg border border-border bg-card p-4">
          <label className="space-y-1">
            <span className="block text-xs text-muted-foreground">시작일</span>
            <input
              type="date"
              value={start}
              onChange={(e) => setStart(e.target.value)}
              className="rounded-md border border-input bg-background px-3 py-2 text-sm"
            />
          </label>
          <label className="space-y-1">
            <span className="block text-xs text-muted-foreground">종료일</span>
            <input
              type="date"
              value={end}
              onChange={(e) => setEnd(e.target.value)}
              className="rounded-md border border-input bg-background px-3 py-2 text-sm"
            />
          </label>
          <button
            onClick={() => run.mutate()}
            disabled={run.isPending}
            className="rounded-md bg-primary px-4 py-2 text-sm hover:bg-primary/90 disabled:opacity-50"
          >
            {run.isPending ? "백테스트 실행 중…" : "백테스트 실행"}
          </button>
          {run.isError && (
            <span className="text-sm text-destructive">{(run.error as Error).message}</span>
          )}
        </section>
        )}

        {latest?.result && (
          <section className="mt-6 space-y-4">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <Metric label="총수익률" value={pct(latest.total_return)} accent />
              <Metric label="최대낙폭(MDD)" value={pct(latest.mdd)} />
              <Metric
                label="샤프지수"
                value={latest.sharpe?.toFixed(2) ?? "-"}
              />
              <Metric
                label="승률 / 매매수"
                value={`${pct(latest.result.win_rate)} / ${latest.result.num_trades}`}
              />
            </div>

            <div className="rounded-lg border border-border bg-card p-4">
              <h2 className="mb-2 text-sm text-muted-foreground">자산 곡선 (Equity Curve)</h2>
              <LineChart data={latest.result.equity_curve} />
            </div>
          </section>
        )}

        <section className="mt-8">
          <h2 className="mb-2 text-sm text-muted-foreground">
            백테스트 이력
            <span className="ml-2 text-xs">
              · ★ 대표로 지정하면 공유 시 성과가 함께 표시됩니다
            </span>
          </h2>
          <div className="space-y-2">
            {backtests.data?.length === 0 && (
              <p className="text-sm text-muted-foreground">아직 실행한 백테스트가 없습니다.</p>
            )}
            {backtests.data?.map((b) => {
              const featured = strategy.data?.featured_backtest_id === b.id;
              return (
                <div
                  key={b.id}
                  className={`flex flex-wrap items-center justify-between gap-2 rounded-md border px-4 py-2 text-sm ${
                    featured
                      ? "border-primary/50 bg-primary/5"
                      : "border-border bg-card"
                  }`}
                >
                  <span className="text-muted-foreground">
                    {b.period_start.slice(0, 10)} ~ {b.period_end.slice(0, 10)}
                  </span>
                  <div className="flex items-center gap-3">
                    <span>
                      수익률 {pct(b.total_return)} · MDD {pct(b.mdd)} · 샤프{" "}
                      {b.sharpe?.toFixed(2) ?? "-"}
                    </span>
                    <button
                      onClick={() => setFeatured.mutate(featured ? null : b.id)}
                      disabled={setFeatured.isPending}
                      className={`shrink-0 rounded-md border px-2 py-1 text-xs transition-colors disabled:opacity-50 ${
                        featured
                          ? "border-primary bg-primary/10 text-primary"
                          : "border-input text-muted-foreground hover:bg-accent"
                      }`}
                    >
                      {featured ? "★ 대표" : "대표 지정"}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
          {setFeatured.isError && (
            <p className="mt-2 text-sm text-destructive">
              {(setFeatured.error as Error).message}
            </p>
          )}
        </section>
      </main>
    </>
  );
}

/**
 * 성과 지표 카드(라벨 + 값).
 * @param label  지표 이름
 * @param value  표시 값
 * @param accent true 면 값을 강조색(파랑)으로 표시
 */
function Metric({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: boolean;
}) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={`mt-1 text-lg font-semibold ${accent ? "text-primary" : ""}`}>
        {value}
      </p>
    </div>
  );
}
