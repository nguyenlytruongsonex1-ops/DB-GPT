"""Summarise the live scenario traces into a small table."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "alert_results" / "live_scenarios"

rows = []
for path in sorted(ROOT.glob("*.json")):
    if path.name.endswith("_anomaly.json"):
        continue
    trace = json.loads(path.read_text())
    signal = trace.get("signal", {})
    peak = 0.0
    for sample in signal.get("samples", []):
        for value in sample.get("values") or []:
            peak = max(peak, value)
    rows.append(
        {
            "scenario": trace["scenario"],
            "live": bool(signal.get("observed")),
            "peak": peak,
            "threshold": trace.get("threshold"),
            "cleanup": trace.get("cleanup"),
            "killed": trace.get("terminated_backends"),
            "diagnosis": "ran" if trace.get("diagnosis") else "not run",
        }
    )

order = ["large_read", "bulk_delete", "many_indexes", "concurrent_writes", "slow_join"]
rows.sort(key=lambda r: order.index(r["scenario"]) if r["scenario"] in order else 99)

header = f"{'scenario':<19}{'live':<7}{'peak':<9}{'thr':<9}{'cleanup':<9}{'killed':<8}{'diagnosis'}"
print(header)
for r in rows:
    print(f"{r['scenario']:<19}{str(r['live']):<7}{r['peak']:<9.0f}{r['threshold']:<9.0f}"
          f"{str(r['cleanup']):<9}{str(r['killed']):<8}{r['diagnosis']}")
print(f"total {len(rows)}")
