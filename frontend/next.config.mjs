/** @type {import('next').NextConfig} */

// 프론트 dev 서버(:3000)로 직접 접속했을 때도 /api 호출이 동작하도록,
// 백엔드(web)로 프록시(rewrite)한다. 표준 운용은 단일 출처 프록시(:8080)이며,
// 이 rewrite 는 로컬 개발 편의를 위한 것이다.
//
// 주의: Next.js rewrite 는 HTTP 만 프록시하고 WebSocket(/ws)은 프록시하지
// 못한다. 실시간(WS) 화면까지 쓰려면 :8080(Caddy 프록시)으로 접속해야 한다.
//
// 백엔드 주소: 도커 네트워크에서는 http://web:8000 으로 닿는다.
// 호스트에서 직접 `npm run dev` 하면 BACKEND_INTERNAL_URL 로 덮어쓴다.
const BACKEND = process.env.BACKEND_INTERNAL_URL || "http://web:8000";

const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${BACKEND}/api/:path*` },
      { source: "/health", destination: `${BACKEND}/health` },
    ];
  },
};

export default nextConfig;
