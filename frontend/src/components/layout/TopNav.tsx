"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Activity, BarChart3, Flame, LineChart, Settings, Target } from "lucide-react";
import clsx from "clsx";

const NAV_ITEMS = [
  { label: "Dashboard",       href: "/",        icon: Activity },
  { label: "Chain Explorer",  href: "/chain",   icon: BarChart3 },
  { label: "Unusual Options", href: "/unusual", icon: Flame },
  { label: "Calculator",      href: "/target",  icon: Target },
  { label: "Charts",          href: "/charts",  icon: LineChart },
  { label: "Settings",        href: "/settings",icon: Settings },
];

export default function TopNav() {
  const pathname = usePathname();

  return (
    <nav className="sticky top-0 z-50 border-b border-bg-border bg-bg-base/95 backdrop-blur-md">
      <div className="max-w-[1600px] mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex items-center h-14 gap-1">
          {/* Brand */}
          <div className="flex items-center mr-6 shrink-0">
            <img
              src="/logo.png"
              alt="OptionsFlow"
              className="h-9 w-auto object-contain"
            />
          </div>

          {/* Nav links */}
          <div className="flex items-center gap-0.5 overflow-x-auto no-scrollbar">
            {NAV_ITEMS.map(({ label, href, icon: Icon }) => {
              const active =
                href === "/" ? pathname === "/" : pathname.startsWith(href);
              return (
                <Link
                  key={href}
                  href={href}
                  className={clsx(
                    "flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-all whitespace-nowrap",
                    active
                      ? "bg-accent/10 text-accent"
                      : "text-text-secondary hover:text-text-primary hover:bg-bg-hover"
                  )}
                >
                  <Icon className="w-3.5 h-3.5" />
                  {label}
                </Link>
              );
            })}
          </div>

          {/* Spacer */}
          <div className="flex-1" />

          {/* Tradier referral CTA */}
          <a
            href="https://trade.tradier.com/raf-open/?mwr=d02539fb"
            target="_blank"
            rel="noopener noreferrer"
            className="hidden sm:flex items-center gap-1.5 text-2xs font-medium text-accent/80 hover:text-accent px-2.5 py-1 rounded-md bg-accent/5 border border-accent/20 hover:border-accent/40 transition-colors whitespace-nowrap"
          >
            Trade options with Tradier
          </a>

          {/* Provider badge */}
          <div className="hidden sm:flex items-center gap-2 text-2xs text-text-muted px-2 py-1 rounded-md bg-bg-raised border border-bg-border ml-1">
            <span className="w-1.5 h-1.5 rounded-full bg-success animate-pulse-fast" />
            Tradier
          </div>
        </div>
      </div>
    </nav>
  );
}
