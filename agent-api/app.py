import os
import random
import threading
import time
import uuid
from collections import deque

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from prometheus_client import Counter, Histogram, CONTENT_TYPE_LATEST, generate_latest

MOCK_MODE = os.getenv("MOCK_MODE", "true").lower() == "true"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
LYZR_API_KEY = os.getenv("LYZR_API_KEY")

START_TIME = time.time()
COST_PER_1K_TOKENS = 0.002  # rough blended estimate, override as needed

REQUEST_COUNT = Counter(
    "agent_requests_total", "Total agent requests", ["endpoint", "status"]
)
REQUEST_LATENCY = Histogram(
    "agent_request_latency_seconds", "Agent request latency", ["endpoint"]
)
TOKEN_USAGE = Counter("agent_tokens_total", "Tokens consumed", ["type"])
ERROR_COUNT = Counter("agent_errors_total", "Agent errors", ["error_type"])
COST_USD = Counter("agent_cost_usd_total", "Estimated USD cost of agent calls")

# Lightweight in-process mirror of the metrics above, used to power the
# built-in /dashboard view without needing a separate Grafana process.
_lock = threading.Lock()
_history = deque(maxlen=200)  # (timestamp, latency_ms, status)
_totals = {"success": 0, "error": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}

# Alerting: simple threshold rules evaluated after every request.
ERROR_RATE_THRESHOLD = 0.20
LATENCY_THRESHOLD_MS = 500
_alerts_active: dict[str, dict] = {}
_alert_log = deque(maxlen=50)

# Chaos injection: a fixed window of degraded behavior you can trigger on
# demand to demo alerting + recovery live.
CHAOS_DURATION_SECONDS = 20
_chaos_until = 0.0

app = FastAPI(title="AI Agent Observability Demo")


class ChatRequest(BaseModel):
    prompt: str


class ChatResponse(BaseModel):
    request_id: str
    response: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float


def call_agent(prompt: str) -> tuple[str, int, int]:
    """Pluggable agent backend.

    MOCK_MODE=true (default): simulates an LLM agent locally - no API key
    or network calls needed, safe for a laptop demo.
    OPENAI_API_KEY set: routes through OpenAI as a real example backend.
    LYZR_API_KEY set: wire this up to a real Lyzr agent - see README.
    """
    if MOCK_MODE or not (OPENAI_API_KEY or LYZR_API_KEY):
        return _mock_agent_call(prompt)
    if OPENAI_API_KEY:
        return _openai_agent_call(prompt)
    return _mock_agent_call(prompt)


def _mock_agent_call(prompt: str) -> tuple[str, int, int]:
    if time.time() < _chaos_until:
        # Chaos window: much slower and much more failure-prone, so the
        # dashboard's latency/error charts and alerts visibly react.
        time.sleep(random.uniform(0.8, 2.5))
        if random.random() < 0.7:
            raise RuntimeError("simulated upstream agent timeout (chaos mode)")
    else:
        time.sleep(random.uniform(0.05, 0.4))  # simulate model latency
        if random.random() < 0.08:
            raise RuntimeError("simulated upstream agent timeout")

    prompt_tokens = max(1, len(prompt.split()))
    completion = f"[mock-agent] Here is a simulated response to: {prompt[:60]}"
    completion_tokens = len(completion.split())
    return completion, prompt_tokens, completion_tokens


def _openai_agent_call(prompt: str) -> tuple[str, int, int]:
    from openai import OpenAI

    client = OpenAI(api_key=OPENAI_API_KEY)
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
    )
    usage = resp.usage
    return resp.choices[0].message.content, usage.prompt_tokens, usage.completion_tokens


