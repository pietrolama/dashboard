import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type Health, type Relation, type RelationGraph, type RelationNode } from "../api";
import { Chip, Empty, ErrorBox, HealthBadge, LifecycleBadge, Panel } from "../components";
import { KIND_COLOR, KindSwatch, LINK_KINDS, LinkForm, RelationList } from "../relations";
import { LINK_LABEL, relativeTime, useAsync } from "../util";

const SIZE = 900;
// Rings of the focused view: direct relations, two steps away, everything else.
const R_NEAR = 150;
const R_FAR = 250;
const R_REST = 330;
// The overview puts every project on one circle.
const R_ALL = 270;
const LABEL_MAX = 18;
const DIRECTED = new Set(["depends_on", "part_of", "evolves_from", "contains"]);
const OBSERVED_KINDS = ["same_remote", "contains"] as const;

const HEALTH_FILL: Record<Health, string> = {
  healthy: "var(--accent)",
  stale: "var(--warn)",
  missing: "var(--danger)",
  unknown: "var(--muted)",
};

type Ring = "focus" | "near" | "far" | "rest" | "all";
interface Placed {
  node: RelationNode;
  x: number;
  y: number;
  angle: number;
  ring: Ring;
}
type Point = { x: number; y: number };

export default function RelationMap() {
  const [params, setParams] = useSearchParams();
  const focus = params.get("focus");
  const showObserved = params.get("observed") !== "0";
  const showArchived = params.get("archived") === "1";
  const graph = useAsync(api.relations, []);
  const [hover, setHover] = useState<string | null>(null);

  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };

  const { nodes, edges } = useMemo(() => filterGraph(graph.data, showObserved, showArchived, focus), [graph.data, showObserved, showArchived, focus]);
  const focused = focus ? nodes.find((n) => n.slug === focus) ?? null : null;
  const layout = useMemo(() => (focused ? focusLayout(nodes, edges, focused.slug) : overviewLayout(nodes, edges)), [nodes, edges, focused]);
  const positions = useTweened(layout);

  if (graph.error) return <ErrorBox error={graph.error} />;
  if (!graph.data) return <Empty>Carico la mappa…</Empty>;

  const neighbours = (slug: string) => new Set(edges.flatMap((e) => (e.source === slug ? [e.target] : e.target === slug ? [e.source] : [])));
  const lit = hover ?? focused?.slug ?? null;
  const litSet = lit ? neighbours(lit).add(lit) : null;
  const linked = new Set(edges.flatMap((e) => [e.source, e.target]));

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Mappa delle relazioni</h1>
          <p className="mt-0.5 text-sm text-muted">
            {focused ? (
              <>
                Al centro <span className="text-fg">{focused.name}</span>; intorno le relazioni dirette, poi quelle a due passi.{" "}
                <button className="text-accent hover:underline" onClick={() => setParam("focus", null)}>
                  Vista d'insieme
                </button>
              </>
            ) : (
              <>Tutti i progetti in cerchio, raggruppati per legami. Clicca un progetto per metterlo al centro.</>
            )}
          </p>
        </div>
        <div className="flex gap-1">
          <Toggle on={showObserved} onClick={() => setParam("observed", showObserved ? "0" : null)}>
            Osservate
          </Toggle>
          <Toggle on={showArchived} onClick={() => setParam("archived", showArchived ? null : "1")}>
            Archiviati
          </Toggle>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-[1fr_340px]">
        <div className="card min-w-0 p-2">
          <svg viewBox={`${-SIZE / 2} ${-SIZE / 2} ${SIZE} ${SIZE}`} className="mx-auto block max-h-[calc(100vh-13rem)] min-h-80 w-full select-none" role="img" aria-label="Mappa radiale delle relazioni tra progetti">
            <defs>
              {[...LINK_KINDS, ...OBSERVED_KINDS].map((k) => (
                <marker key={k} id={`arrow-${k}`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                  <path d="M0,0 L8,4 L0,8 z" style={{ fill: KIND_COLOR[k] }} />
                </marker>
              ))}
            </defs>

            {focused &&
              [R_NEAR, R_FAR].map((r) => <circle key={r} r={r} fill="none" className="stroke-line" strokeDasharray="2 6" />)}

            <g fill="none">
              {edges.map((e) => {
                const a = positions[e.source];
                const b = positions[e.target];
                if (!a || !b) return null;
                const dim = litSet && !(lit === e.source || lit === e.target);
                const observed = e.origin === "observed";
                return (
                  <path
                    key={edgeKey(e)}
                    d={edgePath(a, b, nodeRadius(e.source, focus), nodeRadius(e.target, focus))}
                    style={{ stroke: KIND_COLOR[e.kind] }}
                    strokeWidth={dim ? 1 : 1.75}
                    strokeOpacity={dim ? 0.15 : e.kind === "related" ? 0.55 : 0.9}
                    strokeDasharray={observed ? "4 4" : undefined}
                    markerEnd={DIRECTED.has(e.kind) ? `url(#arrow-${e.kind})` : undefined}
                    className="transition-[stroke-opacity]"
                  >
                    <title>
                      {e.source_name} {LINK_LABEL[e.kind]} {e.target_name}
                      {e.note ? ` — ${e.note}` : ""}
                    </title>
                  </path>
                );
              })}
            </g>

            {layout.map(({ node, ring, angle }) => {
              const p = positions[node.slug];
              if (!p) return null;
              const isFocus = ring === "focus";
              const faint = (ring === "rest" || (ring === "all" && !linked.has(node.slug))) && !(litSet?.has(node.slug));
              const dim = litSet ? !litSet.has(node.slug) : false;
              return (
                <g
                  key={node.slug}
                  transform={`translate(${p.x} ${p.y})`}
                  className="cursor-pointer"
                  opacity={(dim ? 0.35 : 1) * (node.lifecycle === "archived" ? 0.6 : 1)}
                  onMouseEnter={() => setHover(node.slug)}
                  onMouseLeave={() => setHover(null)}
                  onClick={() => setParam("focus", isFocus ? null : node.slug)}
                >
                  <title>
                    {node.name}
                    {node.stack.length ? ` · ${node.stack.join(", ")}` : ""}
                  </title>
                  {isFocus && <circle r={nodeRadius(node.slug, focus) + 6} fill="none" className="stroke-accent" strokeWidth="1.5" strokeDasharray="3 3" />}
                  <circle
                    r={nodeRadius(node.slug, focus) * (faint ? 0.6 : 1)}
                    style={{ fill: node.lifecycle === "active" ? HEALTH_FILL[node.health] : "var(--surface)", stroke: HEALTH_FILL[node.health] }}
                    strokeWidth={node.lifecycle === "active" ? 0 : 2}
                  />
                  <NodeLabel node={node} ring={ring} angle={angle} faint={faint} radius={nodeRadius(node.slug, focus)} />
                </g>
              );
            })}
          </svg>
          <Legend />
        </div>

        <aside className="space-y-6">
          {focused ? (
            <FocusPanel node={focused} relations={graph.data.relations.filter((r) => r.source === focused.slug || r.target === focused.slug)} onChange={graph.reload} onSelect={(s) => setParam("focus", s)} />
          ) : (
            <Panel title="Panoramica">
              <dl className="grid grid-cols-[1fr_auto] gap-y-1.5 text-sm">
                <dt className="text-muted">Progetti in mappa</dt>
                <dd className="tabular-nums">{nodes.length}</dd>
                <dt className="text-muted">Con almeno un legame</dt>
                <dd className="tabular-nums">{linked.size}</dd>
                <dt className="text-muted">Relazioni dichiarate</dt>
                <dd className="tabular-nums">{edges.filter((e) => e.origin === "declared").length}</dd>
                <dt className="text-muted">Relazioni osservate</dt>
                <dd className="tabular-nums">{edges.filter((e) => e.origin === "observed").length}</dd>
              </dl>
              <p className="mt-3 text-xs text-muted">
                Le relazioni osservate vengono dai repository (stesso remote, una cartella dentro l'altra). Le altre le dichiari tu dalla scheda di un progetto, qui dopo averlo selezionato, o un agente col tool MCP <code className="font-mono">link_projects</code>.
              </p>
            </Panel>
          )}
        </aside>
      </div>
    </div>
  );
}

