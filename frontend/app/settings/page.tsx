"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, AlertCircle, Loader2, Globe, Building2 } from "lucide-react";
import { api, type Broker } from "@/lib/api";
import { Nav } from "@/components/Nav";
import { RequireAuth } from "@/components/RequireAuth";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input as TextInput } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/** 브로커별 필드 라벨·안내. broker 에 따라 같은 입력칸의 의미가 달라진다. */
interface BrokerField {
  label: string;
  hint: string;
  placeholder: string;
}

const BROKER_META: Record<
  Broker,
  {
    name: string;
    market: string;
    icon: typeof Building2;
    key: BrokerField;
    secret: BrokerField;
    account: BrokerField;
    note: string;
  }
> = {
  kis: {
    name: "한국투자증권",
    market: "국내주식",
    icon: Building2,
    key: {
      label: "App Key",
      hint: "한국투자증권 개발자센터에서 발급한 앱 키",
      placeholder: "PSxxxxxxxx...",
    },
    secret: {
      label: "App Secret",
      hint: "앱 키와 함께 발급된 시크릿",
      placeholder: "••••••••",
    },
    account: {
      label: "계좌번호",
      hint: "계좌번호-상품코드 형식 (예: 50012345-01)",
      placeholder: "50012345-01",
    },
    note: "국내주식 모의투자 API 키를 등록하세요. 키는 서버에서 암호화 저장됩니다.",
  },
  toss: {
    name: "토스증권",
    market: "해외주식",
    icon: Globe,
    key: {
      label: "App Key (토스 client_id)",
      hint: "토스증권 개발자센터에서 발급한 client_id",
      placeholder: "토스 개발자센터의 client_id",
    },
    secret: {
      label: "App Secret (토스 client_secret)",
      hint: "client_id와 함께 발급된 client_secret",
      placeholder: "••••••••",
    },
    account: {
      label: "계좌번호 (토스 accountSeq)",
      hint: "토스 계좌 식별번호(accountSeq) — 토스앱·개발자센터에서 확인",
      placeholder: "토스 accountSeq",
    },
    note: "해외주식·국내주식 시세를 조회할 수 있습니다. 토스는 모의투자 환경이 없어 항상 실거래로 동작합니다. 키는 서버에서 암호화 저장됩니다.",
  },
};

/** 설정 라우트. 인증 게이트로 감싼 실제 콘텐츠를 렌더한다. */
export default function SettingsPage() {
  return (
    <RequireAuth>
      <SettingsContent />
    </RequireAuth>
  );
}

/** 증권사 선택 + 자격증명을 등록·갱신하는 설정 본문. 저장 성공 시 관련 쿼리를 무효화한다. */
function SettingsContent() {
  const qc = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });

  const [broker, setBroker] = useState<Broker>("kis");
  const [appKey, setAppKey] = useState("");
  const [appSecret, setAppSecret] = useState("");
  const [accountNo, setAccountNo] = useState("");

  // 최초 사용자 정보 로드 시 현재 브로커로 초기화.
  useEffect(() => {
    if (me.data?.broker) setBroker(me.data.broker);
  }, [me.data?.broker]);

  const save = useMutation({
    mutationFn: () => api.registerKis(broker, appKey, appSecret, accountNo),
    onSuccess: () => {
      setAppKey("");
      setAppSecret("");
      qc.invalidateQueries({ queryKey: ["me"] });
      qc.invalidateQueries({ queryKey: ["kis-health"] });
    },
  });

  const meta = BROKER_META[broker];

  return (
    <>
      <Nav />
      <main className="mx-auto max-w-xl px-4 py-8 sm:px-6">
        <h1 className="text-2xl font-semibold tracking-tight">설정</h1>

        <Card className="mt-6">
          <CardHeader>
            <div className="flex items-center justify-between">
              <CardTitle className="text-base">증권사 연동</CardTitle>
              {me.data?.has_kis_credentials ? (
                <Badge variant="success">등록됨</Badge>
              ) : (
                <Badge variant="muted">미등록</Badge>
              )}
            </div>
            <CardDescription>
              사용할 증권사를 선택하고 API 자격증명을 등록하세요.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            {/* 현재 저장된 주문 브로커 — 자동매매 주문이 실제로 나가는 계좌 */}
            {me.data && (
              <div className="flex items-center gap-2 rounded-lg border border-primary/30 bg-primary/5 px-3 py-2.5 text-sm">
                {(() => {
                  const CurrentIcon = BROKER_META[me.data.broker].icon;
                  return <CurrentIcon className="h-4 w-4 text-primary" />;
                })()}
                <span className="text-muted-foreground">현재 주문 브로커</span>
                <span className="font-medium">
                  {BROKER_META[me.data.broker].name}
                </span>
                <Badge variant="muted" className="ml-auto">
                  {BROKER_META[me.data.broker].market}
                </Badge>
              </div>
            )}

            {/* 브로커 선택 세그먼트 */}
            <div className="grid grid-cols-2 gap-2">
              {(Object.keys(BROKER_META) as Broker[]).map((b) => {
                const m = BROKER_META[b];
                const Icon = m.icon;
                const active = broker === b;
                return (
                  <button
                    key={b}
                    type="button"
                    onClick={() => setBroker(b)}
                    className={cn(
                      "flex flex-col items-start gap-1 rounded-lg border p-3 text-left transition-colors",
                      active
                        ? "border-primary bg-primary/10"
                        : "border-border hover:bg-accent/50",
                    )}
                  >
                    <span className="flex items-center gap-1.5 text-sm font-medium">
                      <Icon className="h-4 w-4" />
                      {m.name}
                    </span>
                    <span className="text-xs text-muted-foreground">{m.market}</span>
                  </button>
                );
              })}
            </div>

            <p className="text-xs text-muted-foreground">{meta.note}</p>

            <form
              onSubmit={(e) => {
                e.preventDefault();
                save.mutate();
              }}
              className="space-y-4"
            >
              <Field
                label={meta.key.label}
                hint={meta.key.hint}
                value={appKey}
                onChange={setAppKey}
                placeholder={meta.key.placeholder}
              />
              <Field
                label={meta.secret.label}
                hint={meta.secret.hint}
                value={appSecret}
                onChange={setAppSecret}
                type="password"
                placeholder={meta.secret.placeholder}
              />
              <Field
                label={meta.account.label}
                hint={meta.account.hint}
                value={accountNo}
                onChange={setAccountNo}
                placeholder={meta.account.placeholder}
              />

              {save.isError && (
                <p className="flex items-center gap-1.5 text-sm text-destructive">
                  <AlertCircle className="h-4 w-4 shrink-0" />
                  {(save.error as Error).message}
                </p>
              )}
              {save.isSuccess && (
                <p className="flex items-center gap-1.5 text-sm text-profit">
                  <CheckCircle2 className="h-4 w-4 shrink-0" />
                  {meta.name} 연동 정보가 저장되었습니다.
                </p>
              )}

              <Button type="submit" disabled={save.isPending}>
                {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
                {save.isPending ? "저장 중…" : "저장"}
              </Button>
            </form>
          </CardContent>
        </Card>

        <TossQuoteCard registered={!!me.data?.has_toss_quote} />
      </main>
    </>
  );
}

