#!/usr/bin/env python3
"""model-price-radar — watch live LLM prices and find the cheapest route.

Queries the live model catalogues of the providers you configure, reads their
real per-token prices, and prints a short digest:

  * your focus models (e.g. every DeepSeek route), cheapest first
  * the cheapest text models overall
  * models that appeared since the last run

Stdlib only. No LLM in the data path. Deterministic — the same catalogues give
the same report, so the numbers can never be "summarised" into something wrong.

Usage:
    python radar.py                      # uses radar.json beside this file
    python radar.py --config other.json
    python radar.py --json               # machine-readable
    python radar.py --config my.json --state .my_seen.json

Configuration: copy radar.example.json to radar.json and edit. See README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

USER_AGENT = "model-price-radar"

DEFAULTS = {
    "providers": [],
    "focus": [],
    "exclude_prefixes": [],
    "new_model_ceiling": {"input": 0.15, "output": 0.60},
    "top_cheap": 8,
    "max_new": 8,
    "current_model": "",
    "current_provider": "",
    "state_file": ".radar_seen.json",
    "env_file": "",
}


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def hermes_home() -> str:
    """Best-effort Hermes home, matching hermes_constants.get_hermes_home()."""
    explicit = os.environ.get("HERMES_HOME")
    if explicit:
        return explicit
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "hermes")
    return os.path.expanduser("~/.hermes")


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict):
        raise SystemExit(f"{path}: top level must be a JSON object")
    merged = dict(DEFAULTS)
    merged.update(cfg)
    if not merged["providers"]:
        raise SystemExit(f"{path}: 'providers' is empty — nothing to scan")
    return merged


def load_env_file(path: str) -> dict:
    """KEY=VALUE lines from a .env file. Missing file is not an error."""
    out = {}
    if not path or not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def resolve_auth(auth: dict, env_file_values: dict) -> str:
    """Return the bearer token for a provider, or '' when the kind is 'none'."""
    kind = (auth or {}).get("kind", "none")
    if kind == "none":
        return ""
    if kind == "env":
        var = auth.get("var") or ""
        return os.environ.get(var) or env_file_values.get(var) or ""
    if kind == "hermes_auth":
        provider = auth.get("provider") or ""
        path = os.path.join(hermes_home(), "auth.json")
        try:
            with open(path, encoding="utf-8") as fh:
                state = json.load(fh)
            return ((state.get("providers") or {}).get(provider) or {}).get("access_token") or ""
        except (OSError, ValueError):
            return ""
    raise SystemExit(f"unknown auth kind: {kind!r} (use env, hermes_auth or none)")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def get_json(url: str, token: str, timeout: int = 45) -> dict:
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Provider dialects
#
# A dialect knows how ONE provider's /models response is shaped. Adding a
# provider means adding a function here and naming it in the config.
# Each returns records: {"id", "provider", "in", "out", "cache_read", "ctx"}
# where the prices are already normalised to dollars per MILLION tokens.
# --------------------------------------------------------------------------

def fetch_nous(p: dict, token: str) -> list:
    """OpenAI-style listing: pricing per model, dollars-per-TOKEN as strings."""
    data = get_json(p["base_url"].rstrip("/") + "/models", token)
    out = []
    for m in data.get("data") or []:
        mid = m.get("id") or ""
        if not mid or mid.endswith(":batch"):
            continue
        arch = m.get("architecture") or {}
        mods_in = arch.get("input_modalities")
        mods_out = arch.get("output_modalities")
        if mods_in and "text" not in mods_in:
            continue
        if mods_out and "text" not in mods_out:
            continue
        price = m.get("pricing") or {}
        pin = _num(price.get("prompt"))
        if pin is None:
            continue
        pout = _num(price.get("completion")) or 0.0
        pcache = _num(price.get("input_cache_read"))
        out.append({
            "id": mid, "provider": p["name"],
            "in": pin * 1e6, "out": pout * 1e6,
            "cache_read": None if pcache is None else pcache * 1e6,
            "ctx": m.get("context_length"),
        })
    return out


def fetch_merge(p: dict, token: str) -> list:
    """Gateway listing: pricing per VENDOR, already dollars-per-MILLION."""
    out, cursor = [], None
    for _ in range(12):
        url = p["base_url"].rstrip("/") + "/models?limit=200"
        if cursor:
            url += "&cursor=" + urllib.parse.quote(cursor)
        data = get_json(url, token)
        for m in data.get("data") or []:
            mid = m.get("model") or ""
            if not mid or mid.endswith(":batch"):
                continue
            best = None
            for vendor in (m.get("vendors") or {}).values():
                caps = vendor.get("capabilities") or {}
                if "text" not in (caps.get("input") or []):
                    continue
                if "text" not in (caps.get("output") or []):
                    continue
                price = vendor.get("pricing") or {}
                pin = _num(price.get("input_per_million"))
                if pin is None:
                    continue
                pout = _num(price.get("output_per_million")) or 0.0
                pcache = _num(price.get("cache_read_per_million"))
                if best is None or pin < best[0]:
                    best = (pin, pout, pcache, vendor.get("context_window"))
            if best is None:
                continue
            out.append({
                "id": mid, "provider": p["name"],
                "in": best[0], "out": best[1],
                "cache_read": best[2], "ctx": best[3],
            })
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return out


DIALECTS = {"nous": fetch_nous, "merge": fetch_merge}


def scan(cfg: dict, env_values: dict) -> tuple:
    """Return (records, errors) — a dead provider is reported, never fatal."""
    records, errors = [], []
    for p in cfg["providers"]:
        dialect = DIALECTS.get(p.get("dialect") or "")
        if dialect is None:
            errors.append(f"{p.get('name', '?')}: unknown dialect {p.get('dialect')!r}")
            continue
        try:
            token = resolve_auth(p.get("auth") or {}, env_values)
            if (p.get("auth") or {}).get("kind", "none") != "none" and not token:
                raise RuntimeError("no credential found for this provider")
            found = dialect(p, token)
            records.extend(found)
            print(f"  [{p['name']}] {len(found)} routes", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - report and continue
            errors.append(f"{p.get('name', '?')}: {exc}")
    return records, errors


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------

def is_excluded(rec: dict, prefixes: list) -> bool:
    low = rec["id"].lower()
    return any(low.startswith(pref.lower()) for pref in prefixes if pref)


def matches_focus(rec: dict, focus: list) -> bool:
    low = rec["id"].lower()
    return any(term.lower() in low for term in focus if term)


def current_default(cfg: dict) -> tuple:
    """(model, provider) for the active route.

    Explicit config wins. Otherwise read Hermes config.yaml — model.default and
    model.provider together, since the same model id can exist on two providers
    at different prices.
    """
    if cfg.get("current_model"):
        return cfg["current_model"], cfg.get("current_provider") or ""
    path = os.path.join(hermes_home(), "config.yaml")
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return "", ""
    model, provider, in_model = "", "", False
    for line in lines:
        if re.match(r"^model:\s*$", line):
            in_model = True
            continue
        if in_model:
            if re.match(r"^\S", line):
                break
            m = re.match(r"^\s+default:\s*(\S+)", line)
            if m:
                model = m.group(1).strip("'\"")
            p = re.match(r"^\s+provider:\s*(\S+)", line)
            if p:
                provider = p.group(1).strip("'\"")
    return model, provider


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: str, state: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=0, sort_keys=True)
    except OSError:
        pass


def fmt(rec: dict) -> str:
    return f"{rec['in']:.4f}/{rec['out']:.4f}"


def build_report(cfg: dict, records: list, errors: list, state_path: str) -> dict:
    excluded_prefixes = cfg.get("exclude_prefixes") or []
    pool = [r for r in records if not is_excluded(r, excluded_prefixes)]
    current, current_provider = current_default(cfg)

    seen = load_state(state_path)
    first_run = not seen
    fresh = [r for r in pool if f"{r['provider']}:{r['id']}" not in seen]
    for r in pool:
        seen.setdefault(f"{r['provider']}:{r['id']}", date.today().isoformat())
    save_state(state_path, seen)

    focus = cfg.get("focus") or []
    focus_recs = sorted([r for r in pool if matches_focus(r, focus)],
                        key=lambda r: (r["in"], r["out"])) if focus else []
    cheap = sorted(pool, key=lambda r: (r["in"], r["out"]))[: int(cfg.get("top_cheap", 8))]

    ceiling = cfg.get("new_model_ceiling") or {}
    good_new = [r for r in fresh
                if r["in"] <= float(ceiling.get("input", 0))
                and r["out"] <= float(ceiling.get("output", 0))]
    good_new.sort(key=lambda r: (r["in"], r["out"]))

    return {
        "date": date.today().isoformat(),
        "current_model": current,
        "current_provider": current_provider,
        "focus": focus,
        "focus_routes": focus_recs,
        "cheapest": cheap,
        "new_count": len(fresh),
        "new_models": good_new[: int(cfg.get("max_new", 8))],
        "first_run": first_run,
        "scanned": len(records),
        "errors": errors,
    }


def render(report: dict) -> str:
    lines = [f"\U0001F50E Model Price Radar — {report['date']}",
             "(live prices $/Mtok: in/out)"]
    cur = report["current_model"]
    cur_prov = report.get("current_provider") or ""

    if report["focus"]:
        label = ", ".join(report["focus"])
        lines.append(f"\n\U0001F3AF {label} routes (cheapest first)")
        if report["focus_routes"]:
            for r in report["focus_routes"][:6]:
                on_it = (cur and r["id"] == cur
                         and (not cur_prov or r["provider"] == cur_prov))
                mark = "  \u2190 you're on this" if on_it else ""
                lines.append(f"  {fmt(r):>17}  {r['provider']:<8} {r['id']}{mark}")
        else:
            lines.append("  none found")

    lines.append("\n\U0001F4B8 Cheapest text models overall")
    for r in report["cheapest"]:
        lines.append(f"  {fmt(r):>17}  {r['provider']:<8} {r['id']}")

    if report["first_run"]:
        lines.append("\n\U0001F195 Baseline saved — new models are reported from the next run.")
    else:
        lines.append(f"\n\U0001F195 New since last run ({report['new_count']} listed, "
                     "showing good-price ones)")
        if report["new_models"]:
            for r in report["new_models"]:
                lines.append(f"  {fmt(r):>17}  {r['provider']:<8} {r['id']}")
        else:
            lines.append("  nothing new under the configured price ceiling")

    if report["errors"]:
        lines.append("")
        for err in report["errors"]:
            lines.append(f"\u26A0\uFE0F {err}")
    lines.append(f"\n({report['scanned']} routes scanned)")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Watch live LLM prices across providers.")
    here = os.path.dirname(os.path.abspath(__file__))
    parser.add_argument("--config", default=os.path.join(here, "radar.json"))
    parser.add_argument("--state", default="", help="override the baseline file path")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if not os.path.exists(args.config):
        print(f"config not found: {args.config}\n"
              f"copy radar.example.json to radar.json and edit it.", file=sys.stderr)
        return 2

    cfg = load_config(args.config)
    env_values = load_env_file(cfg.get("env_file") or os.path.join(hermes_home(), ".env"))

    records, errors = scan(cfg, env_values)
    if not records:
        print("\U0001F50E Model Price Radar — no provider reachable this run.")
        for err in errors:
            print(f"  {err}")
        return 1

    state_path = args.state or cfg.get("state_file") or ".radar_seen.json"
    if not os.path.isabs(state_path):
        state_path = os.path.join(here, state_path)

    report = build_report(cfg, records, errors, state_path)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())