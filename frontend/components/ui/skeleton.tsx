import { cn } from "@/lib/utils";

/** 로딩 자리표시자. 콘텐츠 폭·높이에 맞춰 사용한다. */
function Skeleton({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("animate-pulse rounded-md bg-muted", className)}
      {...props}
    />
  );
}

export { Skeleton };
