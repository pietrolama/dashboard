export type Lifecycle = "active" | "paused" | "archived";
export type Phase = "idea" | "prototype" | "development" | "stabilization" | "release";
export type Health = "healthy" | "stale" | "missing" | "unknown";

export interface GitState {
  branch: string | null;
  head: string | null;
  head_date: string | null;
  head_subject: string | null;
  dirty_files: number;
  untracked_files: number;
  remote_url: string | null;
}

export interface Repository {
  id: number;
  path: string;
  vcs: "git" | "none";
  remote_url: string | null;
  default_branch: string | null;
  last_scanned_at: string | null;
  missing_since: string | null;
}

export type DecisionKind = "decision" | "constraint";
export type DecisionStatus = "active" | "review" | "superseded";

export interface Decision {
  id: number;
  project_id: number;
  kind: DecisionKind;
  title: string;
  rationale: string | null;
  alternatives: string | null;
  status: DecisionStatus;
  supersedes_id: number | null;
  superseded_by_id: number | null;
  created_at: string;
  updated_at: string;
}

export interface WorkSession {
  id: number;
  project_id: number;
  started_at: string;
  ended_at: string | null;
  goal: string;
  outcome: string | null;
  open_questions: string[];
  next_step: string | null;
}

export interface MemorySnapshot {
  open_session: WorkSession | null;
  last_session: WorkSession | null;
  next_step: string | null;
  open_questions: string[];
  decisions_active: number;
  decisions_review: number;
}

export interface Project {
  id: number;
  name: string;
  slug: string;
  description: string | null;
  lifecycle: Lifecycle;
  phase: Phase | null;
  priority: number;
  origin: "discovered" | "manual";
  last_activity_at: string | null;
  last_observed_at: string | null;
  created_at: string;
  updated_at: string;
  health: Health;
  readme_title: string | null;
  readme_summary: string | null;
  stack: string[];
  git: GitState | null;
  repositories: Repository[];
  memory: MemorySnapshot;
}

export interface Evidence {
  id: number;
  source_type: string;
  source_ref: string;
  content_hash: string;
  observed_at: string;
  payload: Record<string, unknown>;
}

export interface ProjectEvent {
  id: number;
  project_id: number | null;
  project_name: string | null;
  project_slug: string | null;
  event_type: string;
  occurred_at: string;
  source: string;
  payload: Record<string, unknown>;
}

export interface ProjectDetail extends Project {
  evidence: Record<string, Evidence>;
  events: ProjectEvent[];
  decisions: Decision[];
  sessions: WorkSession[];
}

export interface ScanRun {
  id: number;
  started_at: string;
  finished_at: string | null;
  status: "running" | "ok" | "partial" | "failed";
  stats: Record<string, number>;
  errors: { root?: string; path?: string; error: string }[];
}

export interface Overview {
  total: number;
  counts: { lifecycle: Partial<Record<Lifecycle, number>>; health: Partial<Record<Health, number>> };
  dirty: number;
  active_this_week: number;
  last_scan: ScanRun | null;
  scanning: boolean;
}

export interface Root {
  id: number;
  path: string;
  max_depth: number;
  enabled: boolean;
  created_at: string;
  project_count: number;
}

export interface SessionNotes {
  outcome?: string | null;
  open_questions?: string[];
  next_step?: string | null;
}

export type ResumeMode = "implementation_within_scope" | "analysis_only";

export interface ResumeExport {
  brief_id: string;
  path: string;
  latest: string;
  markdown: string;
  manifest: {
    project: { root: string | null };
    resume: { freshness: "current" | "partial" | "stale" | "unknown" };
    state: { unknowns: { id: string; text: string }[] };
    constraints: unknown[];
    decisions: { status: string }[];
    open_questions: unknown[];
    next_actions: unknown[];
    evidence: string[];
  };
}

export type TeamAgent = "claude" | "agy";
export type TeamRunStatus =
  | "planning"
  | "plan_ready"
  | "running"
  | "integrating"
  | "conflict"
  | "review"
  | "applied"
  | "discarded"
  | "failed";

export interface TeamTask {
  id: number;
  number: number;
  agent: TeamAgent;
  title: string;
  details: string | null;
  depends_on: number[];
  status: "todo" | "doing" | "done" | "blocked";
  summary: string | null;
}

export interface TeamMember {
  agent: TeamAgent;
  worktree: string;
  branch: string;
  pane: string | null;
  status: "working" | "done";
  summary: string | null;
  // The agent's terminal shows a question (folder trust, a permission) and waits for the user.
  waiting: boolean;
}

export interface TeamMessage {
  id: number;
  sender: TeamAgent | "user";
  recipient: TeamAgent | "user" | "team";
  body: string;
  created_at: string;
}

export interface TeamRunSummary {
  id: number;
  request: string;
  status: TeamRunStatus;
  created_at: string;
  updated_at: string;
  task_count: number;
  done_count: number;
}

