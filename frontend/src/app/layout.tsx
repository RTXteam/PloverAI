import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Script from "next/script";
import "./globals.css";
import { fouclessThemeBootstrap } from "@/lib/theme";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "PloverAI",
  description: "AI chat interface for PloverDB (RTX-KG2c).",
  // favicon assets live in /public. the .ico is the legacy fallback
  // for older browsers; the .svg is the scalable master so the bird
  // logo stays crisp at any tab-bar size. apple-touch-icon is the
  // iOS home-screen icon. android-chrome icons are picked up by the
  // browser automatically when listed under icons.icon.
  icons: {
    icon: [
      { url: "/favicon.svg", type: "image/svg+xml" },
      { url: "/favicon-32x32.png", sizes: "32x32", type: "image/png" },
      { url: "/favicon-16x16.png", sizes: "16x16", type: "image/png" },
      { url: "/favicon.ico", sizes: "any" },
    ],
    apple: "/apple-touch-icon.png",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: import("react").ReactNode;
}>) {
  return (
    <html
      lang="en"
      // suppressHydrationWarning is needed because the FOUC script
      // mutates html.className before React boots; without it React
      // logs a noisy "class mismatch" warning on first render.
      suppressHydrationWarning
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full">
        {/* run-once theme bootstrap. next/script with beforeInteractive
            hoists this into the document <head> and runs it before
            React hydrates, so colours are correct on the very first
            paint. content comes from lib/theme.ts so the storage key
            and logic live in one place. */}
        <Script
          id="theme-bootstrap"
          strategy="beforeInteractive"
          dangerouslySetInnerHTML={{ __html: fouclessThemeBootstrap }}
        />
        {children}
      </body>
    </html>
  );
}
