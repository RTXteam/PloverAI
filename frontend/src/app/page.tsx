// the single route in the app. permalinks happen via `?run=<id>` query
// param — not a dynamic /run/[id] path — because next.config.ts sets
// `output: "export"` (static export). dynamic routes in static-export
// mode would need generateStaticParams listing every run at build
// time, which we can't do because runs are created at runtime when
// users submit queries.
//
// the ?run=<id> form serves the same use case (shareable deep links)
// without any server-side routing. ChatShell reads the URL on mount.

import ChatShell from "@/components/ChatShell";

export default function Home() {
  return <ChatShell />;
}
