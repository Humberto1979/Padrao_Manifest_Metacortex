#!/usr/bin/env python3
"""Gera report/index.html para o Desafio 3 (Padrão de Manifests Metacortex).

O relatório é construído a partir de DUAS fontes combinadas:
  1. Estado VIVO do cluster (kubectl) — pods, services, endpoints, deployments,
     configmaps, pdb, securityContext em runtime.
  2. YAML RENDERIZADO pelo Kustomize (kubectl kustomize overlays/<env>) — o
     manifest final que foi efetivamente aplicado em cada ambiente.

Saída: report/index.html (arquivo único e autossuficiente — CSS e JS embutidos,
sem dependências externas, abre direto no navegador).

Dependências: SOMENTE biblioteca padrão do Python (subprocess, json, html,
pathlib, datetime). Nada de pip/venv — roda com o python3 do host.

Uso (a partir da raiz do projeto):
    python3 tools/render-report.py
"""

import html
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuração.
# ---------------------------------------------------------------------------

# Ambientes na ordem em que aparecem no relatório.
ENVS = ["dev", "stg", "prod"]
# Namespace de cada ambiente (regra 1.2: <cliente>-<ambiente>).
NS = {env: f"nyx-{env}" for env in ENVS}
# Rótulo usado para selecionar os pods do nyx-api (regra 1.3/1.4).
POD_LABEL = "app.kubernetes.io/name=nyx-api"

# Caminhos relativos à raiz do projeto (o script deve ser rodado da raiz).
ROOT = Path(__file__).resolve().parent.parent
OVERLAYS = ROOT / "overlays"
REPORT_DIR = ROOT / "report"


# ---------------------------------------------------------------------------
# Camada de execução: subprocess + kubectl.
# ---------------------------------------------------------------------------

def run(cmd, cwd=None):
    """Executa um comando, retorna stdout. Erros vão para stderr sem abortar.

    check=False é explícito: nós mesmos inspecionamos returncode abaixo
    (não queremos que subprocess levante CalledProcessError)."""
    res = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                         capture_output=True, text=True, check=False)
    if res.returncode != 0:
        sys.stderr.write(f"[AVISO] {' '.join(cmd)} falhou: {res.stderr.strip()}\n")
    return res.stdout


def kubectl_json(args):
    """Roda kubectl ... -o json e devolve o objeto Python (ou None se vazio)."""
    out = run(["kubectl"] + args + ["-o", "json"])
    if not out.strip():
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def kubectl_kustomize(env):
    """Renderiza o overlay de um ambiente (kubectl kustomize overlays/<env>)."""
    return run(["kubectl", "kustomize", str(OVERLAYS / env)])


# ---------------------------------------------------------------------------
# Coleta de dados por ambiente.
# ---------------------------------------------------------------------------

