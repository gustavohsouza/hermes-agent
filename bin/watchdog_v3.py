#!/usr/bin/env python3
"""Watchdog v3 block (Claude Fable, 2026-09-04, Gustavo's blocks 4a, 4b and 6).
Prints one line per signal. Lines starting with "ALARM " are problems; lines
starting with "INFO " are context. Called by watchdog.sh in --daily mode only
(azcost takes ~10 s). Everything fails soft: a missing source prints INFO with
the reason, never raises.

Signals
  4a  authoritative billed spend from Azure Cost Management via azcost.py (BRL,
      BRT day D-1). The G-brain ledger is shown only as incomplete activity
      telemetry; it excludes embeddings/raw-SDK calls. Expect Azure billing to
      lag 8 to 24 hours.
  4b  unbilled Hermes Azure activity: calls and tokens from Hermes' own state.db
      (session_model_usage, billing_provider azure-foundry). No session cost is
      inferred because Azure Cost Management does not allocate invoice cost to
      Hermes sessions.
  6   cancelled subagents per dream cycle (24h), sleep + offline minutes (D-1),
      search_degraded count in Hermes tool results (24h), gateway restarts (24h)
      and last exit code, ledger D-1 by phase (top 3).
"""
import datetime as dt, json, os, pathlib, re, sqlite3, subprocess, urllib.request

from watchdog_search_degradation import count_degraded_search_results

HOME = pathlib.Path(os.environ.get("WATCHDOG_HOME", pathlib.Path.home()))
PSQL = "/opt/homebrew/opt/postgresql@17/bin/psql"
PSQL = os.environ.get("WATCHDOG_PSQL", PSQL)
HPY = os.environ.get("WATCHDOG_HPY", str(HOME / ".hermes/hermes-agent/venv/bin/python"))
AZCOST = os.environ.get("WATCHDOG_AZCOST", str(HOME / ".hermes/skills/operations/azure-cost/azcost.py"))
TZ = dt.timezone(dt.timedelta(hours=-3))
now = dt.datetime.now(TZ)
d1 = dt.date.fromisoformat(os.environ["WATCHDOG_D1"]) if os.environ.get("WATCHDOG_D1") else (now - dt.timedelta(days=1)).date()
FX_FALLBACK = 5.40

out = []
def info(s): out.append("INFO " + s)
def alarm(s): out.append("ALARM " + s)

def q(sql):
    r = subprocess.run([PSQL, "-d", "gbrain", "-At", "-F", "|", "-c", sql], capture_output=True, text=True, timeout=60)
    return [l.split("|") for l in r.stdout.splitlines() if l.strip()]

# ---------- 4a ledger vs invoice ----------
try:
    led = q(f"select coalesce(round(sum(cost_usd)::numeric,2),0) from chat_usage_log where (created_at at time zone 'America/Sao_Paulo')::date = '{d1}'")
    ledger_usd = float(led[0][0]) if led else 0.0
    phases = q(f"select coalesce(nullif(phase,''),'sem-fase'), coalesce(round(sum(cost_usd)::numeric,2),0) from chat_usage_log where (created_at at time zone 'America/Sao_Paulo')::date = '{d1}' group by 1 order by 2 desc limit 3")
    inv_brl = None
    try:
        r = subprocess.run([HPY, AZCOST, "--from", str(d1), "--to", str(d1), "--scope", "gus", "--by", "day", "--json"], capture_output=True, text=True, timeout=90)
        j = json.loads(r.stdout[r.stdout.find("{"):])
        det = (j.get("gus") or {}).get("detalhe") or {}
        inv_brl = float(det.get(d1.strftime("%Y%m%d"), (j.get("gus") or {}).get("total", 0)) or 0)
    except Exception as e:
        info(f"fatura Azure de {d1}: azcost falhou ({str(e)[:60]})")
    fx = float(os.environ.get("WATCHDOG_FX_BRL_PER_USD", FX_FALLBACK))
    if "WATCHDOG_FX_BRL_PER_USD" not in os.environ:
        try:
            with urllib.request.urlopen("https://open.er-api.com/v6/latest/USD", timeout=8) as resp:
                fx = float(json.load(resp)["rates"]["BRL"])
        except Exception:
            pass
    if inv_brl is not None:
        inv_usd = inv_brl / fx
        ratio = (ledger_usd / inv_usd) if inv_usd > 0 else None
        rtxt = f"ledger/fatura {ratio:.2f}x" if ratio else "fatura ainda zerada (lag)"
        info(f"gasto faturado Azure Cost Management {d1}: R$ {inv_brl:.2f} (aprox. US$ {inv_usd:.2f} a {fx:.2f}); fonte autoritativa, sujeita a lag de 8-24h")
        info(f"ledger gbrain incompleto {d1}: US$ {ledger_usd:.2f}; exclui embeddings/raw-SDK e nao e gate de gasto | {rtxt}")
        if ratio and (ratio < 0.5 or ratio > 2.0) and inv_usd > 5:
            alarm(f"telemetria incompleta: ledger gbrain fora da fatura Azure em {d1} ({rtxt}); nao usar o ledger como gasto faturado")
        if inv_usd > 25:
            alarm(f"gasto faturado autoritativo Azure Cost Management de {d1} acima de US$ 25 (R$ {inv_brl:.2f}, aprox. US$ {inv_usd:.2f}); sujeito a lag de 8-24h")
    if phases:
        info("ledger por fase " + ", ".join(f"{p}={c}" for p, c in phases))

