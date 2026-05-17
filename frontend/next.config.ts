import type { NextConfig } from "next";

// static export so production deploys are just a folder of HTML/CSS/JS
// served by nginx on the EC2. no Node runtime in production — the
// browser talks directly to the FastAPI service over /api/*.
//
// trailingSlash matches nginx's preference for `/foo/` style URLs;
// images.unoptimized is required because the Image optimizer would
// otherwise demand a Node server at request time.
const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  images: { unoptimized: true },
};

export default nextConfig;
