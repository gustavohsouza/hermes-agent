#!/bin/zsh
# Health check do G-brain + Hermes. Entrega no WhatsApp do Gustavo pela conexao
# nativa do Hermes (hermes send). Telegram e Pombo aposentados.
#
# DOIS MODOS:
#   watchdog.sh            diario 08:30. Manda SEMPRE uma linha. Silencio nunca
#                          e sinal de saude: a linha diaria E o heartbeat.
#   watchdog.sh --check    horario. Fica quieto se nada mudou; fala quando um
#                          problema APARECE ou quando um problema SOME.
#
# Reescrito em 2026-08-30. A versao anterior era cega por construcao:
#   - contava falhas do autopilot com `tail -400`, entao reportava "8 jobs
#     mortos" quando havia 112. Reportava a janela, nao o estado.
#   - imprimia o total de links mas nao tinha regra nenhuma sobre ele. Os links
#     de mention pararam em 28/08 e ninguem soube por 2 dias.
#   - nao olhava a fila de minion_jobs: 1.484 jobs subagent mortos passaram em
#     branco.
#   - nao olhava o estado dos crons do Hermes: o Morning briefing morreu em
#     blocked_config e nao gerou nenhuma mensagem.
#   - nao avisava quando um job estava esperando decisao do Gustavo.
# Agora cada checagem tem uma CHAVE estavel; o estado fica em
# ~/.hermes/watchdog-state.json e o modo --check compara contra ele.

export PATH="$HOME/.bun/bin:/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
source "$HOME/.zshenv" 2>/dev/null

MODE="${1:---daily}"
STATE="${WATCHDOG_STATE:-$HOME/.hermes/watchdog-state.json}"
PSQL="psql -d gbrain -tAc"
HERMES_PY="$HOME/.hermes/hermes-agent/venv/bin/python"
WATCHDOG_DISPATCH="${WATCHDOG_DISPATCH:-$HOME/hermes/bin/watchdog_dispatch.py}"
WATCHDOG_INTAKE="${WATCHDOG_INTAKE:-$HOME/.hermes/bin/watchdog-kanban-intake}"
WATCHDOG_SPOOL="${WATCHDOG_SPOOL:-$HOME/.hermes/state/watchdog-spool}"

# Tests must fail closed against external delivery. A harness may set
# WATCHDOG_TEST_MODE=1, but only after redirecting both state and notification
# capture away from production.
if [ "${WATCHDOG_TEST_MODE:-0}" = "1" ]; then
  case "$STATE" in
    "$HOME/.hermes/watchdog-state.json")
      print -u2 "WATCHDOG_TEST_MODE requires an isolated STATE path"
      exit 64
      ;;
  esac
  [ -n "${WATCHDOG_TEST_NOTIFICATION_LOG:-}" ] || {
    print -u2 "WATCHDOG_TEST_MODE requires WATCHDOG_TEST_NOTIFICATION_LOG"
    exit 64
  }
  [ -n "${WATCHDOG_LOG:-}" ] || {
    print -u2 "WATCHDOG_TEST_MODE requires WATCHDOG_LOG"
    exit 64
  }
fi

# P = problemas ativos nesta rodada, no formato "chave|texto legivel"
P=(); OK=()
INFO=()
add()  { P+=("$1|$2"); }
good() { OK+=("$1"); }

