"""Evaluator-only code: the fixture generator, oracles and grading (E12, spec §14.4).

This package may import anything. No other package may import it: the boundary
test fails `make check` if one does, because worker code that can reach oracle
labels could leak a case's answer into the investigation.
"""