function FocusPanel({ node, relations, onChange, onSelect }: { node: RelationNode; relations: Relation[]; onChange: () => void; onSelect: (slug: string) => void }) {
  const [adding, setAdding] = useState(false);
  useEffect(() => setAdding(false), [node.slug]);
  return (
    <>
      <Panel
        title={node.name}
        action={
          <Link to={`/p/${node.slug}`} className="shrink-0 text-xs text-accent hover:underline">
            Apri scheda
          </Link>
        }
      >
        <div className="flex flex-wrap items-center gap-1.5">
          <HealthBadge health={node.health} />
          <LifecycleBadge lifecycle={node.lifecycle} />
          {node.stack.map((s) => (
            <Chip key={s} mono>
              {s}
            </Chip>
          ))}
        </div>
        {node.last_activity_at && <p className="mt-2 text-xs text-muted">Ultima attività {relativeTime(node.last_activity_at)}</p>}
      </Panel>
      <Panel title="Relazioni">
        <div className="space-y-3">
          <RelationList slug={node.slug} relations={relations} onChange={onChange} onSelect={onSelect} />
          {adding ? (
            <LinkForm slug={node.slug} onAdded={() => { setAdding(false); onChange(); }} />
          ) : (
            <button className="text-xs text-accent hover:underline" onClick={() => setAdding(true)}>
              + Aggiungi relazione
            </button>
          )}
        </div>
      </Panel>
    </>
  );
}