notify() {
  local msg="$1"
  local i
  local -a snapshot
  snapshot=("$MODE" "$msg" "${NEW:-}" "${GONE:-}" "${#P[@]}")
  for e in "${P[@]}"; do
    snapshot+=("${e%%|*}" "${e#*|}")
  done

  if [ "${WATCHDOG_TEST_MODE:-0}" = "1" ]; then
    print -r -- "$msg" >> "$WATCHDOG_TEST_NOTIFICATION_LOG"
  fi

  # Pass opaque alert data only over a NUL-delimited stdin protocol to a fixed
  # argv. The dispatcher atomically spools before bridge submission and safely
  # retries the same canonical event identity on later invocations.
  if printf '%s\0' "${snapshot[@]}" | "$HERMES_PY" "$WATCHDOG_DISPATCH" \
      --state "$STATE" --spool "$WATCHDOG_SPOOL" --launcher "$WATCHDOG_INTAKE" \
      2>>"${WATCHDOG_LOG:-/tmp/watchdog.log}"; then
    return 0
  fi

  local failure="Watchdog automation failed; alert preserved for retry in the local spool."
  print -r -- "[$(date +%F\ %H:%M)] $failure" >> "${WATCHDOG_LOG:-/tmp/watchdog.log}"
  if [ "${WATCHDOG_TEST_MODE:-0}" = "1" ]; then
    print -r -- "$failure" >> "$WATCHDOG_TEST_NOTIFICATION_LOG"
    return 1
  fi
  if "$HERMES_PY" -m hermes_cli.main send --to whatsapp -q \
      "*Watchdog G-brain*\n$failure" 2>>"${WATCHDOG_LOG:-/tmp/watchdog.log}"; then
    return 1
  fi
  osascript -e 'display notification "Watchdog automation failed; alert preserved for retry." with title "Watchdog G-brain"' 2>/dev/null
  return 1
}

# ---------------------------------------------------------------- servicos
# launchctl, nao pgrep: o pgrep do macOS usa ERE, entao o antigo padrao com
# "\|" nunca casava e o watchdog gritava "gateway caido" todo dia com o
# gateway no ar. Ventura+ pode registrar o LaunchAgent em user/<uid>, mas
# instalacoes existentes ainda podem usar gui/<uid>; aceitar ambos sem deixar
# de exigir state=running.
"${0:A:h}/watchdog_gateway_running.sh" "$(id -u)" "ai.hermes.gateway" \
  && good gateway || add gateway "gateway do Hermes caido"
curl -s -m 8 http://127.0.0.1:8011/health >/dev/null 2>&1 \
  && good proxy-azure || add proxy "proxy Azure fora"
# 2026-09-11: sidecar Codex (8010) e router (8012) aposentados na remocao da cota codex; check removido.
pg_isready -q 2>/dev/null && good postgres || add postgres "Postgres fora"
# O autopilot caido e problema, EXCETO durante uma sintese: o wrapper do
# `gbrain dream --phase synthesize` derruba o autopilot de proposito para nao
# disputar cota da Azure com ele (medido em 30/08: com os dois no ar, 78 rate
# limits em 3 minutos; com so a sintese, zero). Alertar nesse caso seria gritar
# por um estado correto, durante horas, que e como um watchdog vira ruido.
if pgrep -f "bun.*gbrain autopilot" >/dev/null; then
  good autopilot
elif pgrep -f "gbrain dream --phase synthesize" >/dev/null; then
  good "autopilot-pausado-p/sintese"
else
  add autopilot "autopilot do gbrain caido"
fi

