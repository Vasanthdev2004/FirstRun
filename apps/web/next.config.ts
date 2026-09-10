import type { NextConfig } from "next";

// This is a deployment-owned origin, never a browser query parameter.
const apiOrigin = new URL(process.env.FIRSTRUN_API_ORIGIN ?? "http://127.0.0.1:8765");
if (!['http:', 'https:'].includes(apiOrigin.protocol) || apiOrigin.username || apiOrigin.password || apiOrigin.pathname !== '/' || apiOrigin.search || apiOrigin.hash) {
  throw new Error("FIRSTRUN_API_ORIGIN must be an HTTP(S) origin without credentials or a path");
}

const config: NextConfig = {
  agentRules: false,
  poweredByHeader: false,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiOrigin.origin}/api/:path*` }];
  },
  async headers() {
    return [{ source: "/:path*", headers: [
      { key: "X-Content-Type-Options", value: "nosniff" },
      { key: "Referrer-Policy", value: "same-origin" },
      { key: "X-Frame-Options", value: "DENY" },
      { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
    ] }];
  },
};
export default config;