def _window_stats():
    with _lock:
        history = list(_history)
        totals = dict(_totals)

    now = time.time()
    recent = [h for h in history if now - h[0] <= 60]
    latencies = sorted(h[1] for h in recent)
    p95_latency = latencies[int(len(latencies) * 0.95)] if latencies else 0
    errors_recent = sum(1 for h in recent if h[2] == "error")
    error_rate = errors_recent / len(recent) if recent else 0

    return {
        "history": history,
        "totals": totals,
        "recent": recent,
        "p95_latency": p95_latency,
        "error_rate": error_rate,
        "rps": len(recent) / 60,
    }


def _evaluate_alerts(stats):
    now = time.time()
    recent_count = len(stats["recent"])

    candidates = {}
    if recent_count >= 5 and stats["error_rate"] > ERROR_RATE_THRESHOLD:
        candidates["high_error_rate"] = (
            f"Error rate {stats['error_rate'] * 100:.0f}% over last 60s "
            f"(threshold {ERROR_RATE_THRESHOLD * 100:.0f}%)"
        )
    if recent_count >= 5 and stats["p95_latency"] > LATENCY_THRESHOLD_MS:
        candidates["high_latency"] = (
            f"p95 latency {stats['p95_latency']:.0f}ms over last 60s "
            f"(threshold {LATENCY_THRESHOLD_MS}ms)"
        )

    with _lock:
        for key, message in candidates.items():
            if key not in _alerts_active:
                _alerts_active[key] = {"key": key, "message": message, "fired_at": now}
                _alert_log.appendleft({"key": key, "message": message, "event": "fired", "at": now})
        for key in list(_alerts_active.keys()):
            if key not in candidates:
                prev = _alerts_active.pop(key)
                _alert_log.appendleft({"key": key, "message": prev["message"], "event": "resolved", "at": now})


@app.get("/health")
def health():
    return {
        "status": "ok",
        "uptime_seconds": time.time() - START_TIME,
        "mock_mode": MOCK_MODE,
    }


@app.post("/chaos/inject")
def chaos_inject():
    global _chaos_until
    _chaos_until = time.time() + CHAOS_DURATION_SECONDS
    return {"status": "chaos injected", "duration_seconds": CHAOS_DURATION_SECONDS}