except Exception as e:
    info(f"4a falhou: {str(e)[:80]}")

# ---------- 4b Hermes Azure activity (explicitly unbilled) ----------
try:
    con = sqlite3.connect(str(HOME / ".hermes/state.db"))
    rows = con.execute("""select model, sum(api_call_count),
               sum(input_tokens+output_tokens+cache_read_tokens+cache_write_tokens),
               coalesce(group_concat(distinct cost_status),'unknown')
        from session_model_usage where (billing_provider like '%azure%' or billing_base_url like '%azure%')
        and date(last_seen,'unixepoch','localtime') = ? group by 1""", (str(d1),)).fetchall()
    if rows:
        parts = []
        for m, calls, tokens, statuses in rows:
            parts.append(f"{m} {calls} chamadas, {(tokens or 0)/1000:.0f}k tokens, cost_status={statuses}")
        info(f"atividade Hermes na Azure nao faturada/alocada {d1}: " + "; ".join(parts) + "; custo de sessao desconhecido, nao interpretar US$0 como gratuito")
    else:
        info(f"atividade Hermes na Azure {d1}: 0 chamadas registradas; nao implica gasto Azure zero")
except Exception as e:
    info(f"4b falhou: {str(e)[:80]}")

# ---------- 6 extra signals ----------
try:
    cyc = q("""select count(distinct queue) filter (where queue like 'dream-inline-%'),
                      count(*) filter (where status='cancelled' and queue like 'dream-inline-%'),
                      count(*) filter (where status='completed' and queue like 'dream-inline-%')
               from minion_jobs where created_at > now() - interval '24 hours'""")
    if cyc:
        ncyc, canc, comp = (int(x) for x in cyc[0])
        info(f"sonho 24h: {ncyc} ciclos, {comp} subagentes concluidos, {canc} cancelados")
        if canc > 50 and canc > comp:
            alarm(f"loop de enfileirar-e-cancelar de volta: {canc} cancelados vs {comp} concluidos em 24h; regra da skill: desligar dream.synthesize.enabled e avisar com numeros")
except Exception as e:
    info(f"sonho: {str(e)[:60]}")

try:
    hb = [int(x) for x in (HOME / ".hermes/state/heartbeat.log").read_text().split() if x.strip()]
    y0 = int(dt.datetime.combine(d1, dt.time(0, 0), TZ).timestamp()); y1 = y0 + 86400
    ys = [t for t in hb if y0 <= t < y1]
    slept = sum((b - a - 60) for a, b in zip(ys, ys[1:]) if b - a > 150) // 60 if len(ys) > 1 else None
    off = 0
    try:
        for ln in (HOME / ".hermes/state/netstate.log").read_text().splitlines():
            ts, st = ln.split()
            if y0 <= int(ts) < y1 and st == "0": off += 1
    except Exception:
        pass
    info(f"Mac {d1}: dormiu {slept if slept is not None else '?'} min, acordado sem rede {off} min")
except Exception as e:
    info(f"sono: {str(e)[:60]}")

