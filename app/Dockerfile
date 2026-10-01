# syntax=docker/dockerfile:1
# nyx-api — imagem de exemplo para validação dos manifests Metacortex.
#
# Decisões alinhadas ao padrão (base/deployment.yaml):
#   - tag imutável 2.9.1 (regra 3.1 — NÃO usar :latest);
#   - registry interno registry.metacortex.io (regra 3.7);
#   - executa como UID 10001 não-root (regra 3.2 — runAsNonRoot);
#   - FS somente leitura em runtime: a app só escreve em /tmp e /var/cache/nyx,
#     montados como emptyDir pelo pod (readOnlyRootFilesystem: true).
#
# Base: python slim (oficial, enxuta, sem toolchain de build desnecessário).
FROM python:3.12-slim

# Tornar o Python determinístico: sem arquivos .pyc no FS read-only e
# buffer de stdout desligado para logs aparecerem imediatamente no kubectl logs.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Diretórios graváveis que o pod monta como emptyDir.
# Pré-criados e pertencentes ao UID 10001 para que o container não-root possa
# escrever mesmo antes do mount (defesa em profundidade).
RUN mkdir -p /tmp /var/cache/nyx /app

WORKDIR /app

# Copia apenas o servidor (sem dependências externas — só stdlib).
COPY server.py /app/server.py

# Usuário não-root fixo (UID/GID 10001), espelhando o securityContext do pod.
# Defesa em profundidade: mesmo que alguém rode a imagem fora do k8s, não roda
# como root.
RUN groupadd --system --gid 10001 nyx \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --no-create-home nyx

USER 10001:10001

# containerPort do deployment (documentação; não publica porta no host).
EXPOSE 8080

# Sem shell wrapper: processo 1 é o próprio Python (sinais SIGTERM diretos ->
# terminationGracePeriodSeconds: 60 funciona corretamente).
CMD ["python", "/app/server.py"]