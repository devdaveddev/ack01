"""Command line.

  python -m ack demo                         run the two-agent story locally
  python -m ack research [--llm] [--out F]   run the three research experiments
  python -m ack web --demo                   try the web app: a broker, beta and alpha in two tabs,
                                             gamma and delta signing in the background
  python -m ack context --goal "..." --github URL --site URL [--out DIR]
                                             build a context pack from a repo and/or website
  python -m ack context-check PACK.json       has anything in a saved pack changed upstream?
  python -m ack web --root DIR --name NAME --broker URL [--port 8800] [--open]
  python -m ack broker [--port 7468] [--http-port 8765] [--data DIR]
                                             Ack wire on --port (ack://), plus HTTP if --http-port is given

  Agent commands work on a project folder (state in <root>/.ack):
  python -m ack agent --root DIR --name NAME --broker URL register
  ... share ID --base FOLDER --files a.py b.py --topic "..." --summary "..." --allow peer [--tests "python -m unittest"]
  ... invite                                 prints a QR code / ack:// URI
  ... join URI
  ... ask PEER "question"
  ... request PEER SOURCE_ID "reason" "what we'll do with it"
  ... update SOURCE_ID                       tell peers holding a changed part (UPDATE_AVAILABLE + diff)
  ... poll [--approve]                       handle incoming messages; --approve asks before sandbox runs and installs
  ... audit                                  check what we hold against our logs, the registry and our cache

  Announcements (one publisher, many signatories, a quorum):
  ... subscribe PUBLISHER [--off]
  ... announce "title" --files a.py b.py [--encrypted] [--quorum K] [--to alpha gamma]
  ... sign ANNOUNCEMENT_ID ACK|NAK ["reason"]
  ... policy auto|manual                     auto signs ACK when the signature and digest check out
  ... announcements                          what you published and what you were asked to sign
"""

import argparse
import shlex
from pathlib import Path