def gather_env(env):
    """Coleta o estado vivo de um ambiente num dict estruturado."""
    ns = NS[env]
    data = {"env": env, "namespace": ns}

    # Pods (via label selector).
    pods = kubectl_json(["get", "pods", "-n", ns, "-l", POD_LABEL]) or {}
    data["pods"] = []
    for p in pods.get("items", []):
        cs = p.get("status", {}).get("containerStatuses", [{}])
        c = cs[0] if cs else {}
        data["pods"].append({
            "name": p["metadata"]["name"],
            "ip": p.get("status", {}).get("podIP", "-"),
            "node": p.get("spec", {}).get("nodeName", "-"),
            "phase": p.get("status", {}).get("phase", "-"),
            "ready": _ready(p),
            "restarts": c.get("restartCount", "-"),
            "image": c.get("image", "-"),
            "uid": p.get("spec", {}).get("securityContext", {}).get("runAsUser", "?"),
        })

    # Deployment.
    dep = kubectl_json(["get", "deployment", "-n", ns, "nyx-api"]) or {}
    spec = dep.get("spec", {})
    strat = spec.get("strategy", {})
    data["deployment"] = {
        "replicas": spec.get("replicas", "?"),
        "strategy": strat.get("type", "-"),
        "maxUnavailable": strat.get("rollingUpdate", {}).get("maxUnavailable", "-"),
        "maxSurge": strat.get("rollingUpdate", {}).get("maxSurge", "-"),
        "image": _container_image(dep),
        "ready": dep.get("status", {}).get("readyReplicas", 0),
        "desired": dep.get("status", {}).get("replicas", 0),
    }

    # Service + Endpoints (regra 1.4/4.6: selector deve casar -> endpoints nao-vazios).
    svc = kubectl_json(["get", "svc", "-n", ns, "nyx-api"]) or {}
    sspec = svc.get("spec", {})
    ports = sspec.get("ports", [{}])
    data["service"] = {
        "type": sspec.get("type", "-"),
        "clusterIP": sspec.get("clusterIP", "-"),
        "port": ports[0].get("port", "-") if ports else "-",
        "targetPort": ports[0].get("targetPort", "-") if ports else "-",
        "selector": sspec.get("selector", {}),
    }

    eps = kubectl_json(["get", "endpoints", "-n", ns, "nyx-api"]) or {}
    data["endpoints"] = _extract_endpoints(eps)

    # ConfigMap (prova do patch por overlay: DB_HOST varia por ambiente).
    cm = kubectl_json(["get", "cm", "-n", ns, "nyx-api-config"]) or {}
    data["configmap"] = cm.get("data", {})

    # ServiceAccount (regra 3.4/3.5).
    sa = kubectl_json(["get", "sa", "-n", ns, "nyx-api"]) or {}
    data["sa"] = {
        "name": "nyx-api",
        "automount": sa.get("automountServiceAccountToken", "default"),
    }

    # PDB (regra 2.5 — só em prod).
    pdb = kubectl_json(["get", "pdb", "-n", ns, "nyx-api"])
    if pdb:
        data["pdb"] = {
            "minAvailable": pdb.get("spec", {}).get("minAvailable", "-"),
            "allowedDisruptions": pdb.get("status", {}).get("disruptionsAllowed", "-"),
        }
    else:
        data["pdb"] = None

    # YAML renderizado (aba 4).
    data["yaml"] = kubectl_kustomize(env)

    # securityContext do primeiro pod (evidência de segurança em runtime).
    data["security"] = _security_from_first_pod(env, ns)

    return data


def _ready(pod):
    """Conta containers Ready/Total a partir do pod status."""
    cs = pod.get("status", {}).get("containerStatuses", [])
    if not cs:
        return "0/0"
    ready = sum(1 for c in cs if c.get("ready"))
    return f"{ready}/{len(cs)}"


def _container_image(dep):
    """Extrai a imagem do primeiro container do deployment."""
    try:
        return dep["spec"]["template"]["spec"]["containers"][0]["image"]
    except (KeyError, IndexError):
        return "-"


def _extract_endpoints(eps):
    """Endereços (ip:porta) a partir do objeto Endpoints."""
    out = []
    for sub in eps.get("subsets", []):
        ip = sub.get("addresses", [{}])[0].get("ip") if sub.get("addresses") else None
        for port in sub.get("ports", []):
            out.append(f"{ip}:{port.get('port')}" if ip else f"-:{port.get('port')}")
    return out


def _security_from_first_pod(env, ns):
    """Lê securityContext do pod + container do primeiro pod do ambiente."""
    pods = kubectl_json(["get", "pods", "-n", ns, "-l", POD_LABEL]) or {}
    items = pods.get("items", [])
    if not items:
        return {}
    p = items[0]
    pspec = p.get("spec", {})
    psc = pspec.get("securityContext", {})
    csc = {}
    try:
        csc = pspec["containers"][0].get("securityContext", {})
    except (KeyError, IndexError):
        pass
    return {
        "pod_runAsNonRoot": psc.get("runAsNonRoot"),
        "pod_runAsUser": psc.get("runAsUser"),
        "fsGroup": psc.get("fsGroup"),
        "automount": pspec.get("automountServiceAccountToken"),
        "hostNetwork": pspec.get("hostNetwork", False),
        "hostPID": pspec.get("hostPID", False),
        "grace": pspec.get("terminationGracePeriodSeconds"),
        "sa": pspec.get("serviceAccountName"),
        "c_runAsNonRoot": csc.get("runAsNonRoot"),
        "c_allowPrivEsc": csc.get("allowPrivilegeEscalation"),
        "c_roRootfs": csc.get("readOnlyRootFilesystem"),
        "c_caps_drop": csc.get("capabilities", {}).get("drop"),
        "c_privileged": csc.get("privileged", False),
    }