function NodeLabel({ node, ring, angle, faint, radius }: { node: RelationNode; ring: Ring; angle: number; faint: boolean; radius: number }) {
  const cls = faint ? "fill-muted text-[11px]" : "fill-fg text-[13px]";
  if (ring === "focus") {
    return (
      <text y={radius + 22} textAnchor="middle" className="fill-fg text-[15px] font-semibold">
        {node.name}
      </text>
    );
  }
  if (ring === "near" || ring === "far") {
    // Horizontal, on the outer side of the node.
    const right = Math.cos(angle) >= 0;
    return (
      <text x={right ? radius + 6 : -radius - 6} y={4} textAnchor={right ? "start" : "end"} className={cls}>
        {node.name}
      </text>
    );
  }
  // Outer circle: along the radius, flipped on the left half so it never reads upside down.
  let deg = (angle * 180) / Math.PI;
  const flip = Math.cos(angle) < 0;
  if (flip) deg += 180;
  return (
    <text transform={`rotate(${deg})`} x={flip ? -radius - 5 : radius + 5} y={4} textAnchor={flip ? "end" : "start"} className={cls}>
      {node.name.length > LABEL_MAX ? `${node.name.slice(0, LABEL_MAX - 1)}…` : node.name}
    </text>
  );
}

function Legend() {
  const items = [...LINK_KINDS, ...OBSERVED_KINDS];
  return (
    <div className="flex flex-wrap justify-center gap-x-4 gap-y-1.5 border-t border-line px-2 pt-2 pb-1 text-xs text-muted">
      {items.map((k) => (
        <span key={k} className="inline-flex items-center gap-1.5">
          <KindSwatch kind={k} />
          {LINK_LABEL[k]}
        </span>
      ))}
      <span className="inline-flex items-center gap-1.5">
        <svg viewBox="0 0 10 10" className="size-2.5" aria-hidden>
          <circle cx="5" cy="5" r="4" fill="none" stroke="var(--muted)" strokeWidth="1.5" />
        </svg>
        in pausa / archiviato
      </span>
    </div>
  );
}

