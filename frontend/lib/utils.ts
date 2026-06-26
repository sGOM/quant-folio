import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** Tailwind 클래스를 조건부 결합하고 충돌을 정리한다(shadcn/ui 표준 유틸). */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