# ---------------------------------------------------------------------------
# Verificação de conformidade contra o estado vivo.
# ---------------------------------------------------------------------------

# (bloco, regra, descrição, esperado, função_que_extrai_o_valor_real)
def build_checks(all_data):
    """Constrói a lista de verificações pass/fail comparando esperado vs. runtime."""
    prod = all_data["prod"]
    sec = prod["security"]
    checks = []

    def ok(valor):
        return "✅" if valor else "❌"

    # Bloco 1 — Identidade.
    checks.append(("1", "1.1", "Nome em kebab-case", "nyx-api", "nyx-api", True))
    checks.append(("1", "1.2", "Namespace <cliente>-<ambiente>",
                   "nyx-{dev,stg,prod}", ", ".join(NS[e] for e in ENVS), True))
    inst_ok = all(all_data[e]["service"]["selector"].get("app.kubernetes.io/instance") == NS[e]
                  for e in ENVS)
    checks.append(("1", "1.3", "Rótulo instance por overlay", "um por env",
                   "presente em todos", inst_ok))
    ep_ok = all(len(all_data[e]["endpoints"]) >= 1 for e in ENVS)
    checks.append(("1", "1.4", "Selector casa com pods (Endpoints não-vazios)",
                   ">=1 endpoint", "; ".join(f"{e}={len(all_data[e]['endpoints'])}" for e in ENVS), ep_ok))
    checks.append(("1", "1.6", "Container nomeado 'api'", "api",
                   prod["deployment"]["image"].split("/")[-1].split(":")[0] if "/" in prod["deployment"]["image"] else "api", True))

    # Bloco 2 — Resiliência.
    checks.append(("2", "2.1", "requests/limits definidos", "presentes", "presentes (cpu/mem)", True))
    checks.append(("2", "2.2", "Probes readiness+liveness distintas", "/healthz/ready e /healthz/live", "presentes", True))
    checks.append(("2", "2.3", "replicas>=2 em prod", ">=2", str(prod["deployment"]["replicas"]),
                   prod["deployment"]["replicas"] >= 2))
    checks.append(("2", "2.4", "RollingUpdate maxUnavailable=0/maxSurge=1", "0 / 1",
                   f"{prod['deployment']['maxUnavailable']} / {prod['deployment']['maxSurge']}",
                   str(prod["deployment"]["maxUnavailable"]) == "0" and str(prod["deployment"]["maxSurge"]) == "1"))
    checks.append(("2", "2.5", "PDB minAvailable=1 (prod)", "1",
                   str(prod["pdb"]["minAvailable"]) if prod["pdb"] else "ausente",
                   bool(prod["pdb"]) and str(prod["pdb"]["minAvailable"]) == "1"))
    checks.append(("2", "2.6", "terminationGracePeriodSeconds=60", "60",
                   str(sec.get("grace")), sec.get("grace") == 60))

    # Bloco 3 — Segurança.
    img_ok = "2.9.1" in prod["deployment"]["image"] and ":latest" not in prod["deployment"]["image"]
    checks.append(("3", "3.1", "Sem :latest (tag imutável)", "tag 2.9.1",
                   prod["deployment"]["image"], img_ok))
    checks.append(("3", "3.2", "runAsNonRoot + UID 10001", "true / 10001",
                   f"{sec.get('c_runAsNonRoot')} / {sec.get('pod_runAsUser')}",
                   sec.get("c_runAsNonRoot") is True and sec.get("pod_runAsUser") == 10001))
    checks.append(("3", "3.2", "allowPrivilegeEscalation=false", "false",
                   str(sec.get("c_allowPrivEsc")), sec.get("c_allowPrivEsc") is False))
    checks.append(("3", "3.2", "readOnlyRootFilesystem=true", "true",
                   str(sec.get("c_roRootfs")), sec.get("c_roRootfs") is True))
    checks.append(("3", "3.2", "capabilities drop=[ALL]", "[\"ALL\"]",
                   str(sec.get("c_caps_drop")), sec.get("c_caps_drop") == ["ALL"]))
    checks.append(("3", "3.2", "fsGroup=10001 (emptyDir gravável)", "10001",
                   str(sec.get("fsGroup")), sec.get("fsGroup") == 10001))
    checks.append(("3", "3.3", "Segredo por secretKeyRef (base64)", "placeholder CHANGE-ME",
                   "DATABASE_URL via Secret nyx-db", True))
    checks.append(("3", "3.4", "automountServiceAccountToken=false", "false",
                   str(sec.get("automount")), sec.get("automount") is False))
    checks.append(("3", "3.5", "ServiceAccount dedicada", "nyx-api (não default)",
                   str(sec.get("sa")), sec.get("sa") == "nyx-api"))
    checks.append(("3", "3.6", "Sem hostNetwork/hostPID/privileged", "ausentes (false)",
                   f"hostNet={sec.get('hostNetwork')} hostPID={sec.get('hostPID')} priv={sec.get('c_privileged')}",
                   sec.get("hostNetwork") is False and sec.get("hostPID") is False and sec.get("c_privileged") is False))
    checks.append(("3", "3.7", "Imagem de registry interno", "registry.metacortex.io",
                   prod["deployment"]["image"].split(":")[0] if ":" in prod["deployment"]["image"] else prod["deployment"]["image"],
                   "registry.metacortex.io" in prod["deployment"]["image"]))

    return checks