function Toggle({ on, onClick, children }: { on: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button onClick={onClick} aria-pressed={on} className={`rounded-md px-3 py-1.5 text-sm ${on ? "bg-surface-2 text-fg" : "text-muted hover:text-fg"}`}>
      {children}
    </button>
  );
}

// ------------------------------------------------------------------ layout

function filterGraph(data: RelationGraph | null, observed: boolean, archived: boolean, focus: string | null) {
  if (!data) return { nodes: [], edges: [] };
  // The focused project stays visible even when its kind of project is filtered out.
  const nodes = data.nodes.filter((n) => archived || n.lifecycle !== "archived" || n.slug === focus);
  const present = new Set(nodes.map((n) => n.slug));
  const edges = data.relations.filter((e) => (observed || e.origin === "declared") && present.has(e.source) && present.has(e.target));
  return { nodes, edges };
}

function adjacency(edges: Relation[]) {
  const adj = new Map<string, Set<string>>();
  const link = (a: string, b: string) => (adj.get(a) ?? adj.set(a, new Set()).get(a)!).add(b);
  for (const e of edges) {
    link(e.source, e.target);
    link(e.target, e.source);
  }
  return adj;
}

const byName = (a: RelationNode, b: RelationNode) => a.name.localeCompare(b.name);

function onCircle(node: RelationNode, ring: Ring, r: number, angle: number): Placed {
  return { node, ring, angle, x: r * Math.cos(angle), y: r * Math.sin(angle) };
}

/** Every project on one circle; connected groups are kept together, largest
 * first, then the projects with no relation in alphabetical order. */
function overviewLayout(nodes: RelationNode[], edges: Relation[]): Placed[] {
  const adj = adjacency(edges);
  const bySlug = new Map(nodes.map((n) => [n.slug, n]));
  const seen = new Set<string>();
  const groups: RelationNode[][] = [];
  for (const n of [...nodes].sort(byName)) {
    if (seen.has(n.slug) || !adj.has(n.slug)) continue;
    const group: RelationNode[] = [];
    const queue = [n.slug];
    seen.add(n.slug);
    while (queue.length) {
      const s = queue.shift()!;
      group.push(bySlug.get(s)!);
      for (const t of [...(adj.get(s) ?? [])].sort()) {
        if (!seen.has(t)) {
          seen.add(t);
          queue.push(t);
        }
      }
    }
    groups.push(group);
  }
  groups.sort((a, b) => b.length - a.length);
  const order = [...groups.flat(), ...nodes.filter((n) => !seen.has(n.slug)).sort(byName)];
  const step = (2 * Math.PI) / Math.max(order.length, 1);
  return order.map((n, i) => onCircle(n, "all", R_ALL, -Math.PI / 2 + i * step));
}

/** The focused project in the middle, its relations on the first ring, their
 * relations on the second (near the project that leads to them), the rest outside. */
function focusLayout(nodes: RelationNode[], edges: Relation[], focus: string): Placed[] {
  const adj = adjacency(edges);
  const bySlug = new Map(nodes.map((n) => [n.slug, n]));
  const near = [...(adj.get(focus) ?? [])].map((s) => bySlug.get(s)!).sort(byName);
  const nearAngle = new Map<string, number>();
  const placed: Placed[] = [{ node: bySlug.get(focus)!, ring: "focus", angle: 0, x: 0, y: 0 }];

  near.forEach((n, i) => {
    const a = -Math.PI / 2 + (i * 2 * Math.PI) / near.length;
    nearAngle.set(n.slug, a);
    placed.push(onCircle(n, "near", R_NEAR, a));
  });

  // Second ring: sorted by the mean direction of the first-ring projects they hang off.
  const far = nodes
    .filter((n) => n.slug !== focus && !nearAngle.has(n.slug) && [...(adj.get(n.slug) ?? [])].some((s) => nearAngle.has(s)))
    .map((n) => {
      const parents = [...adj.get(n.slug)!].filter((s) => nearAngle.has(s)).map((s) => nearAngle.get(s)!);
      const want = Math.atan2(parents.reduce((t, a) => t + Math.sin(a), 0), parents.reduce((t, a) => t + Math.cos(a), 0));
      return { n, want };
    })
    .sort((a, b) => a.want - b.want);
  spread(far.map((f) => f.want), (2 * Math.PI) / 28).forEach((a, i) => placed.push(onCircle(far[i].n, "far", R_FAR, a)));

  const shown = new Set(placed.map((p) => p.node.slug));
  const rest = nodes.filter((n) => !shown.has(n.slug)).sort(byName);
  rest.forEach((n, i) => placed.push(onCircle(n, "rest", R_REST, -Math.PI / 2 + (i * 2 * Math.PI) / rest.length)));
  return placed;
}