# ---------------------------------------------------------------- brain
PAGES=$(${=PSQL} "select count(*) from pages where deleted_at is null;" 2>/dev/null)
LINKS=$(${=PSQL} "select count(*) from links;" 2>/dev/null)
STALE=$(${=PSQL} "select count(*) from content_chunks where embedding is null;" 2>/dev/null)
# Embeddings: o que importa nao e o TAMANHO do backlog, e se ele esta encolhendo.
# Um backfill saudavel passa horas com dezenas de milhares de chunks pendentes.
# Alarme so quando esta grande E parado (nenhum embedding novo em 1h).
if [ "${STALE:-0}" -gt 2000 ]; then
  # embedded_at, nao updated_at: content_chunks nao tem updated_at, e uma coluna
  # inexistente faz o psql errar e a checagem passar em silencio (foi assim que a
  # regra de halt do extract ficou morta sem ninguem perceber).
  MOVING=$(${=PSQL} "select 1 from content_chunks
                     where embedded_at > now() - interval '1 hour' limit 1;" 2>/dev/null)
  [ -z "$MOVING" ] && add embeddings "${STALE} chunks sem embedding e o backfill esta parado"
fi

# backup do repo do brain
LAST=$(cd "$HOME/hermes/brain" && git log -1 --format=%ct 2>/dev/null)
NOW=$(date +%s)
[ -n "$LAST" ] && [ $((NOW-LAST)) -gt 172800 ] && add backup "brain sem backup ha $(( (NOW-LAST)/3600 ))h"

# Google do gbrain: diferenciar o cursor incremental expirado da People API
# (HTTP 400, reparavel sem login) de falha real de OAuth (invalid_grant,
# token revoked ou HTTP 401/403 de autenticacao). O status pode rotular ambos
# como refresh_probe != ok, portanto a assinatura recente do stderr decide.
GST=$(gbrain google status --json 2>/dev/null | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print(''); raise SystemExit
print(','.join(a['account'] for a in d.get('accounts',[]) if a.get('refresh_probe')!='ok'))" 2>/dev/null)
if [ -n "$GST" ]; then
  GLOG="$HOME/.gbrain/autopilot.err"
  GSIG=""
  if [ -f "$GLOG" ]; then
    # Use the newest relevant line. Old Contacts and OAuth failures can coexist
    # in this long-lived log, so "any match" would let stale evidence win.
    GSIG=$(tail -n 5000 "$GLOG" 2>/dev/null | grep -Ei 'HTTP 400 on people.*Sync token is expired|Sync token is expired.*HTTP 400 on people|invalid_grant|token (has been )?revoked|revoked.*token|HTTP (401|403).*(auth|oauth|token|credential)|((auth|oauth|token|credential)[^[:space:]]*.*HTTP (401|403))' | tail -n 1)
  fi
  if printf '%s\n' "$GSIG" | grep -Eqi 'HTTP 400 on people.*Sync token is expired|Sync token is expired.*HTTP 400 on people'; then
    add google-contacts-sync-token "sync token Contacts expirado ($GST): auto-corrigivel, sem reauth; backup + remover contacts_sync_token dos .google-source.json + gbrain sync --all"
  elif printf '%s\n' "$GSIG" | grep -Eqi 'invalid_grant|token (has been )?revoked|revoked.*token|HTTP (401|403).*(auth|oauth|token|credential)|((auth|oauth|token|credential)[^[:space:]]*.*HTTP (401|403))'; then
    add google-token "OAuth Google invalido ($GST): gbrain google connect --reauth $GST"
  else
    add google-token-unknown "Google probe falhou ($GST), mas sem assinatura de OAuth revogado; verificar gbrain google status --json e $GLOG antes de pedir reauth"
  fi
fi

# credenciais do silo Google do Hermes (skill google-workspace). Silo separado
# do gbrain: em 30/08 o gbrain estava 100% saudavel e o Morning briefing morria
# porque ESTE silo estava vazio. Checar os dois, sempre.
for f in google_token.json google_client_secret.json; do
  [ -f "$HOME/.hermes/$f" ] || add hermes-google "silo Google do Hermes sem $f (Morning briefing nao roda)"
done

LOOPS=$(${=PSQL} "select count(*) from open_loops where status='open';" 2>/dev/null)
[ "${LOOPS:-0}" -gt 800 ] && add open-loops "${LOOPS} open loops, poda travada"

# ------------------------------------------------- links parados por fonte
# Regra que faltava. Cada link_source tem cadencia propria; o que importa e
# se PAROU de crescer enquanto paginas continuam entrando. mentions e derivado
# de gazetteer e roda em passe separado, entao tolera mais tempo.
LINKSTALL=$(${=PSQL} "
  select string_agg(link_source || ' (' || round(extract(epoch from (now()-mx))/3600) || 'h)', ', ')
  from (select link_source, max(created_at) mx from links group by 1) s
  where (link_source = 'markdown'  and mx < now() - interval '12 hours')
     or (link_source = 'mentions'  and mx < now() - interval '36 hours');" 2>/dev/null)
[ -n "$LINKSTALL" ] && add links-parados "extracao de links parada: ${LINKSTALL}"

# --------------------------------------------- fila de jobs do gbrain
# Estado, nao janela. Conta o que esta morto AGORA, por tipo.
# 2026-09-04 (Fable): "estado" virou alarme permanente, porque nada limpa uma
# linha dead: 15 subagents mortos em 03/09 (Mac sem rede) foram reportados
# ate serem apagados na mao. Regra nova: conta so o que morreu nas ultimas
# 24h, e o proprio watchdog apaga linhas dead/cancelled com mais de 72h.
# NUNCA apagar linhas completed nem a fila dream-marker-optionA: a
# deduplicacao da sintese (dream:synth-v2:*) vive nessas linhas; apagar =
# re-sintetizar 666 transcripts na terra. Por isso `gbrain jobs prune` e proibido.
${=PSQL} "delete from minion_jobs where status in ('dead','cancelled')
  and updated_at < now() - interval '72 hours' and queue <> 'dream-marker-optionA';" >/dev/null 2>&1
DEAD=$(${=PSQL} "
  select string_agg(name || ' x' || n, ', ' order by n desc)
  from (select name, count(*) n from minion_jobs where status='dead'
        and updated_at > now() - interval '24 hours' group by 1) s;" 2>/dev/null)
[ -n "$DEAD" ] && add jobs-mortos "jobs mortos nas ultimas 24h: ${DEAD} (causa em error_text; subagent de padroes morre sem rede e o ciclo seguinte refaz, so retry se for sintese de transcript)"

# fila travada: um backlog grande e saudavel enquanto drena, e doente quando
# para de drenar. O sinal honesto e a IDADE do job mais velho na fila, nao o
# tamanho dela. O worker roda com concorrencia 1, entao um job longo (atoms
# drain, ate 10min) segura os curtos: 4h de tolerancia cobre isso com folga.
# updated_at, nao created_at: `gbrain jobs retry` preserva o created_at original,
# entao um lote reposto hoje parecia "24h na fila" e gerava alarme falso.
STUCK=$(${=PSQL} "
  select 'job mais velho na fila ha ' || round(extract(epoch from (now()-min(updated_at)))/3600) || 'h ('
         || count(*) || ' esperando)'
  from minion_jobs where status='waiting'
  having min(updated_at) < now() - interval '4 hours';" 2>/dev/null)
[ -n "$STUCK" ] && add fila-travada "$STUCK"

# nenhum job concluido em 1h com fila cheia = worker morto ou wedged
IDLE=$(${=PSQL} "
  select 'worker parado: 0 jobs concluidos em 1h com ' || (select count(*) from minion_jobs where status='waiting') || ' na fila'
  where (select count(*) from minion_jobs where status='waiting') > 5
    and (select count(*) from minion_jobs where status='completed' and finished_at > now() - interval '1 hour') = 0;" 2>/dev/null)
[ -n "$IDLE" ] && add worker-parado "$IDLE"

# extracao que "passa" mas nao produz nada. Olha SO o dia corrente: o rollup e
# cumulativo por dia, entao um dia ruim ja consertado ficaria gritando ate a
# virada. Colunas reais: halt_count / round_completed_count (nao halts/completed,
# que era o nome errado e fazia a checagem falhar em silencio).
# Condicionado a producao REAL, nao a round_completed_count: uma rodada pode
# halt no meio (um item barrado pelo content filter da Azure) e ainda assim ter
# escrito atoms. O sinal honesto e "halts acumulando E nada saindo".
HALT=$(${=PSQL} "
  select string_agg(kind || '/' || source_id || ' (' || halt_count || ' halts)', ', ')
  from extract_rollup_7d r
  where r.day = current_date and r.halt_count > 5 and r.round_completed_count = 0
    and r.updated_at > now() - interval '2 hours'
    and not exists (select 1 from links
                    where link_source = 'atom-provenance'
                      and created_at > now() - interval '2 hours');" 2>/dev/null)
[ -n "$HALT" ] && add extract-halt "extracao sem produzir: ${HALT}"

# ------------------------------------------- crons do Hermes: estado real
CRON=$(python3 - <<'PY' 2>/dev/null
import json, os, datetime
p = os.path.expanduser('~/.hermes/cron/jobs.json')
try: jobs = json.load(open(p))['jobs']
except Exception: raise SystemExit
bad, waiting = [], []
for j in jobs:
    if not j.get('enabled'): continue
    st = (j.get('last_status') or '').lower()
    if st and st != 'ok':
        bad.append(f"{j.get('name')} [{st}]")
    elif (j.get('failure_streak') or 0) > 0:
        bad.append(f"{j.get('name')} [falhou {j['failure_streak']}x]")
if bad: print("CRON:" + "; ".join(bad))
PY
)
[ -n "$CRON" ] && add cron-falhando "${CRON#CRON:}"

# ------------------------------ job esperando decisao do Gustavo ha muito tempo
# O gbrain-buildout ficou 10h30 parado esperando ele escolher o limiar de
# sintese e ninguem lembrou dele. Decisao pendente e um problema operacional.
WAIT=$(python3 - <<'PY' 2>/dev/null
import sqlite3, os, datetime
db = os.path.expanduser('~/.hermes/cron/notepad.db')
try: con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
except Exception: raise SystemExit
out = []
for job_id, key, value, upd in con.execute("select job_id,key,value,updated_at from cron_notepad"):
    try: import json; d = json.loads(value)
    except Exception: continue
    if not isinstance(d, dict): continue
    w = d.get('waiting_for')
    if not w: continue
    try:
        t = datetime.datetime.fromisoformat(upd)
        h = (datetime.datetime.now(t.tzinfo) - t).total_seconds() / 3600
    except Exception:
        h = 0
    if h >= 6:
        out.append(f"{job_id} ha {h:.0f}h: {str(w)[:90]}")
if out: print("; ".join(out))
PY
)
[ -n "$WAIT" ] && add decisao-pendente "esperando decisao sua: ${WAIT}"

# ------------------------------------------- erros do proxy Azure
# O access log do uvicorn so mostra status; o debug log mostra a causa. Sem
# isso o bug de max_tokens/temperature ficou 24h invisivel.
if [ -f /tmp/azure-proxy-debug.log ]; then
  PXERR=$(python3 - <<'PY' 2>/dev/null
import json, datetime
cut = datetime.datetime.now().astimezone() - datetime.timedelta(hours=24)
n = 0; sample = ""; errs = []
for line in open('/tmp/azure-proxy-debug.log', errors='ignore'):
    try: r = json.loads(line)
    except Exception: continue
    if r.get('kind') not in ('upstream_error', 'responses_failed'): continue
    # 429 e backpressure normal (o backfill de embeddings satura a cota da Azure
    # e o cliente reenvia). Contar isso como erro fazia o watchdog gritar durante
    # toda drenagem saudavel, que e exatamente como um alerta vira ruido ignorado.
    if r.get('status') == 429: continue
    try:
        if datetime.datetime.strptime(r['ts'], '%Y-%m-%dT%H:%M:%S%z') < cut: continue
    except Exception: pass
    n += 1
    if not sample: sample = str(r.get('upstream', ''))[:80]
    try: errs.append(datetime.datetime.strptime(r['ts'], '%Y-%m-%dT%H:%M:%S%z').timestamp())
    except Exception: pass
# 2026-09-04 (Fable): 411 erros "Errno 8 nodename nor servname" em 03/09 eram
# o Mac sem rede (bateria, tampa fechada), nao a Azure. Cruza cada erro com os
# buracos do heartbeat (~/.hermes/state/heartbeat.log, 1 batida/60 s): erro
# dentro de um buraco de >150 s (com folga de 3 min) conta como offline.
offline = 0
try:
    hb = [int(x) for x in open('/Users/gustavosouza/.hermes/state/heartbeat.log').read().split() if x.strip()]
    gaps = [(a - 180, b + 180) for a, b in zip(hb, hb[1:]) if b - a > 150]
    # netstate.log (since 2026-09-04): "<epoch> 0|1" per beat; 0 = awake but no
    # network (dark wake on battery), which the gap rule alone cannot see.
    offbeats = []
    try:
        for ln in open('/Users/gustavosouza/.hermes/state/netstate.log'):
            ts, st = ln.split()
            if st == '0': offbeats.append(int(ts))
    except Exception:
        pass
    for e in errs:
        if any(a <= e <= b for a, b in gaps) or any(abs(e - o) <= 120 for o in offbeats): offline += 1
except Exception:
    pass
online = n - offline
if online > 10: print(f"{online} erros do proxy Azure em 24h com o Mac online ({sample})")
elif n > 10: print(f"info: {n} erros do proxy Azure em 24h, {offline} deles com o Mac sem rede; nada a fazer")
PY
)
  case "$PXERR" in
    info:*) INFO+=("${PXERR#info: }") ;;
    "") ;;
    *) add proxy-erros "$PXERR" ;;
  esac
fi

# cota Azure
# 2026-09-04 (Fable): antes batia em admdatas-ai-gateway.azure-api.net (outro
# gateway, nao e o que o brain usa). Agora testa gus-foundry pelo proxy local,
# com o modelo mais barato e 5 tokens.
AZ=$(curl -s -m 30 -o /dev/null -w "%{http_code}" http://127.0.0.1:8011/v1/chat/completions -H "Authorization: Bearer gus-foundry-router" -H "Content-Type: application/json" -d '{"model":"gpt-5.6-luna","messages":[{"role":"user","content":"x"}],"max_tokens":5}')
[ "$AZ" = "403" ] && add azure-cota "cota Azure (gus-foundry) esgotada"
[ "$AZ" = "401" ] && add azure-chave "chave Azure (gus-foundry) invalida"

# ---------------------------------------------- watchdog v3 (2026-09-04)
# Blocos 4a, 4b e 6 do Gustavo: ledger vs fatura, Hermes na Azure, ciclos do
# sonho, sono e rede, busca degradada, reinicios do gateway. So no --daily
# (azcost leva ~10 s). Linhas ALARM viram problema; INFO vai no rodape.
if [ "$MODE" != "--check" ] && [ -f "$HOME/hermes/bin/watchdog_v3.py" ]; then
  while IFS= read -r line; do
    case "$line" in
      ALARM\ *)
        # v3 prose can contain uppercase, punctuation, and accents. Stable keys
        # crossing the intake boundary must remain in its strict ASCII alphabet.
        V3KEY=$(printf '%s' "${line#ALARM }" | cut -c1-24 | tr '[:upper:] ' '[:lower:]-' | tr -cd 'a-z0-9._-')
        [ -n "$V3KEY" ] || V3KEY="alarm"
        add "v3-${V3KEY}" "${line#ALARM }"
        ;;
      INFO\ *)  INFO+=("${line#INFO }") ;;
    esac
  done < <(python3 "$HOME/hermes/bin/watchdog_v3.py" 2>/dev/null)
fi

# ---------------------------------------------------------------- saida
KEYS=(); TEXTS=()
for e in "${P[@]}"; do KEYS+=("${e%%|*}"); TEXTS+=("${e#*|}"); done
INFOTXT=""; [ ${#INFO[@]} -gt 0 ] && INFOTXT=" Info: ${(j:; :)INFO}."

if [ ${#P[@]} -eq 0 ]; then
  MSG="Brain OK. ${PAGES} paginas, ${LINKS} links, ${STALE} chunks p/ embeddar. Servicos: ${(j:, :)OK}.${INFOTXT}"
else
  MSG="ATENCAO: ${(j:; :)TEXTS}. (paginas ${PAGES}, links ${LINKS})${INFOTXT}"
fi
echo "[$(date +%F\ %H:%M)] $MSG" >> "${WATCHDOG_LOG:-/tmp/watchdog.log}"

# diff contra o estado anterior
DIFF=$(python3 - "$STATE" "${(j:,:)KEYS}" <<'PY'
import json, sys
path, cur = sys.argv[1], [k for k in sys.argv[2].split(',') if k]
try: old = json.load(open(path)).get('active', [])
except Exception: old = []
new  = [k for k in cur if k not in old]
gone = [k for k in old if k not in cur]
print(json.dumps({'new': new, 'gone': gone}))
PY
)
NEW=$(echo "$DIFF"  | python3 -c "import json,sys; print(','.join(json.load(sys.stdin)['new']))")
GONE=$(echo "$DIFF" | python3 -c "import json,sys; print(','.join(json.load(sys.stdin)['gone']))")

if [ "$MODE" = "--check" ]; then
  # horario: so fala em transicao
  OUT=""
  [ -n "$NEW" ]  && OUT="NOVO: ${(j:; :)TEXTS}"
  [ -n "$GONE" ] && OUT="${OUT}${OUT:+
}RESOLVIDO: ${GONE}"
  # Submit unchanged/repeated snapshots too. Successful bridge intake is silent;
  # only a bridge failure invokes the automation-failure fallback.
  notify "${OUT:-$MSG}"
  exit 0
fi

# diario: manda sempre
[ -n "$GONE" ] && MSG="${MSG}
Resolvido desde ontem: ${GONE}."
notify "$MSG"
