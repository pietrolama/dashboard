import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";
import "./index.css";
import Dashboard from "./pages/Dashboard";
import ProjectPage from "./pages/ProjectPage";
import RelationMap from "./pages/RelationMap";
import Settings from "./pages/Settings";

function Layout({ children }: { children: React.ReactNode }) {
  const link = ({ isActive }: { isActive: boolean }) =>
    `rounded-md px-3 py-1.5 text-sm ${isActive ? "bg-surface-2 text-fg" : "text-muted hover:text-fg"}`;
  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-10 border-b border-line bg-bg/85 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-7xl items-center gap-6 px-4 sm:px-6">
          <NavLink to="/" className="flex items-center gap-2 font-semibold tracking-tight">
            <svg viewBox="0 0 32 32" className="size-5" aria-hidden>
              <circle cx="16" cy="16" r="5" className="fill-accent" />
              <circle cx="16" cy="16" r="12" fill="none" className="stroke-accent" strokeWidth="2" strokeDasharray="4 4" />
            </svg>
            Project OS
          </NavLink>
          <nav className="flex gap-1">
            <NavLink to="/" end className={link}>
              Progetti
            </NavLink>
            <NavLink to="/map" className={link}>
              Mappa
            </NavLink>
            <NavLink to="/settings" className={link}>
              Sorgenti
            </NavLink>
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6">{children}</main>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <Layout>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/p/:slug" element={<ProjectPage />} />
          <Route path="/map" element={<RelationMap />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </Layout>
    </BrowserRouter>
  </StrictMode>,
);