/** Push sorted angles apart so neighbours are at least `gap` radians away,
 * keeping each as close as possible to where it wants to be. */
function spread(wanted: number[], gap: number): number[] {
  if (wanted.length * gap >= 2 * Math.PI) return wanted.map((_, i) => -Math.PI / 2 + (i * 2 * Math.PI) / wanted.length);
  const out = [...wanted];
  for (let pass = 0; pass < 20; pass++) {
    for (let i = 1; i < out.length; i++) if (out[i] - out[i - 1] < gap) out[i] = out[i - 1] + gap;
    for (let i = out.length - 2; i >= 0; i--) if (out[i + 1] - out[i] < gap) out[i] = out[i + 1] - gap;
  }
  return out;
}

function nodeRadius(slug: string, focus: string | null) {
  return slug === focus ? 12 : 7;
}

function edgeKey(e: Relation) {
  return `${e.origin}-${e.id ?? ""}-${e.kind}-${e.source}-${e.target}`;
}

/** A curve bowed toward the centre (a straight line when one end is the centre),
 * trimmed so it starts and ends at the edge of each node. */
function edgePath(a: Point, b: Point, ra: number, rb: number) {
  const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
  const atCentre = Math.hypot(a.x, a.y) < 1 || Math.hypot(b.x, b.y) < 1;
  const c = atCentre ? mid : { x: mid.x * 0.35, y: mid.y * 0.35 };
  const trim = (from: Point, toward: Point, r: number) => {
    const d = Math.hypot(toward.x - from.x, toward.y - from.y) || 1;
    return { x: from.x + ((toward.x - from.x) * r) / d, y: from.y + ((toward.y - from.y) * r) / d };
  };
  const s = trim(a, c, ra + 2);
  const t = trim(b, c, rb + 3);
  return `M${s.x.toFixed(1)},${s.y.toFixed(1)} Q${c.x.toFixed(1)},${c.y.toFixed(1)} ${t.x.toFixed(1)},${t.y.toFixed(1)}`;
}

/** Positions that glide from the previous layout to the new one. */
function useTweened(layout: Placed[]): Record<string, Point> {
  const target = useMemo(() => Object.fromEntries(layout.map((p) => [p.node.slug, { x: p.x, y: p.y }])), [layout]);
  const [current, setCurrent] = useState(target);
  const currentRef = useRef(current);
  currentRef.current = current;

  useEffect(() => {
    const from = currentRef.current;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce || Object.keys(from).length === 0) {
      setCurrent(target);
      return;
    }
    const start = performance.now();
    const duration = 450;
    let frame = requestAnimationFrame(function tick(now) {
      const t = Math.min((now - start) / duration, 1);
      const k = 1 - Math.pow(1 - t, 3);
      const next: Record<string, Point> = {};
      for (const [slug, to] of Object.entries(target)) {
        const f = from[slug] ?? { x: 0, y: 0 };
        next[slug] = { x: f.x + (to.x - f.x) * k, y: f.y + (to.y - f.y) * k };
      }
      setCurrent(next);
      if (t < 1) frame = requestAnimationFrame(tick);
    });
    return () => cancelAnimationFrame(frame);
  }, [target]);

  return current;
}
