"""Record a trace of every bundled scenario and build the interactive demo page.

    python scripts/export_traces.py

Writes docs/demo-data.json and docs/index.html (from docs/demo.template.html).
docs/index.html is a single self-contained file you can open locally or serve
with GitHub Pages.
"""

import json
from pathlib import Path

from kubesleuth.agent import Investigation, approve_all
from kubesleuth.brains.rules import RuleBrain
from kubesleuth.cluster.simulated import SimulatedCluster, list_scenarios, load_scenario

DOCS = Path(__file__).resolve().parent.parent / "docs"
HEAD = (
    '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
)


def record() -> list[dict]:
    data = []
    for s in list_scenarios():
        scenario = load_scenario(s["id"])
        inv = Investigation(SimulatedCluster(s["id"]), RuleBrain(), approver=approve_all)
        inv.run()
        data.append({
            "id": s["id"],
            "title": s["title"],
            "ground_truth": scenario["ground_truth"],
            "pods": [{"name": p["name"], "owner": p.get("owner"), "phase": p["phase"], "ready": p["ready"],
                      "restarts": p.get("restarts", 0), "node": p.get("node")} for p in scenario["pods"]],
            "events": [e.to_dict() for e in inv.events],
        })
    return data


def main() -> None:
    data = record()
    payload = json.dumps(data, indent=1).replace("</", "<\\/")
    (DOCS / "demo-data.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    template = (DOCS / "demo.template.html").read_text(encoding="utf-8")
    page = template.replace("__DATA__", payload)
    (DOCS / "index.html").write_text(HEAD + page + "\n</html>\n", encoding="utf-8")
    print(f"recorded {len(data)} scenarios -> docs/demo-data.json, docs/index.html")


if __name__ == "__main__":
    main()