/**
 * 통합 시세(국내+해외) 전용 토스 자격증명 등록 카드.
 * 주문 브로커와 독립적으로, 등록 시 워치리스트 시세가 토스로 통합된다.
 * @param registered 현재 토스 시세 연동 여부(배지 표시용)
 */
function TossQuoteCard({ registered }: { registered: boolean }) {
  const qc = useQueryClient();
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [accountSeq, setAccountSeq] = useState("");

  const save = useMutation({
    mutationFn: () =>
      api.registerTossQuote(clientId, clientSecret, accountSeq),
    onSuccess: () => {
      setClientSecret("");
      qc.invalidateQueries({ queryKey: ["me"] });
    },
  });

  return (
    <Card className="mt-4">
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-1.5 text-base">
            <Globe className="h-4 w-4" /> 통합 시세 연동 (토스)
          </CardTitle>
          {registered ? (
            <Badge variant="success">연동됨</Badge>
          ) : (
            <Badge variant="muted">미연동</Badge>
          )}
        </div>
        <CardDescription>
          토스 시세 연동 시 국내·해외 종목을 한 워치리스트에서 함께 모니터링할 수
          있습니다. 주문/자동매매 브로커와는 별개입니다.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
          className="space-y-4"
        >
          <Field
            label="App Key (토스 client_id)"
            hint="토스증권 개발자센터에서 발급한 client_id"
            value={clientId}
            onChange={setClientId}
            placeholder="토스 개발자센터의 client_id"
          />
          <Field
            label="App Secret (토스 client_secret)"
            hint="client_id와 함께 발급된 client_secret"
            value={clientSecret}
            onChange={setClientSecret}
            type="password"
            placeholder="••••••••"
          />
          <Field
            label="계좌번호 (토스 accountSeq)"
            hint="토스 계좌 식별번호(accountSeq)"
            value={accountSeq}
            onChange={setAccountSeq}
            placeholder="토스 accountSeq"
          />

          {save.isError && (
            <p className="flex items-center gap-1.5 text-sm text-destructive">
              <AlertCircle className="h-4 w-4 shrink-0" />
              {(save.error as Error).message}
            </p>
          )}
          {save.isSuccess && (
            <p className="flex items-center gap-1.5 text-sm text-profit">
              <CheckCircle2 className="h-4 w-4 shrink-0" />
              토스 시세 연동 정보가 저장되었습니다.
            </p>
          )}

          <Button type="submit" disabled={save.isPending}>
            {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
            {save.isPending ? "저장 중…" : "저장"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

/**
 * 라벨이 달린 텍스트 입력 필드.
 * @param label       필드 라벨
 * @param value       현재 값(제어 컴포넌트)
 * @param onChange    값 변경 콜백
 * @param type        input type(기본 "text")
 * @param placeholder 안내 문구
 */
function Field({
  label,
  hint,
  value,
  onChange,
  type = "text",
  placeholder,
}: {
  label: string;
  hint?: string;
  value: string;
  onChange: (v: string) => void;
  type?: string;
  placeholder?: string;
}) {
  return (
    <div className="space-y-1.5">
      <Label>{label}</Label>
      <TextInput
        required
        type={type}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}