@app.post("/agent/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    start = time.time()
    request_id = str(uuid.uuid4())
    try:
        response_text, prompt_tokens, completion_tokens = call_agent(req.prompt)
    except Exception as exc:
        latency = time.time() - start
        REQUEST_LATENCY.labels(endpoint="/agent/chat").observe(latency)
        REQUEST_COUNT.labels(endpoint="/agent/chat", status="error").inc()
        ERROR_COUNT.labels(error_type=type(exc).__name__).inc()
        with _lock:
            _totals["error"] += 1
            _history.append((time.time(), latency * 1000, "error"))
        _evaluate_alerts(_window_stats())
        raise HTTPException(status_code=502, detail=str(exc))

    latency = time.time() - start
    cost = (prompt_tokens + completion_tokens) / 1000 * COST_PER_1K_TOKENS
    REQUEST_LATENCY.labels(endpoint="/agent/chat").observe(latency)
    REQUEST_COUNT.labels(endpoint="/agent/chat", status="success").inc()
    TOKEN_USAGE.labels(type="prompt").inc(prompt_tokens)
    TOKEN_USAGE.labels(type="completion").inc(completion_tokens)
    COST_USD.inc(cost)
    with _lock:
        _totals["success"] += 1
        _totals["prompt_tokens"] += prompt_tokens
        _totals["completion_tokens"] += completion_tokens
        _totals["cost_usd"] += cost
        _history.append((time.time(), latency * 1000, "success"))
    _evaluate_alerts(_window_stats())

    return ChatResponse(
        request_id=request_id,
        response=response_text,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency * 1000,
    )


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/dashboard/data")
def dashboard_data():
    stats = _window_stats()
    totals = stats["totals"]
    history = stats["history"]
    now = time.time()

    with _lock:
        active_alerts = list(_alerts_active.values())
        alert_log = list(_alert_log)[:10]

    return {
        "total_requests": totals["success"] + totals["error"],
        "success_count": totals["success"],
        "error_count": totals["error"],
        "requests_per_sec_last_60s": round(stats["rps"], 3),
        "error_rate_last_60s": round(stats["error_rate"], 3),
        "p95_latency_ms_last_60s": round(stats["p95_latency"], 1),
        "prompt_tokens_total": totals["prompt_tokens"],
        "completion_tokens_total": totals["completion_tokens"],
        "cost_usd_total": round(totals["cost_usd"], 6),
        "chaos_active": now < _chaos_until,
        "chaos_remaining_seconds": max(0, round(_chaos_until - now)) if now < _chaos_until else 0,
        "active_alerts": active_alerts,
        "alert_log": alert_log,
        "history": [
            {"t": t, "latency_ms": round(latency_ms, 1), "status": status}
            for t, latency_ms, status in history[-60:]
        ],
    }


DASHBOARD_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Agent Observability</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0b0d12; --panel: #12151d; --border: #232838; --text: #e8eaf0; --muted: #8b93a7;
    --accent: #6366f1; --success: #22c55e; --warning: #f59e0b; --danger: #ef4444;
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: var(--bg); color: var(--text); margin: 0; padding: 28px 32px 48px;
  }
  header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 22px; flex-wrap: wrap; gap: 12px; }
  h1 { font-size: 19px; font-weight: 650; margin: 0; letter-spacing: -0.01em; }
  .header-right { display: flex; align-items: center; gap: 14px; }
  .live-pill {
    display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600;
    color: var(--success); background: rgba(34,197,94,0.1); border: 1px solid rgba(34,197,94,0.3);
    padding: 5px 10px; border-radius: 999px;
  }
  .live-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--success); animation: pulse 1.6s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }
  button#chaos-btn {
    font-size: 12px; font-weight: 600; color: var(--danger); background: rgba(239,68,68,0.08);
    border: 1px solid rgba(239,68,68,0.35); padding: 7px 12px; border-radius: 6px; cursor: pointer;
  }
  button#chaos-btn:hover:not(:disabled) { background: rgba(239,68,68,0.18); }
  button#chaos-btn:disabled { opacity: 0.55; cursor: default; }

  #alert-banner {
    display: none; background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.4);
    color: #fca5a5; border-radius: 8px; padding: 10px 14px; font-size: 13px; margin-bottom: 18px;
  }
  #alert-banner.show { display: block; }

  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 22px; }
  .tile { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; border-left: 3px solid var(--accent); }
  .tile .label { font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); margin-bottom: 6px; }
  .tile .value { font-size: 23px; font-weight: 650; }
  .value.error { color: var(--danger); }
  .value.ok { color: var(--text); }

  .charts { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 22px; }
  .chart-card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
  .chart-card .title { font-size: 12px; font-weight: 600; color: var(--muted); margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.04em; }
  .chart-card canvas { max-height: 190px; }

  .bottom { display: grid; grid-template-columns: 260px 1fr; gap: 14px; }
  .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
  .panel .title { font-size: 12px; font-weight: 600; color: var(--muted); margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.04em; }
  .alert-row { display: flex; gap: 8px; align-items: flex-start; font-size: 12.5px; padding: 7px 0; border-bottom: 1px solid var(--border); }
  .alert-row:last-child { border-bottom: none; }
  .alert-dot { width: 7px; height: 7px; border-radius: 50%; margin-top: 4px; flex-shrink: 0; }
  .alert-dot.fired { background: var(--danger); }
  .alert-dot.resolved { background: var(--success); }
  .alert-text .msg { color: var(--text); }
  .alert-text .time { color: var(--muted); font-size: 11px; }
  .empty { color: var(--muted); font-size: 12.5px; }

  @media (max-width: 800px) {
    .charts { grid-template-columns: 1fr; }
    .bottom { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
  <header>
    <h1>AI Agent Observability</h1>
    <div class="header-right">
      <span class="live-pill"><span class="live-dot"></span>LIVE</span>
      <button id="chaos-btn" onclick="injectChaos()">Inject Failure Spike (20s)</button>
    </div>
  </header>

  <div id="alert-banner"></div>
  <div class="grid" id="tiles"></div>

  <div class="charts">
    <div class="chart-card"><div class="title">Request Rate (req/s, 60s window)</div><canvas id="chart-rps"></canvas></div>
    <div class="chart-card"><div class="title">p95 Latency (ms, 60s window)</div><canvas id="chart-latency"></canvas></div>
    <div class="chart-card"><div class="title">Token Usage Rate (tokens/s)</div><canvas id="chart-tokens"></canvas></div>
    <div class="chart-card"><div class="title">Cumulative Estimated Cost (USD)</div><canvas id="chart-cost"></canvas></div>
  </div>

  <div class="bottom">
    <div class="panel">
      <div class="title">Success vs Error</div>
      <canvas id="chart-donut" height="180"></canvas>
    </div>
    <div class="panel">
      <div class="title">Alert Log</div>
      <div id="alert-log"><div class="empty">No alerts yet.</div></div>
    </div>
  </div>

<script>
const MAX_POINTS = 40;
const labels = [];
const series = { rps: [], latency: [], promptRate: [], completionRate: [], cost: [] };
let prevTokens = null, prevT = null;

function makeLineChart(ctx, datasets, opts = {}) {
  return new Chart(ctx, {
    type: 'line',
    data: { labels: [], datasets },
    options: {
      responsive: true, animation: false, maintainAspectRatio: false,
      plugins: { legend: { display: datasets.length > 1, labels: { color: '#8b93a7', boxWidth: 10, font: { size: 11 } } } },
      scales: {
        x: { ticks: { display: false }, grid: { color: '#1c2130' } },
        y: { beginAtZero: true, ticks: { color: '#8b93a7', font: { size: 10 } }, grid: { color: '#1c2130' } },
      },
      elements: { point: { radius: 0 }, line: { tension: 0.3 } },
      ...opts,
    },
  });
}

const chartRps = makeLineChart(document.getElementById('chart-rps'),
  [{ data: [], borderColor: '#6366f1', backgroundColor: 'rgba(99,102,241,0.12)', fill: true, borderWidth: 2 }]);
const chartLatency = makeLineChart(document.getElementById('chart-latency'),
  [{ data: [], borderColor: '#f59e0b', backgroundColor: 'rgba(245,158,11,0.12)', fill: true, borderWidth: 2 }]);
const chartTokens = makeLineChart(document.getElementById('chart-tokens'),
  [{ label: 'prompt', data: [], borderColor: '#6366f1', borderWidth: 2 },
   { label: 'completion', data: [], borderColor: '#22c55e', borderWidth: 2 }]);
const chartCost = makeLineChart(document.getElementById('chart-cost'),
  [{ data: [], borderColor: '#22c55e', backgroundColor: 'rgba(34,197,94,0.12)', fill: true, borderWidth: 2 }]);
const chartDonut = new Chart(document.getElementById('chart-donut'), {
  type: 'doughnut',
  data: { labels: ['Success', 'Error'], datasets: [{ data: [0, 0], backgroundColor: ['#22c55e', '#ef4444'], borderWidth: 0 }] },
  options: { animation: false, plugins: { legend: { position: 'bottom', labels: { color: '#8b93a7', font: { size: 11 } } } } },
});

function pushPoint(chart, idx, value) {
  chart.data.labels = labels;
  chart.data.datasets[idx].data.push(value);
  if (chart.data.datasets[idx].data.length > MAX_POINTS) chart.data.datasets[idx].data.shift();
}

async function injectChaos() {
  const btn = document.getElementById('chaos-btn');
  btn.disabled = true;
  try { await fetch('/chaos/inject', { method: 'POST' }); } catch (e) {}
}

function fmtAgo(seconds) {
  if (seconds < 60) return Math.round(seconds) + 's ago';
  return Math.round(seconds / 60) + 'm ago';
}

async function refresh() {
  let data;
  try {
    const res = await fetch('/dashboard/data');
    data = await res.json();
  } catch (e) { return; }

  const tiles = [
    ['Total Requests', data.total_requests, 'ok'],
    ['Errors', data.error_count, data.error_count > 0 ? 'error' : 'ok'],
    ['Req/sec (60s)', data.requests_per_sec_last_60s, 'ok'],
    ['Error rate (60s)', (data.error_rate_last_60s * 100).toFixed(1) + '%', data.error_rate_last_60s > 0.05 ? 'error' : 'ok'],
    ['p95 Latency (60s)', data.p95_latency_ms_last_60s + ' ms', data.p95_latency_ms_last_60s > 500 ? 'error' : 'ok'],
    ['Prompt Tokens', data.prompt_tokens_total, 'ok'],
    ['Completion Tokens', data.completion_tokens_total, 'ok'],
    ['Est. Cost (USD)', '$' + data.cost_usd_total.toFixed(6), 'ok'],
  ];
  document.getElementById('tiles').innerHTML = tiles.map(([label, value, cls]) =>
    `<div class="tile"><div class="label">${label}</div><div class="value ${cls}">${value}</div></div>`
  ).join('');

  const banner = document.getElementById('alert-banner');
  if (data.active_alerts.length > 0) {
    banner.className = 'show';
    banner.textContent = data.active_alerts.length + ' active alert(s): ' + data.active_alerts.map(a => a.message).join(' | ');
  } else {
    banner.className = '';
  }

  const logEl = document.getElementById('alert-log');
  if (data.alert_log.length === 0) {
    logEl.innerHTML = '<div class="empty">No alerts yet.</div>';
  } else {
    const now = Date.now() / 1000;
    logEl.innerHTML = data.alert_log.map(a => `
      <div class="alert-row">
        <div class="alert-dot ${a.event}"></div>
        <div class="alert-text">
          <div class="msg">${a.event === 'fired' ? 'FIRED' : 'RESOLVED'} &mdash; ${a.message}</div>
          <div class="time">${fmtAgo(now - a.at)}</div>
        </div>
      </div>`).join('');
  }

  const btn = document.getElementById('chaos-btn');
  if (data.chaos_active) {
    btn.disabled = true;
    btn.textContent = 'Chaos active (' + data.chaos_remaining_seconds + 's)...';
  } else {
    btn.disabled = false;
    btn.textContent = 'Inject Failure Spike (20s)';
  }

  chartDonut.data.datasets[0].data = [data.success_count, data.error_count];
  chartDonut.update();

  const now = Date.now() / 1000;
  labels.push(new Date().toLocaleTimeString());
  if (labels.length > MAX_POINTS) labels.shift();

  pushPoint(chartRps, 0, data.requests_per_sec_last_60s);
  pushPoint(chartLatency, 0, data.p95_latency_ms_last_60s);
  pushPoint(chartCost, 0, data.cost_usd_total);

  const totalTokens = data.prompt_tokens_total + data.completion_tokens_total;
  if (prevTokens !== null && prevT !== null) {
    const dt = Math.max(now - prevT, 1);
    const promptRate = Math.max(0, (data.prompt_tokens_total - prevTokens.prompt) / dt);
    const completionRate = Math.max(0, (data.completion_tokens_total - prevTokens.completion) / dt);
    pushPoint(chartTokens, 0, Math.round(promptRate * 10) / 10);
    pushPoint(chartTokens, 1, Math.round(completionRate * 10) / 10);
  }
  prevTokens = { prompt: data.prompt_tokens_total, completion: data.completion_tokens_total };
  prevT = now;

  [chartRps, chartLatency, chartTokens, chartCost].forEach(c => c.update());
}

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML
