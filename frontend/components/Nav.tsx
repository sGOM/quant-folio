"use client";

import { useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  LayoutDashboard,
  LineChart,
  Activity,
  Settings,
  BookOpen,
  LogOut,
  Share2,
} from "lucide-react";
import { useLogout } from "@/lib/useAuth";
import { GlossaryDrawer } from "@/components/GlossaryDrawer";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/** 상단 내비게이션 메뉴 항목(경로 → 표시 라벨·아이콘). */
const LINKS = [
  { href: "/dashboard", label: "대시보드", icon: LayoutDashboard },
  { href: "/strategies", label: "전략", icon: LineChart },
  { href: "/strategies/shared", label: "공유 전략", icon: Share2 },
  { href: "/monitor", label: "실시간", icon: Activity },
  { href: "/settings", label: "설정", icon: Settings },
];

/**
 * 현재 경로가 메뉴 링크에 해당하는지 판정한다.
 * "/strategies" 가 "/strategies/shared" 의 prefix 이므로, 더 구체적인(긴) 링크가
 * 매칭되는 경우 덜 구체적인 링크는 비활성으로 둔다.
 */
function isActive(pathname: string, href: string): boolean {
  if (pathname !== href && !pathname.startsWith(href + "/")) return false;
  // 이 링크보다 더 길게(구체적으로) 매칭되는 다른 링크가 있으면 양보한다.
  return !LINKS.some(
    (other) =>
      other.href.length > href.length &&
      (pathname === other.href || pathname.startsWith(other.href + "/")),
  );
}

/**
 * 보호 페이지 상단 내비게이션 바.
 * 현재 경로에 해당하는 메뉴를 강조하고, 로그아웃 버튼을 제공한다.
 */
export function Nav() {
  const pathname = usePathname();
  const logout = useLogout();
  const [glossaryOpen, setGlossaryOpen] = useState(false);

  return (
    <nav className="sticky top-0 z-40 border-b border-border/60 bg-background/80 backdrop-blur supports-[backdrop-filter]:bg-background/60">
      <div className="mx-auto flex h-14 max-w-6xl items-center justify-between px-4 sm:px-6">
        <div className="flex items-center gap-6">
          <Link
            href="/dashboard"
            className="flex items-center gap-2 font-semibold tracking-tight"
          >
            <span className="flex h-6 w-6 items-center justify-center rounded-md bg-primary text-primary-foreground">
              <LineChart className="h-4 w-4" />
            </span>
            QuantFolio
          </Link>
          <div className="hidden gap-1 md:flex">
            {LINKS.map((l) => {
              const active = isActive(pathname, l.href);
              const Icon = l.icon;
              return (
                <Link
                  key={l.href}
                  href={l.href}
                  className={cn(
                    "flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
                    active
                      ? "bg-accent text-foreground"
                      : "text-muted-foreground hover:bg-accent/50 hover:text-foreground",
                  )}
                >
                  <Icon className="h-4 w-4" />
                  {l.label}
                </Link>
              );
            })}
          </div>
        </div>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setGlossaryOpen(true)}
            className="text-muted-foreground"
          >
            <BookOpen className="h-4 w-4" />
            <span className="hidden sm:inline">용어집</span>
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => logout.mutate()}
            disabled={logout.isPending}
            className="text-muted-foreground"
          >
            <LogOut className="h-4 w-4" />
            <span className="hidden sm:inline">로그아웃</span>
          </Button>
        </div>
      </div>
      {/* 모바일: 아이콘 탭 바 */}
      <div className="flex items-center gap-1 overflow-x-auto px-2 pb-2 md:hidden">
        {LINKS.map((l) => {
          const active = pathname.startsWith(l.href);
          const Icon = l.icon;
          return (
            <Link
              key={l.href}
              href={l.href}
              className={cn(
                "flex shrink-0 items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
                active
                  ? "bg-accent text-foreground"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              <Icon className="h-3.5 w-3.5" />
              {l.label}
            </Link>
          );
        })}
      </div>
      <GlossaryDrawer open={glossaryOpen} onClose={() => setGlossaryOpen(false)} />
    </nav>
  );
}
