"""Test package bootstrap: silence the LLM fallback chatter.

Every robustness test deliberately uses a dead or garbage LLM, so
prodigal.llm correctly logs a WARNING per call (hundreds per run).
That noise hides the real result. Fallback *behavior* is still asserted
in the tests; only the log volume is muted here.
"""
import logging

logging.getLogger("prodigal.llm").setLevel(logging.CRITICAL)
