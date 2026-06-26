"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  GLOSSARY,
  GlossaryCategory,
  GlossaryTerm,
} from "@/lib/glossary";

/** 카테고리 필터 칩 노출 순서. "전체"는 별도 처리. */
const CATEGORIES: GlossaryCategory[] = [
  "지표",
  "전략",
  "리스크",
  "성과",
  "시장",
  "방법론",
];

/**
 * 단일 용어가 검색어에 매칭되는지 판별한다(표제어·별칭·정의 본문, 대소문자 무시).
 * @param t 용어 항목
 * @param q 정규화된(소문자) 검색어
 */
function matches(t: GlossaryTerm, q: string): boolean {
  if (!q) return true;
  const haystack = [t.term, ...(t.aliases ?? []), t.definition]
    .join(" ")
    .toLowerCase();
  return haystack.includes(q);
}

/**
 * 금융 용어집 슬라이드 오버 드로어.
 * 우측에서 미끄러져 나오며, 검색어·카테고리로 용어를 필터링한다.
 * @param open  열림 여부
 * @param onClose 닫기 콜백(배경 클릭·X·ESC)
 */
export function GlossaryDrawer({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<GlossaryCategory | "전체">("전체");
  const inputRef = useRef<HTMLInputElement>(null);
  // Nav의 backdrop-filter가 fixed 자손의 containing block을 가로채므로, 드로어는
  // body로 포털 렌더해 viewport 기준 전체 화면을 덮게 한다. 포털은 클라이언트에서만.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  // ESC 키로 닫기.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // 열렸을 때만 검색창에 포커스. autoFocus를 쓰면 닫힌 드로어가 페이지 로드 시
  // 포커스를 가로채 화면 밖 패널이 노출되는 버그가 생기므로 직접 제어한다.
  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    return GLOSSARY.filter(
      (t) =>
        (category === "전체" || t.category === category) && matches(t, q),
    );
  }, [query, category]);

  if (!mounted) return null;

  return createPortal(
    // 닫힘 상태의 패널은 translate-x-full로 화면 밖으로 밀려나는데, 그대로 두면
    // 문서 가로 스크롤 영역이 늘어난다. overflow-hidden 래퍼로 화면 밖 영역을 잘라낸다.
    <div
      className={`fixed inset-0 z-50 overflow-hidden ${
        open ? "" : "pointer-events-none"
      }`}
      aria-hidden={!open}
    >
      {/* 배경 오버레이 */}
      <div
        onClick={onClose}
        className={`absolute inset-0 bg-black/50 transition-opacity ${
          open ? "opacity-100" : "opacity-0"
        }`}
        aria-hidden
      />

      {/* 드로어 패널 */}
      <aside
        role="dialog"
        aria-label="금융 용어집"
        aria-modal="true"
        className={`absolute right-0 top-0 flex h-full w-full max-w-md flex-col border-l border-border bg-background shadow-xl transition-[transform,visibility] duration-200 ${
          open ? "visible translate-x-0" : "invisible translate-x-full"
        }`}
      >
        <header className="flex items-center justify-between border-b border-border px-5 py-4">
          <h2 className="text-base font-semibold">금융 용어집</h2>
          <button
            onClick={onClose}
            aria-label="닫기"
            className="rounded-md px-2 py-1 text-muted-foreground hover:bg-accent hover:text-foreground"
          >
            ✕
          </button>
        </header>

        <div className="space-y-3 border-b border-border px-5 py-4">
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="용어 검색 (예: 샤프, MDD, RSI, 손절)"
            className="w-full rounded-md border border-input bg-card px-3 py-2 text-sm outline-none focus:border-ring"
          />
          <div className="flex flex-wrap gap-1.5">
            {(["전체", ...CATEGORIES] as const).map((c) => (
              <button
                key={c}
                onClick={() => setCategory(c)}
                className={`rounded-full px-2.5 py-1 text-xs ${
                  category === c
                    ? "bg-primary text-white"
                    : "bg-secondary text-foreground hover:bg-accent"
                }`}
              >
                {c}
              </button>
            ))}
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4">
          <p className="mb-3 text-xs text-muted-foreground">{results.length}개 용어</p>
          {results.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              일치하는 용어가 없습니다.
            </p>
          ) : (
            <ul className="space-y-4">
              {results.map((t) => (
                <li
                  key={t.term}
                  tabIndex={t.detail ? 0 : undefined}
                  className="group relative rounded-lg border border-border bg-card p-4 outline-none focus-visible:border-border"
                >
                  <div className="flex items-baseline justify-between gap-2">
                    <h3 className="font-medium text-foreground">
                      {t.term}
                      {t.detail && (
                        <span
                          aria-hidden
                          className="ml-1 align-middle text-xs text-muted-foreground group-hover:text-primary"
                          title="자세히 보기"
                        >
                          ⓘ
                        </span>
                      )}
                    </h3>
                    <span className="shrink-0 rounded-full bg-secondary px-2 py-0.5 text-[10px] text-muted-foreground">
                      {t.category}
                    </span>
                  </div>
                  {t.aliases && t.aliases.length > 0 && (
                    <p className="mt-0.5 text-xs text-muted-foreground">
                      {t.aliases.join(" · ")}
                    </p>
                  )}
                  <p className="mt-2 text-sm leading-relaxed text-foreground">
                    {t.definition}
                  </p>

                  {/* 호버/포커스 시 나타나는 심화 설명 툴팁. 드로어가 우측이라 카드 왼쪽에 띄운다. */}
                  {t.detail && (
                    <div
                      role="tooltip"
                      className="pointer-events-none absolute right-full top-0 z-10 mr-3 w-80 max-w-[calc(100vw-3rem)] translate-x-2 rounded-lg border border-input bg-secondary p-3 text-xs leading-relaxed text-foreground opacity-0 shadow-xl transition-all duration-150 group-hover:translate-x-0 group-hover:opacity-100 group-focus-within:translate-x-0 group-focus-within:opacity-100"
                    >
                      <p className="mb-1 font-medium text-foreground">{t.term} · 자세히</p>
                      {t.detail}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </aside>
    </div>,
    document.body,
  );
}
