"""Shared Groq client configuration -- the ONE place the model id and retry
constants live, imported by every module that calls Groq
(reasoning/analyze_news.py, reasoning/causal_reasoning.py,
reasoning/daily_summary.py, graph/generate_relations.py) so a model
deprecation is a single-line fix instead of a grep-and-replace across four
files -- exactly what was missing when Groq deprecated
llama-3.3-70b-versatile and every one of those four call sites started
failing independently and silently.

Standalone module -- imports nothing from elsewhere in this project --
specifically so it can be imported from either reasoning/ or graph/ without
risking a circular import (graph/generate_relations.py already imports
from reasoning.daily_summary, while reasoning/daily_summary.py and
reasoning/causal_reasoning.py import from graph.build_graph).
"""

# Groq deprecated llama-3.3-70b-versatile on 2026-08-16 (see
# https://console.groq.com/docs/deprecations); openai/gpt-oss-120b is
# Groq's own recommended replacement (alongside qwen/qwen3.6-27b) --
# confirmed still listed and active via a live
# GET https://api.groq.com/openai/v1/models call, and confirmed working
# with a real chat.completions.create() smoke test using this project's
# exact prompt shape (JSON mode, temperature=0) on 2026-08-24.
GROQ_MODEL = "openai/gpt-oss-120b"

MAX_RETRIES = 5      # for 429 rate-limit backoff
BACKOFF_BASE = 2.0   # seconds: 2, 4, 8, 16, 32

# Anti-backlog-runaway guard for the per-run Groq analysis loops
# (analyze_news.py, causal_reasoning.py, generate_relations.py,
# daily_summary.py). None of their existing stop conditions -- a
# success-based daily quota counter (bump_usage/DAILY_CALL_LIMIT), or a
# Groq-reported daily-token-limit signal (_is_daily_token_limit) -- ever
# fire when EVERY call fails for a reason unrelated to quota, e.g. a
# deprecated/renamed model returning 404 on every single request, exactly
# what happened here: a 51,443-item news backlog was attempted call-by-call
# for nearly two hours during a real pipeline run, because bump_usage()
# only increments on a SUCCESSFUL analysis, so the "quota reached" check
# never tripped. These two caps are a hard backstop independent of *why*
# calls are failing -- checked and enforced by each loop itself (there is
# no single shared loop to enforce them in centrally).
MAX_ATTEMPTS_PER_RUN = 500        # hard ceiling on API calls attempted, success or failure
MAX_CONSECUTIVE_FAILURES = 10     # abort fast once it looks like EVERY call is failing
