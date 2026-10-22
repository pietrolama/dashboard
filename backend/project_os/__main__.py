"""Command line entry point: `python -m project_os <command>`."""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

from .config import Settings
from .db import Database
from .errors import NotFound
from .registry import Conflict, Registry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="project-os", description="Personal project registry")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="start the local dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--open", action="store_true", help="open the browser")

    sub.add_parser("mcp", help="run MCP server over stdio for AI agents")

    add = sub.add_parser("add-root", help="register a directory to scan")
    add.add_argument("path")
    add.add_argument("--max-depth", type=int, default=3)

    sub.add_parser("roots", help="list registered roots")
    sub.add_parser("scan", help="scan all roots now")
    sub.add_parser("list", help="list projects")

    ign = sub.add_parser("ignore", help="skip a path glob when scanning (e.g. vendored deps)")
    ign.add_argument("pattern", help="glob relative to a root, e.g. imageGen/stable-diffusion.cpp")
    sub.add_parser("ignores", help="list ignore patterns")
    unign = sub.add_parser("unignore", help="remove an ignore pattern")
    unign.add_argument("pattern")

    def with_source(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--source", default="cli", help="who is writing: cli, agent, … (shown in the history)")
        return p

    decide = with_source(sub.add_parser("decide", help="record a decision or constraint"))
    decide.add_argument("project", help="project slug")
    decide.add_argument("title")
    decide.add_argument("--why", dest="rationale", help="rationale")
    decide.add_argument("--alternatives", help="options considered and discarded")
    decide.add_argument("--constraint", action="store_true", help="record as a constraint")
    decide.add_argument("--supersedes", type=int, metavar="ID", help="decision this one replaces")

    decisions = sub.add_parser("decisions", help="list a project's decisions")
    decisions.add_argument("project")
    decisions.add_argument("--all", action="store_true", help="include superseded decisions")

    sstart = with_source(sub.add_parser("session-start", help="start a work session"))
    sstart.add_argument("project")
    sstart.add_argument("goal")

    for name, help_ in (("session-end", "end the session in progress"),
                        ("session-log", "record a finished session in one step")):
        p = with_source(sub.add_parser(name, help=help_))
        p.add_argument("project")
        if name == "session-log":
            p.add_argument("goal")
        p.add_argument("--outcome")
        p.add_argument("--question", action="append", dest="questions", default=[], help="open question (repeatable)")
        p.add_argument("--next", dest="next_step", help="next concrete step")

    mem = sub.add_parser("memory", help="show what to pick a project back up with")
    mem.add_argument("project")

    resume = with_source(sub.add_parser("resume", help="generate the Riprendi progetto brief (PRP 1.0)"))
    resume.add_argument("project")
    resume.add_argument("--format", choices=("markdown", "json", "bundle"), default="bundle",
                        help="markdown/json print to stdout; bundle writes RESUME.md, manifest.json, evidence.json")
    resume.add_argument("--mode", choices=("implementation_within_scope", "analysis_only"),
                        default="implementation_within_scope")

    check = sub.add_parser("resume-check", help="verify a bundle and report what changed since")
    check.add_argument("bundle", help="bundle directory, e.g. …/exports/<slug>/latest")

    args = parser.parse_args(argv)
    settings = Settings.from_env()

    if args.command == "resume-check":
        return _resume_check(Path(args.bundle))

    if args.command == "serve":
        import uvicorn

        from .api import create_app

        if args.open:
            webbrowser.open(f"http://{args.host}:{args.port}")
        uvicorn.run(create_app(settings), host=args.host, port=args.port)
        return 0

    if args.command == "mcp":
        from .mcp_server import run_server
        import asyncio

        asyncio.run(run_server())
        return 0

    registry = Registry(Database(settings.db_path))
    try:
        if args.command == "add-root":
            print(json.dumps(registry.add_root(args.path, args.max_depth), indent=2))
        elif args.command == "roots":
            for r in registry.list_roots():
                state = "on " if r["enabled"] else "off"
                print(f"[{state}] {r['path']}  (depth {r['max_depth']}, {r['project_count']} projects)")
        elif args.command == "scan":
            run = registry.scan()
            print(json.dumps({k: run[k] for k in ("status", "stats", "errors")}, indent=2))
        elif args.command == "list":
            for p in registry.list_projects():
                activity = (p["last_activity_at"] or "—")[:10]
                print(f"{p['health']:<8} {p['lifecycle']:<8} {activity}  {p['slug']}")
        elif args.command == "ignore":
            res = registry.add_ignore(args.pattern)
            dropped = ", ".join(res["dropped"]) or "none"
            print(f"Ignoring {res['pattern']}  (dropped: {dropped})")
        elif args.command == "ignores":
            for pat in registry.list_ignores():
                print(pat)
        elif args.command == "unignore":
            registry.remove_ignore(args.pattern)
            print(f"Removed {args.pattern}")
        elif args.command == "resume":
            return _resume(args, registry, settings)
        else:
            return _memory_command(args, registry)
    except (Conflict, NotFound) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _memory_command(args: argparse.Namespace, registry: Registry) -> int:
    memory = registry.memory
    project_id = registry.resolve(args.project)

    if args.command == "decide":
        d = memory.record_decision(
            project_id, args.title, args.rationale, args.alternatives,
            kind="constraint" if args.constraint else "decision",
            supersedes_id=args.supersedes, source=args.source,
        )
        print(f"recorded {d['kind']} #{d['id']}: {d['title']}")
    elif args.command == "decisions":
        for d in memory.list_decisions(project_id, include_superseded=args.all):
            print(f"#{d['id']:<4} {d['kind']:<10} {d['status']:<10} {d['title']}")
    elif args.command == "session-start":
        s = memory.start_session(project_id, args.goal, source=args.source)
        print(f"session #{s['id']} started: {s['goal']}")
    elif args.command == "session-end":
        open_session = memory.snapshot(project_id)["open_session"]
        if not open_session:
            raise Conflict("No session in progress for this project")
        s = memory.end_session(open_session["id"], args.outcome, args.questions, args.next_step, source=args.source)
        print(f"session #{s['id']} ended")
    elif args.command == "session-log":
        s = memory.log_session(project_id, args.goal, args.outcome, args.questions, args.next_step, source=args.source)
        print(f"session #{s['id']} recorded")
    elif args.command == "memory":
        snap = memory.snapshot(project_id)
        if snap["open_session"]:
            print(f"In progress: {snap['open_session']['goal']}")
        print(f"Next step: {snap['next_step'] or '—'}")
        for q in snap["open_questions"]:
            print(f"  ? {q}")
        for d in memory.list_decisions(project_id, include_superseded=False):
            marker = "!" if d["status"] == "review" else "-"
            print(f"  {marker} [{d['kind']}] {d['title']}")
    return 0


def _resume(args: argparse.Namespace, registry: Registry, settings: Settings) -> int:
    from . import resume

    project_id = registry.resolve(args.project)
    if args.format == "bundle":
        brief, path = resume.export(registry, project_id, settings.exports_dir, args.mode, source=args.source)
        print(path)
        print(f"freshness: {brief.manifest['resume']['freshness']}", file=sys.stderr)
        return 0
    brief = resume.build_brief(registry, project_id, args.mode)
    if brief.errors:
        print("brief failed validation:\n  " + "\n  ".join(brief.errors), file=sys.stderr)
        return 1
    if args.format == "markdown":
        print(brief.markdown)
    else:
        print(json.dumps(brief.manifest, indent=2, ensure_ascii=False))
    return 0


def _resume_check(bundle: Path) -> int:
    """Exit 0 when intact and current, 1 when corrupt or invalid, 2 when the project moved on."""
    from . import resume

    report = resume.verify_bundle(bundle)
    for problem in report["integrity"]:
        print(f"INTEGRITY  {problem}")
    for change in report["divergence"]:
        print(f"DIVERGED   {change}")
    if not report["valid"]:
        return 1
    if report["divergence"]:
        return 2
    print("OK: bundle intact and the project matches it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