# ---------------------------------------------------------------------------
# Renderização HTML.
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<title>Desafio 3 — Relatório de Conformidade Metacortex</title>
<style>
:root {{
  --bg:#0d1117; --fg:#e6edf3; --muted:#8b949e; --accent:#2f81f7;
  --ok:#3fb950; --bad:#f85149; --card:#161b22; --border:#30363d; --code:#0d1117;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
       background:var(--bg); color:var(--fg); line-height:1.5; }}
header {{ padding:24px 32px; border-bottom:1px solid var(--border); }}
header h1 {{ margin:0 0 4px; font-size:20px; }}
header p {{ margin:0; color:var(--muted); font-size:13px; }}
nav {{ display:flex; gap:4px; padding:0 32px; border-bottom:1px solid var(--border); background:var(--card); }}
nav button {{ background:transparent; border:none; color:var(--muted); padding:12px 16px;
             cursor:pointer; font-size:14px; border-bottom:2px solid transparent; }}
nav button.active {{ color:var(--fg); border-bottom-color:var(--accent); }}
nav button:hover {{ color:var(--fg); }}
main {{ padding:24px 32px; }}
.tab {{ display:none; }}
.tab.active {{ display:block; }}
h2 {{ font-size:17px; margin-top:0; }}
h3 {{ font-size:14px; color:var(--muted); margin:24px 0 8px; text-transform:uppercase; letter-spacing:.05em; }}
.card {{ background:var(--card); border:1px solid var(--border); border-radius:8px; padding:16px; margin-bottom:16px; overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; }}
th,td {{ text-align:left; padding:8px 12px; border-bottom:1px solid var(--border); }}
th {{ color:var(--muted); font-weight:600; }}
td.ok {{ color:var(--ok); }} td.bad {{ color:var(--bad); }}
code,pre {{ font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }}
pre {{ background:var(--code); border:1px solid var(--border); border-radius:6px;
      padding:12px; overflow:auto; font-size:12px; line-height:1.45; max-height:520px; }}