def _confirm(action: str, detail: str) -> bool:
    return input(f"approve {action}: {detail}? [y/N] ").strip().lower() == "y"


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="ack")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo")
    rs = sub.add_parser("research")
    rs.add_argument("--llm", action="store_true", help="add Claude-backed judges and answerers (needs credentials)")
    rs.add_argument("--models", nargs=2, default=["claude-opus-5-5", "claude-sonnet-5-5"],
                    help="the two models for the panel")
    rs.add_argument("--out", default="research-results.json")
    w = sub.add_parser("web", help="open the browser interface for one agent")
    w.add_argument("--root", help="your project folder")
    w.add_argument("--name", help="your agent name")
    w.add_argument("--broker", default="ack://127.0.0.1:7468")
    w.add_argument("--port", type=int, default=8800)
    w.add_argument("--open", action="store_true", help="open it in your browser")
    w.add_argument("--demo", action="store_true", help="local broker plus two demo agents")
    w.add_argument("--no-pair", action="store_true", help="with --demo: leave pairing to you")
    c = sub.add_parser("context", help="build a context pack from GitHub repos and websites")
    c.add_argument("--goal", required=True, help="what you're trying to build")
    c.add_argument("--github", nargs="*", default=[], metavar="URL", help="repository URLs")
    c.add_argument("--site", nargs="*", default=[], metavar="URL", help="website start pages")
    c.add_argument("--only", nargs="*", default=[], metavar="DIR", help="only look in these repo folders")
    c.add_argument("--pages", type=int, default=10, help="most pages to read per website")
    c.add_argument("--budget", type=int, default=12000, help="about how many tokens the pack may use")
    c.add_argument("--name", default="", help="pack name (default: from the goal)")
    c.add_argument("--project", default="", help="your project, as the prompt should name it")
    c.add_argument("--out", default="context-packs", help="folder to save the .md and .json in")
    c.add_argument("--print", action="store_true", help="also print the pack")
    cc = sub.add_parser("context-check", help="check a saved pack for upstream changes")
    cc.add_argument("pack", help="the pack's .json file")
    b = sub.add_parser("broker")
    b.add_argument("--host", default="127.0.0.1")
    b.add_argument("--port", type=int, default=7468, help="Ack wire port")
    b.add_argument("--http-port", type=int, default=None, help="also serve HTTP on this port")
    b.add_argument("--data", default="broker-data")

    a = sub.add_parser("agent")
    a.add_argument("--root", required=True)
    a.add_argument("--name", required=True)
    a.add_argument("--broker", default="ack://127.0.0.1:7468")
    acts = a.add_subparsers(dest="action", required=True)
    sb = acts.add_parser("subscribe")
    sb.add_argument("publisher")
    sb.add_argument("--off", action="store_true")
    an = acts.add_parser("announce")
    an.add_argument("title")
    an.add_argument("--files", nargs="+", required=True)
    an.add_argument("--encrypted", action="store_true")
    an.add_argument("--quorum", type=int)
    an.add_argument("--to", nargs="+")
    sg = acts.add_parser("sign")
    sg.add_argument("id")
    sg.add_argument("verdict", choices=["ACK", "NAK"])
    sg.add_argument("reason", nargs="?", default="")
    po = acts.add_parser("policy")
    po.add_argument("policy", choices=["auto", "manual"])
    acts.add_parser("announcements")
    acts.add_parser("register")
    s = acts.add_parser("share")
    s.add_argument("id")
    s.add_argument("--base", default="")
    s.add_argument("--files", nargs="+", required=True)
    s.add_argument("--topic", required=True)
    s.add_argument("--summary", required=True)
    s.add_argument("--tags", nargs="*", default=[])
    s.add_argument("--allow", nargs="*", default=[])
    s.add_argument("--tests", default="python -m unittest discover -s . -p test_*.py")
    acts.add_parser("invite")
    j = acts.add_parser("join")
    j.add_argument("uri")
    q = acts.add_parser("ask")
    q.add_argument("peer")
    q.add_argument("text")
    q.add_argument("--exact", action="store_true", help="send exactly as typed, without spelling fixes")
    r = acts.add_parser("request")
    r.add_argument("peer")
    r.add_argument("source_id")
    r.add_argument("reason")
    r.add_argument("intent", help="what you plan to do with the part")
    u = acts.add_parser("update")
    u.add_argument("source_id")
    pl = acts.add_parser("poll")
    pl.add_argument("--approve", action="store_true", help="ask before sandbox runs and installs")
    acts.add_parser("audit")

    args = p.parse_args(argv)
    if args.cmd == "demo":
        from .demo import run
        run()
    elif args.cmd == "research":
        from .research.run import run
        run(llm=args.llm, models=tuple(args.models), out=Path(args.out))
    elif args.cmd == "web":
        from . import web
        if args.demo:
            servers = web.demo(args.port, args.open, pair=not args.no_pair)
        else:
            if not args.root or not args.name:
                p.error("web needs --root and --name (or --demo)")
            servers = [web.serve(web.WebApp(args.name, Path(args.root), args.broker), args.port, args.open)]
        web.run_forever(servers)
    elif args.cmd == "context":
        _context(args)
    elif args.cmd == "context-check":
        import json
        from .context import check
        for row in check(json.loads(Path(args.pack).read_text())):
            print(f"{row['status']:30} {row['source']}  {row['path']}")
    elif args.cmd == "broker":
        import threading
        from .broker import BrokerCore, BrokerState, make_server, make_wire_server
        core = BrokerCore(BrokerState(Path(args.data)))  # one state, whichever transport a request arrives on
        if args.http_port is not None:
            http = make_server(Path(args.data), args.host, args.http_port, core=core)
            threading.Thread(target=http.serve_forever, daemon=True).start()
            print(f"broker HTTP on http://{args.host}:{args.http_port}")
        server = make_wire_server(Path(args.data), args.host, args.port, core=core)
        print(f"broker listening on ack://{args.host}:{args.port}, data in {args.data}")
        server.serve_forever()
    else:
        _agent(args)