export interface TeamRun extends Omit<TeamRunSummary, "task_count" | "done_count"> {
  project_slug: string;
  repo_path: string;
  base_branch: string | null;
  base_commit: string;
  plan_summary: string | null;
  tmux_session: string | null;
  diffstat: string | null;
  review: string | null;
  error: string | null;
  tasks: TeamTask[];
  members: TeamMember[];
  messages: TeamMessage[];
  session_alive: boolean;
  attach_command: string | null;
  integration_worktree: string;
  claude_trusted: boolean | null;
}

export interface TeamTaskDraft {
  ref: number;
  agent: TeamAgent;
  title: string;
  details: string | null;
  depends_on: number[];
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let message = res.statusText;
    try {
      const data = await res.json();
      message = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, message);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

const qs = (params: Record<string, string | undefined>) => {
  const s = new URLSearchParams(Object.entries(params).filter(([, v]) => v) as [string, string][]);
  return s.size ? `?${s}` : "";
};

export const api = {
  overview: () => request<Overview>("GET", "/overview"),
  projects: (f: { q?: string; lifecycle?: string; health?: string } = {}) =>
    request<Project[]>("GET", `/projects${qs(f)}`),
  project: (slug: string) => request<ProjectDetail>("GET", `/projects/${encodeURIComponent(slug)}`),
  createProject: (body: { name: string; description?: string; path?: string; phase?: Phase | null }) =>
    request<ProjectDetail>("POST", "/projects", body),
  updateProject: (slug: string, body: Partial<Pick<Project, "name" | "description" | "lifecycle" | "phase" | "priority">>) =>
    request<ProjectDetail>("PATCH", `/projects/${encodeURIComponent(slug)}`, body),
  deleteProject: (slug: string) => request<void>("DELETE", `/projects/${encodeURIComponent(slug)}`),
  recordDecision: (
    slug: string,
    body: { title: string; rationale?: string | null; alternatives?: string | null; kind: DecisionKind; supersedes_id?: number | null },
  ) => request<Decision>("POST", `/projects/${encodeURIComponent(slug)}/decisions`, body),
  updateDecision: (id: number, body: Partial<Pick<Decision, "title" | "rationale" | "alternatives" | "kind">> & { status?: "active" | "review" }) =>
    request<Decision>("PATCH", `/decisions/${id}`, body),
  startSession: (slug: string, goal: string) =>
    request<WorkSession>("POST", `/projects/${encodeURIComponent(slug)}/sessions`, { goal }),
  logSession: (slug: string, body: SessionNotes & { goal: string }) =>
    request<WorkSession>("POST", `/projects/${encodeURIComponent(slug)}/sessions`, { ...body, ended: true }),
  endSession: (id: number, body: SessionNotes) => request<WorkSession>("POST", `/sessions/${id}/end`, body),
  exportResume: (slug: string, mode: ResumeMode) =>
    request<ResumeExport>("POST", `/projects/${encodeURIComponent(slug)}/resume`, { mode }),
  resumeZipUrl: (slug: string, briefId: string) => `/api/projects/${encodeURIComponent(slug)}/resume/${briefId}.zip`,
  teamRuns: (slug: string) => request<TeamRunSummary[]>("GET", `/projects/${encodeURIComponent(slug)}/team-runs`),
  createTeamRun: (slug: string, req: string) =>
    request<TeamRun>("POST", `/projects/${encodeURIComponent(slug)}/team-runs`, { request: req }),
  teamRun: (id: number) => request<TeamRun>("GET", `/team-runs/${id}`),
  updateTeamPlan: (id: number, tasks: TeamTaskDraft[], summary?: string | null) =>
    request<TeamRun>("PUT", `/team-runs/${id}/plan`, { tasks, summary }),
  replanTeamRun: (id: number, req?: string) => request<TeamRun>("POST", `/team-runs/${id}/replan`, { request: req }),
  teamAction: (id: number, action: "start" | "finish" | "retry" | "apply" | "discard") =>
    request<TeamRun>("POST", `/team-runs/${id}/${action}`),
  openTeamTerminal: (id: number) => request<{ opened: boolean }>("POST", `/team-runs/${id}/terminal`),
  sendTeamMessage: (id: number, to: TeamAgent, text: string) =>
    request<{ delivered: boolean }>("POST", `/team-runs/${id}/messages`, { to, text }),
  events: (limit = 30) => request<ProjectEvent[]>("GET", `/events?limit=${limit}`),
  roots: () => request<Root[]>("GET", "/roots"),
  addRoot: (path: string, max_depth: number) => request<Root>("POST", "/roots", { path, max_depth }),
  updateRoot: (id: number, body: { enabled?: boolean; max_depth?: number }) =>
    request<Root>("PATCH", `/roots/${id}`, body),
  removeRoot: (id: number) => request<void>("DELETE", `/roots/${id}`),
  scan: () => request<ScanRun>("POST", "/scans"),
  scans: () => request<ScanRun[]>("GET", "/scans"),
};
