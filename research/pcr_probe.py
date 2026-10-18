#!/usr/bin/env python3
"""Probe: can the runner read Cboe's daily put/call statistics? Writes
research/results/pcr_probe.txt (structure only, a few dates)."""
import json, os, re
import requests

H = {"User-Agent": "Mozilla/5.0 (research)"}
dates = ["2020-10-15", "2023-06-14", "2026-10-16"]
out = []
for d in dates:
    for url in (f"https://cdn.cboe.com/data/us/options/market_statistics/daily/{d}_daily_options",
                f"https://www.cboe.com/us/options/market_statistics/daily/?dt={d}"):
        try:
            r = requests.get(url, headers=H, timeout=20)
            body = r.text
            out.append(f"== {url} -> {r.status_code} {r.headers.get('content-type')} len={len(body)}")
            if r.status_code == 200:
                if body.lstrip().startswith(("{", "[")):
                    j = r.json()
                    out.append(json.dumps(j)[:1500])
                else:
                    for m in re.finditer(r"(?i)(total|index|equity|spx|vix|etp)[^<>]{0,40}put/call ratio[^0-9]{0,200}([0-9.]+)", body):
                        out.append("HTML: " + m.group(0)[:160])
        except Exception as e:
            out.append(f"== {url} -> ERROR {e}")
os.makedirs("research/results", exist_ok=True)
open("research/results/pcr_probe.txt", "w").write("\n".join(out) + "\n")
print("\n".join(out))
