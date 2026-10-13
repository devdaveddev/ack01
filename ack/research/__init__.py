"""Experiments for the three research questions. Run with `python -m ack research`.

  judges.py      Do two LLMs beat one, and does either beat rules, on adopt/adapt/reject?
  tiers.py       How small can the shared packet get before answers get worse?
  provenance.py  Does the provenance trail catch bad or stale transfers?

Everything runs offline. Pass --llm to add Claude-backed judges and answerers.
"""