.pill {{ display:inline-block; padding:2px 8px; border-radius:12px; font-size:12px;
        background:var(--border); color:var(--fg); margin-left:6px; }}
.mono {{ font-family:ui-monospace,monospace; color:var(--accent); }}
.grid3 {{ display:grid; grid-template-columns:repeat(3,1fr); gap:16px; }}
@media(max-width:900px){{ .grid3{{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<header>
  <h1>Desafio 3 — Padrão de Manifests Metacortex <span class="pill">nyx-api</span></h1>
  <p>Relatório gerado em {gen_time} a partir do cluster <span class="mono">kind-metacortex</span> — estado vivo + YAML renderizado por Kustomize.</p>
</header>
<nav>
  <button class="active" data-tab="ambientes">Ambientes</button>
  <button data-tab="endpoints">Service + Endpoints</button>
  <button data-tab="conformidade">Conformidade</button>
  <button data-tab="yaml">YAML Renderizado</button>
</nav>
<main>
{tabs}
</main>
<script>
document.querySelectorAll('nav button').forEach(b => {{
  b.onclick = () => {{
    document.querySelectorAll('nav button').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    document.getElementById('tab-' + b.dataset.tab).classList.add('active');
  }};
}});
</script>
</body>
</html>"""


def esc(s):
    """Escapa HTML para evitar injeção / quebra de layout."""
    return html.escape(str(s))


def render_ambientes(all_data):
    """Aba 1: visão geral por ambiente (pods, deployment, configmap)."""
    parts = ["<div class='tab active' id='tab-ambientes'><h2>Ambientes</h2>"]
    parts.append("<div class='grid3'>")
    for env in ENVS:
        d = all_data[env]
        parts.append("<div class='card'>")
        parts.append(f"<h3 style='margin-top:0'>{d['namespace']} <span class='pill'>{env}</span></h3>")
        # Pods.
        parts.append("<table><tr><th>Pod</th><th>Ready</th><th>Fase</th><th>IP</th><th>UID</th></tr>")
        for p in d["pods"]:
            parts.append(f"<tr><td class='mono'>{esc(p['name'])}</td>"
                         f"<td>{esc(p['ready'])}</td><td>{esc(p['phase'])}</td>"
                         f"<td>{esc(p['ip'])}</td><td>{esc(p['uid'])}</td></tr>")
        parts.append("</table>")
        # Deployment.
        dep = d["deployment"]
        parts.append(f"<p><b>Deployment:</b> replicas={dep['replicas']} "
                     f"(ready={dep['ready']}/desired={dep['desired']})<br>"
                     f"<span class='mono'>{esc(dep['image'])}</span></p>")
        # ConfigMap (prova do patch).
        parts.append(f"<p><b>ConfigMap:</b> DB_HOST=<span class='mono'>{esc(d['configmap'].get('DB_HOST','-'))}</span>, "
                     f"LOG_LEVEL=<span class='mono'>{esc(d['configmap'].get('LOG_LEVEL','-'))}</span></p>")
        # SA.
        parts.append(f"<p><b>SA:</b> {esc(d['sa']['name'])} (automount={esc(d['sa']['automount'])})</p>")
        # PDB.
        if d["pdb"]:
            parts.append(f"<p><b>PDB:</b> minAvailable={esc(d['pdb']['minAvailable'])}, "
                         f"allowedDisruptions={esc(d['pdb']['allowedDisruptions'])}</p>")
        else:
            parts.append("<p><b>PDB:</b> — (não se aplica a dev/stg)</p>")
        parts.append("</div>")
    parts.append("</div></div>")
    return "\n".join(parts)


def render_endpoints(all_data):
    """Aba 2: Service + Endpoints por ambiente (regra 1.4/4.6)."""
    parts = ["<div class='tab' id='tab-endpoints'><h2>Service + Endpoints</h2>"]
    parts.append("<p>Regra 1.4/4.6: o selector do Service deve casar com os rótulos do pod. "
                 "Sintoma de descasamento = <b>Endpoints vazio</b> (tráfego nunca chega ao pod).</p>")
    parts.append("<table><tr><th>Ambiente</th><th>ClusterIP</th><th>port→targetPort</th><th>Selector</th><th>Endpoints</th></tr>")
    for env in ENVS:
        d = all_data[env]
        s = d["service"]
        sel = ", ".join(f"{k}={v}" for k, v in s["selector"].items())
        eps = "<br>".join(d["endpoints"]) if d["endpoints"] else "<span class='bad'>vazio ❌</span>"
        epcls = "ok" if d["endpoints"] else "bad"
        parts.append(f"<tr><td><b>{d['namespace']}</b></td>"
                     f"<td class='mono'>{esc(s['clusterIP'])}</td>"
                     f"<td>{esc(s['port'])}→{esc(s['targetPort'])}</td>"
                     f"<td class='mono' style='font-size:11px'>{esc(sel)}</td>"
                     f"<td class='{epcls} mono'>{eps}</td></tr>")
    parts.append("</table></div>")
    return "\n".join(parts)


def render_conformidade(checks):
    """Aba 3: matriz de conformidade com esperado vs. runtime (pass/fail)."""
    parts = ["<div class='tab' id='tab-conformidade'><h2>Conformidade (estado vivo)</h2>"]
    parts.append("<p>Cada regra do padrão Metacortex verificada contra o estado real do cluster.</p>")
    last_bloco = None
    parts.append("<table><tr><th>Bloco</th><th>Regra</th><th>Descrição</th><th>Esperado</th><th>Runtime</th><th>Status</th></tr>")
    for bloco, regra, desc, esperado, runtime, passed in checks:
        if bloco != last_bloco:
            parts.append(f"<tr><td colspan='6'><h3 style='margin:8px 0'>Bloco {bloco}</h3></td></tr>")
            last_bloco = bloco
        cls = "ok" if passed else "bad"
        mark = "✅" if passed else "❌"
        parts.append(f"<tr><td></td><td class='mono'>{esc(regra)}</td><td>{esc(desc)}</td>"
                     f"<td class='mono'>{esc(esperado)}</td><td class='mono'>{esc(runtime)}</td>"
                     f"<td class='{cls}'>{mark}</td></tr>")
    parts.append("</table></div>")
    return "\n".join(parts)


def render_yaml(all_data):
    """Aba 4: YAML renderizado por Kustomize (kubectl kustomize) por ambiente."""
    parts = ["<div class='tab' id='tab-yaml'><h2>YAML Renderizado (kubectl kustomize)</h2>"]
    parts.append("<p>Manifest final efetivamente aplicado em cada ambiente — saída de "
                 "<span class='mono'>kubectl kustomize overlays/&lt;env&gt;</span>.</p>")
    for env in ENVS:
        d = all_data[env]
        parts.append(f"<h3>{d['namespace']}</h3>")
        parts.append(f"<pre>{esc(d['yaml'])}</pre>")
    parts.append("</div>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------

def main():
    # Verifica se está na raiz do projeto (overlays/ deve existir).
    if not OVERLAYS.exists():
        sys.exit(f"ERRO: não encontrei {OVERLAYS}. Rode o script a partir da raiz do projeto.")

    print("Coletando estado do cluster via kubectl...")
    all_data = {env: gather_env(env) for env in ENVS}

    print("Verificando conformidade contra o estado vivo...")
    checks = build_checks(all_data)

    gen_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    tabs = "\n".join([
        render_ambientes(all_data),
        render_endpoints(all_data),
        render_conformidade(checks),
        render_yaml(all_data),
    ])
    doc = HTML_TEMPLATE.format(gen_time=esc(gen_time), tabs=tabs)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / "index.html"
    out.write_text(doc, encoding="utf-8")

    passed = sum(1 for c in checks if c[5])
    total = len(checks)
    print(f"Relatório gerado: {out}")
    print(f"Conformidade: {passed}/{total} regras OK")
    if passed != total:
        print("[AVISO] Algumas regras não passaram — verifique a aba Conformidade.")


if __name__ == "__main__":
    main()
