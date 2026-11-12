# Oracles

One `Oracle` per snapshot (`src/incidentpilot/evaluation/oracle.py`), holding the answer a correct investigation should reach. Only `incidentpilot.evaluation` reads these files, and workers never mount this folder (erratum E12).