def _context(args) -> None:
    from .context import ContextError, gather, render, save, slug
    if not args.github and not args.site:
        raise SystemExit("give at least one --github or --site")
    try:
        items, errors = gather(args.goal, args.github, args.site, only=args.only, max_pages=args.pages,
                               budget=args.budget)
    except ContextError as e:
        raise SystemExit(f"error: {e}")
    for err in errors:
        print(f"skipped: {err}")
    flagged = [it for it in items if it.flagged]
    pack = render(args.name or slug(args.goal), args.goal, items, args.project or str(Path.cwd()))
    path = save(pack, Path(args.out))
    print(f"{len(pack.items)} excerpt(s), about {sum(i.tokens for i in pack.items):,} tokens, "
          f"from {len({i.url for i in pack.items})} file(s)/page(s)")
    if flagged:
        print(f"left out {len(flagged)} flagged excerpt(s) that address an AI model, e.g. {flagged[0].path}")
    print(f"saved {path} (and {path.with_suffix('.json').name})")
    if args.print:
        print()
        print(pack.markdown)


def _agent(args) -> None:
    from .agent import Agent, deny_all
    from .memory import Entry
    from .pairing import Invite, render_qr

    approver = _confirm if getattr(args, "approve", False) else deny_all
    agent = Agent(args.name, Path(args.root), args.broker, approver=approver)
    if args.action == "register":
        agent.register()
        print(f"registered {args.name}")
    elif args.action == "share":
        agent.share(Entry(id=args.id, base=args.base, files=args.files, topic=args.topic,
                          summary=args.summary, tags=args.tags, allow=args.allow,
                          tests=shlex.split(args.tests)))
        print(f"sharing {args.id}")
    elif args.action == "invite":
        print(render_qr(agent.invite().to_uri()))
    elif args.action == "join":
        agent.join(Invite.from_uri(args.uri))
    elif args.action == "ask":
        cached = agent.ask(args.peer, args.text, exact=args.exact)
        if agent.last_correction:
            print("spelling fixed: " + ", ".join(f"{a} -> {b}" for a, b in agent.last_correction))
        print(cached if cached else "sent; run poll to read the answer")
    elif args.action == "request":
        agent.request(args.peer, args.source_id, args.reason, args.intent)
        print("sent; run poll to continue")
    elif args.action == "update":
        print(f"notified {agent.publish_update(args.source_id)} peer(s)")
    elif args.action == "poll":
        print(f"handled {agent.poll()} message(s)")
    elif args.action == "subscribe":
        agent.subscribe(args.publisher, on=not args.off)
    elif args.action == "announce":
        files = {f: (Path(args.root) / f).read_text() for f in args.files}
        rec = agent.announce(args.title, files, encrypted=args.encrypted, subscribers=args.to, quorum=args.quorum)
        print(f"announced {rec['statement']['id']}: waiting on {', '.join(rec['status']['waiting'])}")
    elif args.action == "sign":
        agent.poll()
        st = agent.vote(args.id, args.verdict, args.reason)
        print(f"{st['state']}: {st['acks']} of {st['of']} acknowledged, quorum {st['quorum']}")
    elif args.action == "policy":
        agent.set_sign_policy(args.policy)
    elif args.action == "announcements":
        agent.poll()
        agent.refresh_announcements()
        for r in agent.state["published"].values():
            st, stmt = r["status"], r["statement"]
            print(f"out {stmt['id']}  {st['state']:7} {st['acks']}/{st['of']} q{st['quorum']}  {stmt['title']}")
        for r in agent.state["inbox"].values():
            stmt, vote = r["statement"], r["vote"]
            mark = vote["verdict"] if vote else ("check ok" if r["check"]["match"] else "check failed")
            print(f"in  {stmt['id']}  {mark:12} from {stmt['publisher']}  {stmt['title']}")
    elif args.action == "audit":
        from .audit import audit
        findings = audit(agent)
        for f in findings:
            print(f"{f.kind:9} {f.source}: {f.detail}")
        print(f"{len(findings)} finding(s)")


if __name__ == "__main__":
    main()
