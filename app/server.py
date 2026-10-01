"""nyx-api — aplicação de exemplo para validação dos manifests Metacortex.

Aplicação HTTP mínima construída SOMENTE com a biblioteca padrão do Python
(sem dependências externas), para que o Dockerfile permaneça simples e a imagem
enxuta.

Contrato que esta app satisfaz (definido em base/deployment.yaml e base/service.yaml):
  - escuta na porta 8080 (containerPort / targetPort do Service);
  - GET /healthz/ready  -> readiness probe (regra 2.2);
  - GET /healthz/live   -> liveness probe  (regra 2.2 — NÃO checa banco);
  - lê LOG_LEVEL e DB_HOST do ConfigMap (env);
  - lê DATABASE_URL do Secret (env) — nunca loga o valor, só sua presença (regra 3.3);
  - roda como UID 10001 não-root (securityContext do pod);
  - FS somente leitura -> escreve apenas em /tmp e /var/cache/nyx (emptyDir).
"""

import json
import logging
import os
import socket
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Configuração a partir de variáveis de ambiente injetadas pelo Kubernetes.
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("PORT", "8080"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "info").upper()
DB_HOST = os.environ.get("DB_HOST", "unset")
# DATABASE_URL vem do Secret via secretKeyRef. NUNCA logar o valor (regra 3.3).
DATABASE_URL = os.environ.get("DATABASE_URL")
# Diretórios graváveis montados como emptyDir (readOnlyRootFilesystem: true).
CACHE_DIR = os.environ.get("CACHE_DIR", "/var/cache/nyx")
TMP_DIR = os.environ.get("TMP_DIR", "/tmp")

LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}

logging.basicConfig(
    level=LEVELS.get(LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,  # readOnlyRootFilesystem: logs vão para stdout, não para arquivo
)
log = logging.getLogger("nyx-api")
log.info("iniciando nyx-api na porta %s — LOG_LEVEL=%s DB_HOST=%s", PORT, LOG_LEVEL, DB_HOST)
log.info("DATABASE_URL %s", "presente (Secret)" if DATABASE_URL else "AUSENTE")
log.info("cache dir=%s tmp dir=%s", CACHE_DIR, TMP_DIR)

# Pré-cria os diretórios graváveis caso ainda não existam (emptyDir vem vazio).
for d in (CACHE_DIR, TMP_DIR):
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as exc:
        log.warning("não foi possível criar %s: %s", d, exc)


# ---------------------------------------------------------------------------
# Handler HTTP.
# ---------------------------------------------------------------------------

CACHE_FILE = os.path.join(CACHE_DIR, "hits.json")


def _read_hits() -> int:
    """Lê contador de hits do cache (demonstra escrita no emptyDir)."""
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh).get("hits", 0)
    except (OSError, ValueError):
        return 0


def _write_hits(n: int) -> None:
    """Persiste contador de hits no cache (demonstra escrita no emptyDir)."""
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump({"hits": n}, fh)
    except OSError as exc:
        log.warning("falha ao escrever cache: %s", exc)


class Handler(BaseHTTPRequestHandler):
    server_version = "nyx-api/2.9.1"

    # Silencia logs verbosos do BaseHTTPRequestHandler (já logamos via logging).
    # Assinatura casa caractere por caractere com a da base (LSP): (self, format, *args).
    def log_message(self, format, *args):
        pass

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- Liveness: o processo está vivo? -------------------------------------
    # NÃO checa banco (regra 2.2): se o banco cair, reiniciar o container não
    # resolve e só adiciona instabilidade. Liveness = "o processo responde?".
    def do_GET_healthz_live(self):
        self._send_json(200, {
            "status": "ok",
            "check": "liveness",
            "timestamp": _now_iso(),
        })

    # -- Readiness: pronto para receber tráfego? -----------------------------
    # Aqui poderíamos checar dependências (ex.: ping no DB_HOST). Mantemos
    # simples: o processo está de pé e conseguindo servir -> pronto.
    def do_GET_healthz_ready(self):
        self._send_json(200, {
            "status": "ok",
            "check": "readiness",
            "db_host": DB_HOST,
            "database_url": "presente" if DATABASE_URL else "ausente",
            "timestamp": _now_iso(),
        })

    # -- Endpoint raiz: identidade + contagem de hits (demo de emptyDir) ----
    def do_GET_root(self):
        hits = _read_hits() + 1
        _write_hits(hits)
        self._send_json(200, {
            "app": "nyx-api",
            "version": "2.9.1",
            "pod": socket.gethostname(),
            "namespace": os.environ.get("NAMESPACE", "unknown"),
            "log_level": LOG_LEVEL,
            "db_host": DB_HOST,
            "hits": hits,
            "timestamp": _now_iso(),
        })

    def do_GET(self):
        routing = {
            "/": self.do_GET_root,
            "/healthz/live": self.do_GET_healthz_live,
            "/healthz/ready": self.do_GET_healthz_ready,
        }
        handler = routing.get(self.path.split("?", 1)[0])
        if handler is None:
            self._send_json(404, {"error": "not found", "path": self.path})
            return
        handler()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------

def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("escutando em 0.0.0.0:%s", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("encerrando (KeyboardInterrupt)")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()