try:
    con = sqlite3.connect(str(HOME / ".hermes/state.db"))
    since = (now - dt.timedelta(hours=24)).timestamp()
    n = count_degraded_search_results(con, since=since)
    if n:
        alarm(f"busca do gbrain degradada em {n} respostas MCP nas ultimas 24h (keyword_only_no_embedding_provider): o env OPENAI_BASE_URL do MCP sumiu")
    else:
        info("busca MCP: nenhuma resposta degradada em 24h")
except Exception as e:
    info(f"search_degraded: {str(e)[:60]}")

try:
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/ai.hermes.gateway"], capture_output=True, text=True, timeout=10)
    m = re.search(r"last exit code = ([^\n]+)", r.stdout)
    code = m.group(1) if m else "?"
    restarts = 0
    try:
        for ln in (HOME / ".hermes/logs/gateway.log").read_text(errors="ignore").splitlines()[-20000:]:
            if "Gateway housekeeping started" in ln:
                try:
                    t = dt.datetime.strptime(ln[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
                    if t.timestamp() > since: restarts += 1
                except Exception:
                    pass
    except Exception:
        pass
    info(f"gateway: {restarts} inicio(s) em 24h, ultimo exit code {code}")
    if restarts > 3:
        alarm(f"gateway do Hermes reiniciou {restarts} vezes em 24h (exit code {code})")
except Exception as e:
    info(f"gateway: {str(e)[:60]}")

# ---------- LinkedIn nightly (people_linkedin.py) ----------
# 2026-09-17 (pedido do Gustavo): os skip-paths do enriquecimento LinkedIn sao
# silenciosos por design (li_at morto ou 9444 fora viram "skipped" so no log).
# Este bloco transforma silencio em alarme: ultima rodada >36h, skipped,
# session=false ou visited=0 gritam no daily. A rodada so conta quando o JSON
# aparece imediatamente apos a linha de comando do people_linkedin.py.
try:
    _lk_log = pathlib.Path(os.environ.get(
        "WATCHDOG_LINKEDIN_LOG", str(HOME / ".gbrain/people_nightly.log")))
    _lk_ts, _lk_stats = None, None
    _lk_lines = _lk_log.read_text(errors="ignore").splitlines()
    for _i, _ln in enumerate(_lk_lines):
        if "people_linkedin.py" not in _ln or not re.match(r"^\[[0-9-]+ [0-9:]+\] >> ", _ln):
            continue
        _m = re.match(r"\[([0-9-]+ [0-9:]+)\]", _ln)
        if not _m:
            continue
        _candidate = None
        for _next in _lk_lines[_i + 1:_i + 4]:
            if re.match(r"^\[[0-9-]+ [0-9:]+\] ", _next):
                break
            try:
                _parsed = json.loads(_next)
                if isinstance(_parsed, dict) and "session" in _parsed:
                    _candidate = _parsed
                    break
            except (json.JSONDecodeError, TypeError):
                pass
        if _candidate is not None:
            _lk_stats = _candidate
            _lk_ts = dt.datetime.strptime(
                _m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
    if _lk_stats is None:
        alarm("linkedin nightly: nenhuma rodada encontrada no people_nightly.log")
    else:
        _age_h = (now - _lk_ts).total_seconds() / 3600
        _v = int(_lk_stats.get("visited") or 0)
        _upd = int(_lk_stats.get("updated") or 0)
        if _age_h > 36:
            alarm(f"linkedin nightly parado ha {_age_h:.0f}h (ultima rodada {_lk_ts})")
        elif _lk_stats.get("skipped") or _lk_stats.get("session") is not True:
            alarm(f"linkedin nightly pulou em silencio: {_lk_stats.get('skipped') or 'session=false'} "
                  f"(sessao LinkedIn do Hermes Chrome provavelmente expirou; relogar li_at no profile ~/hermes/chrome-profile)")
        elif _v == 0:
            alarm("linkedin nightly rodou mas visitou 0 perfis; verificar fila e Chrome 9444")
        else:
            info(f"linkedin nightly ok: {_v} perfis visitados, {_upd} atualizados ({_lk_ts})")
except Exception as e:
    alarm(f"linkedin nightly: check falhou ({str(e)[:60]})")

print("\n".join(out))
