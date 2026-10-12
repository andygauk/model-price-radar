# model-price-radar

**Find out whether you're on the cheapest route for the model you actually use — and get told every week.**

LLM prices move constantly, and the same model is often served by several providers at very different rates. `model-price-radar` reads the **live** catalogues of the providers you configure, prints a short digest of what's cheapest, and flags new models worth trying. Run it by hand or schedule it.

No dependencies. No API keys of its own. Standard library only.

```
🔎 Model Price Radar — 2026-10-12
(live prices $/Mtok: in/out)

🎯 deepseek routes (cheapest first)
      0.0128/0.7680  nous     ~deepseek/deepseek-v4-flash-latest
      0.0240/1.5360  nous     deepseek/deepseek-v4-flash
      0.0336/1.1880  nous     ~deepseek/deepseek-flash-latest
      0.0350/0.0700  merge    deepseek/deepseek-v4-flash-0731  ← you're on this
      0.0700/0.1400  nous     deepseek/deepseek-v4-flash-0731

💸 Cheapest text models overall
      0.0000/0.0000  nous     stepfun/step-5-preview:free
      0.0000/0.0000  nous     meituan/longcat-2.0:free
      ...

🆕 New since last run (3 listed, showing good-price ones)
      0.0300/0.1300  nous     qwen/qwen3.7-flash

(598 routes scanned)
```

## Why this exists

I kept asking "am I on the best model?" and getting an answer that went stale within a fortnight. Two things were true and neither was obvious:

1. **Per-token price is the wrong number.** My workload is cache-read heavy — hundreds of millions of cached tokens against a few million fresh ones. The cache-read rate decided the winner, not the headline input price. Ranking on input price picked the *wrong* route.
2. **"Cheapest" isn't the same as "cheapest for me".** I already pay flat monthly rates for some vendors, so their prices are noise. A list that ignores that is a list I have to re-filter by hand every week.

So the digest is short on purpose: *the routes I care about, cheapest first* and *anything genuinely new worth a look*.

## Quick start

```bash
git clone https://github.com/andygauk/model-price-radar
cd model-price-radar

cp radar.example.json radar.json     # then edit it
python radar.py
```

Requires Python 3.9+ (nothing else).

## Configuration

Everything lives in `radar.json` (gitignored — copy from `radar.example.json`).

| Key | Meaning |
|---|---|
| `providers` | Which catalogues to scan, and how to authenticate each |
| `focus` | Substrings to highlight, cheapest first — e.g. `["deepseek"]` |
| `exclude_prefixes` | Model id prefixes to drop entirely — e.g. vendors you already pay flat rates for |
| `new_model_ceiling` | Only report new models under this `$/Mtok` input/output ceiling |
| `current_model` / `current_provider` | Your active route, so the digest can mark it. Leave blank to auto-detect from Hermes |
| `state_file` | Where the "models seen" baseline is kept |

### Providers

Each provider names a **dialect** (how that provider's `/models` response is shaped) and an **auth** method.

```json
{
  "name": "merge",
  "dialect": "merge",
  "base_url": "https://api-gateway.merge.dev/v1",
  "auth": { "kind": "env", "var": "MERGE_API_KEY" }
}
```

Auth kinds:

- `{"kind": "env", "var": "MERGE_API_KEY"}` — read the key from the environment or from `env_file`.
- `{"kind": "hermes_auth", "provider": "nous"}` — read the OAuth token from `~/.hermes/auth.json`. Handy if you run [Hermes](https://github.com/NousResearch/hermes-agent).
- `{"kind": "none"}` — no credential needed.

Included dialects: **`nous`** (OpenAI-style listing, prices per *token*) and **`merge`** (gateway listing, prices per *vendor*, already per *million*). Adding another provider is a small function — see *Extending* below.

## Options

```bash
python radar.py                        # default config beside the script
python radar.py --config other.json    # a different config
python radar.py --state /tmp/seen.json # a different baseline file
python radar.py --json                 # machine-readable, for your own tooling
```

Exit codes: `0` ok, `1` no provider reachable, `2` config missing/invalid.

## Running it weekly

Any scheduler works. Cron example (Mondays 8am, output to your inbox):

```cron
0 8 * * 1 cd /path/to/model-price-radar && /usr/bin/python3 radar.py
```

On Windows, Task Scheduler with the same command. The script prints the digest to stdout and nothing else, so whatever captures stdout gets a clean report.

## The one thing that will bite you

**A catalogue listing is a candidate, not proof.** Some listed routes 404 when you actually call them. Before you switch anything, send one real request and confirm it answers. The script deliberately does not switch your default — it tells you what's cheapest and lets you decide.

## Extending

To add a provider, write a function that returns a list of records:

```python
{
    "id": "vendor/model",     # the model id
    "provider": "myname",     # your provider name
    "in": 0.035,              # $ per MILLION input tokens
    "out": 0.070,             # $ per MILLION output tokens
    "cache_read": 0.007,      # $ per MILLION cached input tokens, or None
    "ctx": 1000000,           # context window, or None
}
```

Register it in the `DIALECTS` dict and name it in your config. Prices must be normalised to dollars per **million** tokens.

## Licence

MIT — see [LICENSE](LICENSE).

Built while scratching my own itch. If it saves you a few quid a month, it's done its job.