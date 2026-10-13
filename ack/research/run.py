"""Runs all three experiments, prints tables, writes JSON."""

import json
from pathlib import Path

from ..judge import RuleJudge
from . import judges, provenance, tiers


def _pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def run(llm: bool = False, models: tuple[str, str] = ("claude-opus-5-5", "claude-sonnet-5-5"),
        out: Path | None = None) -> dict:
    results = {}

    print("\nRQ1  Packet size vs answer quality")
    answerer = None
    if llm:
        from ..llm import anthropic_complete
        answerer = tiers.llm_answerer(anthropic_complete(models[0]))
    rq1 = tiers.run(answerer)
    results["rq1_tiers"] = rq1
    print(f"  {'tier':15}{'score':>8}{'bytes':>8}{'score/KB':>10}   ({'LLM answers' if llm else 'fact coverage'})")
    for tier, v in rq1["tiers"].items():
        print(f"  {tier:15}{_pct(v['score']):>8}{v['bytes']:8.0f}{v['score_per_kb']:10.2f}")

    print("\nRQ2  Adopt / adapt / reject")
    candidates = {"rules": RuleJudge(), "static-scan": judges.StaticScanJudge()}
    if llm:
        from ..llm import LLMJudge, PanelJudge, anthropic_complete
        one = LLMJudge(anthropic_complete(models[0]), models[0])
        two = LLMJudge(anthropic_complete(models[1]), models[1])
        candidates |= {models[0]: one, models[1]: two, "panel": PanelJudge([one, two])}
    rq2 = judges.evaluate(candidates)
    results["rq2_judges"] = rq2
    print(f"  {'judge':22}{'accuracy':>10}{'unsafe accept':>15}{'false reject':>14}")
    for name, v in rq2.items():
        print(f"  {name:22}{_pct(v['accuracy']):>10}{_pct(v['unsafe_accept']):>15}{_pct(v['false_reject']):>14}")
        if v.get("backend_errors"):
            print(f"  {'':22}! {len(v['backend_errors'])} backend errors, e.g. {v['backend_errors'][0]}")
    print(f"  ({len(judges.scenarios())} hand-labelled scenarios; see ack/research/judges.py)")

    print("\nRQ3  Provenance catches bad or stale transfers")
    rq3 = provenance.run()
    results["rq3_provenance"] = rq3
    for name, t in rq3["trials"].items():
        mark = "caught" if t["caught"] else "MISSED"
        what = "no findings expected" if not t["expected"] else f"expect {t['expected']}"
        print(f"  {name:24}{what:24}{mark:8} found={t['found']}")
    print(f"  detection {_pct(rq3['detection_rate'])}, false alarm on clean run: {rq3['false_alarm']}")

    if out:
        out.write_text(json.dumps(results, indent=2))
        print(f"\nwrote {out}")
    return results
