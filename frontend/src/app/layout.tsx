import type { Metadata } from "next";
import "./globals.css";
import TopNav from "@/components/layout/TopNav";
import DonationBanner from "@/components/ui/DonationBanner";

export const metadata: Metadata = {
  title: "Options Analytics",
  description: "Production-grade options flow analysis",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <head>
        {/* Prevent theme flash: apply saved theme before first paint */}
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var t=localStorage.getItem('theme');document.documentElement.className=t==='light'?'light':'dark';}catch(e){}})();`,
          }}
        />
      </head>
      <body className="min-h-screen bg-bg-base">
        <TopNav />
        <main className="max-w-[1600px] mx-auto px-4 sm:px-6 lg:px-8 py-6">
          <div className="space-y-6">
            <DonationBanner />
            {children}
          </div>
        </main>
      </body>
    </html>
  );
}
