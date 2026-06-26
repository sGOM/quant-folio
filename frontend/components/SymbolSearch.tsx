"use client";

import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, SymbolHit } from "@/lib/api";

/** 입력 공용 스타일(shadcn input 토큰과 동일 톤). */
const INPUT =
  "flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50";

/**
 * 종목코드 입력 + 자동완성. 코드/한글명/영문명으로 검색해 선택하면 코드를 채운다.
 * 직접 코드를 타이핑하는 방식도 그대로 지원한다(value 는 항상 종목코드).
 *
 * @param value    현재 종목코드
 * @param onChange 코드 변경 콜백(선택 또는 직접 입력)
 */
export function SymbolSearch({
  value,
  onChange,
}: {
  value: string;
  onChange: (code: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);

  // 디바운스: 입력이 멈춘 뒤 250ms 후에만 검색어를 확정한다.
  const [debounced, setDebounced] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setDebounced(query.trim()), 250);
    return () => clearTimeout(t);
  }, [query]);

  const results = useQuery({
    queryKey: ["symbols", debounced],
    queryFn: () => api.searchSymbols(debounced),
    enabled: open && debounced.length >= 1,
    staleTime: 60_000,
  });

  // 바깥 클릭 시 드롭다운 닫기.
  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  /** 종목 선택 시 코드 반영 + 이름 표시 + 드롭다운 닫기. */
  function pick(s: SymbolHit) {
    onChange(s.code);
    setSelectedName(s.name);
    setQuery("");
    setOpen(false);
  }

  return (
    <div ref={boxRef} className="relative">
      <input
        value={open ? query : value}
        onChange={(e) => {
          const v = e.target.value;
          setQuery(v);
          setOpen(true);
          setSelectedName(null);
          // 숫자 코드를 직접 입력하는 경우 즉시 반영(자유 입력 지원).
          if (/^\d{0,6}$/.test(v)) onChange(v);
        }}
        onFocus={() => {
          setQuery(value);
          setOpen(true);
        }}
        placeholder="코드/종목명 검색 (예: 005930, 삼성, samsung)"
        className={INPUT}
        autoComplete="off"
      />

      {!open && selectedName && (
        <p className="mt-1 text-xs text-muted-foreground">
          {value} · {selectedName}
        </p>
      )}

      {open && debounced.length >= 1 && (
        <ul className="absolute z-20 mt-1 max-h-64 w-full overflow-y-auto rounded-md border bg-popover shadow-xl">
          {results.isLoading && (
            <li className="px-3 py-2 text-xs text-muted-foreground">검색 중…</li>
          )}
          {results.data?.length === 0 && (
            <li className="px-3 py-2 text-xs text-muted-foreground">
              일치하는 종목이 없습니다.
            </li>
          )}
          {results.data?.map((s) => (
            <li key={s.code}>
              <button
                type="button"
                onClick={() => pick(s)}
                className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm transition-colors hover:bg-accent"
              >
                <span>
                  <span className="font-medium">{s.name}</span>
                  {s.name_en && (
                    <span className="ml-1 text-xs text-muted-foreground">
                      {s.name_en}
                    </span>
                  )}
                </span>
                <span className="shrink-0 font-mono text-xs text-muted-foreground">
                  {s.code}
                  {s.market && (
                    <span className="ml-1 text-muted-foreground/60">{s.market}</span>
                  )}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
