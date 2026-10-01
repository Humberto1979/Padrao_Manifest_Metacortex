# Desafio 3 — Padrão de Manifests da Metacortex

Entrega do Desafio 3: um conjunto de manifests Kubernetes que **passam na revisão**
descrita em [`Desafio3.txt`](./Desafio3.txt) — o padrão interno de Plataforma mantido
por Segurança & Compliance da Metacortex.

A entrega usa **Kustomize** (base + overlays por ambiente), de forma que a mesma
aplicação (`nyx-api` do cliente `nyx`) é renderizada para `nyx-dev`, `nyx-stg` e
`nyx-prod` mudando apenas o overlay. Exemplos **negativos** (o que NÃO fazer)
aparecem como comentários `# ERRADO (...)` dentro dos próprios manifests, no estilo
do documento original.

Além dos manifests, o repositório traz uma **aplicação de exemplo** (`app/`) que
materializa a imagem referenciada (`registry.metacortex.io/nyx/api:2.9.1`) e um
**relatório de conformidade** (`report/index.html`) gerado a partir do estado vivo
de um cluster [kind](https://kind.sigs.k8s.io/) onde os manifests foram aplicados e
verificados em runtime.

## Estrutura

```
.
├── Desafio3.txt            # enunciado: padrão de manifests da Metacortex
├── README.md               # este arquivo
├── .gitignore
├── base/                   # base comum a todos os ambientes (não é aplicada isolada)
│   ├── kustomization.yaml
│   ├── serviceaccount.yaml
│   ├── configmap.yaml
│   ├── secret.yaml         # placeholder base64 — valor real via pipeline/sealed-secret
│   ├── deployment.yaml     # replicas: 1, sem strategy (defaults; prod sobrescreve)
│   └── service.yaml
├── overlays/
│   ├── dev/
│   │   ├── kustomization.yaml   # namespace: nyx-dev  + instance: nyx-dev
│   │   ├── namespace.yaml
│   │   └── patches/
│   │       └── configmap-dev.yaml   # DB_HOST -> pg.nyx-dev.svc
│   ├── stg/
│   │   ├── kustomization.yaml   # namespace: nyx-stg  + instance: nyx-stg
│   │   ├── namespace.yaml
│   │   └── patches/
│   │       └── configmap-stg.yaml   # DB_HOST -> pg.nyx-stg.svc
│   └── prod/
│       ├── kustomization.yaml   # namespace: nyx-prod + instance: nyx-prod
│       ├── namespace.yaml
│       ├── pdb.yaml             # PodDisruptionBudget (regra 2.5, só prod)
│       └── patches/
│           ├── deployment-prod.yaml   # replicas: 2 + RollingUpdate (regras 2.3/2.4)
│           └── configmap-prod.yaml    # DB_HOST -> pg.nyx-prod.svc (explícito)
├── app/                    # aplicação de exemplo (materializa a imagem dos manifests)
│   ├── server.py           # HTTP server stdlib-only: porta 8080 + probes /healthz/*
│   ├── Dockerfile          # python:3.12-slim, UID 10001 não-root, CMD direto
│   └── .dockerignore
├── tools/
│   └── render-report.py    # gerador do relatório (stdlib-only, sem dependências)
└── report/
    └── index.html          # relatório de conformidade (evidência de runtime)
```

## Aplicação de exemplo (`app/`)

Os manifests referenciam `registry.metacortex.io/nyx/api:2.9.1`. Para que a imagem
exista e seja reproduzível, `app/` traz uma aplicação HTTP mínima em Python
**somente com biblioteca padrão** (sem dependências externas), que satisfaz o
contrato esperado pelos manifests:

- escuta na porta **8080** (`containerPort` / `targetPort` do Service);
- `GET /healthz/ready` (readiness) e `GET /healthz/live` (liveness) — endpoints
  distintos (regra 2.2); a liveness não checa banco;
- lê `LOG_LEVEL` e `DB_HOST` do ConfigMap e `DATABASE_URL` do Secret (env);
- roda como UID **10001** não-root e escreve só em `/tmp` e `/var/cache/nyx`
  (compatível com `readOnlyRootFilesystem: true` + `emptyDir` + `fsGroup: 10001`).

O `Dockerfile` usa `python:3.12-slim`, cria o usuário não-root `nyx` (UID/GID 10001),
define `USER 10001:10001` e `CMD ["python", "/app/server.py"]` (processo 1 direto,
para que o `SIGTERM` do `terminationGracePeriodSeconds` chegue sem shell no meio).

```bash
# Construir a imagem com a tag imutável referenciada nos manifests:
docker build -t registry.metacortex.io/nyx/api:2.9.1 app/
```

## Como aplicar / validar

```bash
# Renderiza o manifesto final de cada ambiente (não aplica):
kubectl kustomize overlays/dev
kubectl kustomize overlays/stg
kubectl kustomize overlays/prod

# Dry-run de sintaxe (client):
kubectl apply --dry-run=client -k overlays/prod

# Dry-run contra o apiserver (precisa de cluster acessível):
kubectl apply --dry-run=server -k overlays/prod

# Aplicar um ambiente:
kubectl apply -k overlays/prod
```

> Se o `dry-run=server` reclamar que o namespace não existe, aplique só o
> namespace antes (`kubectl apply -f overlays/prod/namespace.yaml`) ou aplique
> tudo sem `dry-run` — o Namespace é criado junto.

## Validação em cluster (kind)

Os manifests foram aplicados e verificados em runtime num cluster
**kind** (`kindest/node:v1.37.0`, kubectl v1.37.1). Resumo do executado:

1. `kind create cluster --name metacortex`
2. `kind load docker-image registry.metacortex.io/nyx/api:2.9.1 --name metacortex`
3. `kubectl apply --dry-run=client -k overlays/{dev,stg,prod}` — validação client OK
4. `kubectl apply -k overlays/{dev,stg,prod}` — aplicação real
5. Verificação em runtime: pods, services, endpoints, PDB, securityContext, logs

Resultado observado:

| Ambiente | Pods | Réplicas | Service Endpoints | PDB |
|----------|------|----------|-------------------|-----|
| `nyx-dev` | 1/1 Running | 1 | `10.244.0.5:8080` | — |
| `nyx-stg` | 1/1 Running | 1 | `10.244.0.6:8080` | — |
| `nyx-prod` | 2/2 Running | 2 | `10.244.0.7:8080`, `10.244.0.8:8080` | `minAvailable: 1` |

Evidências de segurança confirmadas em runtime (dentro de um pod prod):

- `id` → `uid=10001(nyx) gid=10001(nyx)` (não-root);
- escrita em `/` bloqueada (`Read-only file system`) → `readOnlyRootFilesystem` efetivo;
- `/tmp` e `/var/cache/nyx` graváveis → `emptyDir` + `fsGroup: 10001` funcionando;
- `hostNetwork`/`hostPID`/`privileged` ausentes; `automountServiceAccountToken: false`;
- `DB_HOST` por ambiente: `pg.nyx-dev.svc` / `pg.nyx-stg.svc` / `pg.nyx-prod.svc`.

O relatório completo está em [`report/index.html`](./report/index.html) — arquivo
autossuficiente (CSS/JS embutidos, sem dependências externas) com 4 abas:
**Ambientes**, **Service + Endpoints**, **Conformidade** (22/22 regras OK) e
**YAML Renderizado**. Para regenerá-lo a partir do cluster vivo:

```bash
python3 tools/render-report.py
```

## Mapeamento de conformidade

Cada regra abaixo foi verificada **no manifest e em runtime** (cluster kind). A
coluna "Onde" aponta o arquivo; a validação empírica está no
`report/index.html` (aba Conformidade).

### Bloco 1 — Identidade e nomenclatura

| Regra | Status | Onde |
|-------|--------|------|
| 1.1 Nome em kebab-case | ✅ obrigatório | `nyx-api` em todos os objetos |
| 1.2 Namespace `<cliente>-<ambiente>` | ✅ obrigatório | `namespace:` em cada overlay → `nyx-dev`/`nyx-stg`/`nyx-prod` |
| 1.3 Quatro rótulos obrigatórios | ✅ obrigatório | `name`/`part-of`/`managed-by` na base + `instance` via `commonLabels` do overlay, em todos os objetos |
| 1.4 seletor casa com rótulos do pod | ✅ obrigatório | `service.yaml` selector e `deployment.yaml` matchLabels == pod template labels; confirmado em runtime: Endpoints não-vazios nos 3 ambientes |
| 1.5 Anotação de dono/runbook | ✅ recomendado | `metacortex.io/owner` + `metacortex.io/runbook` |
| 1.6 Container = nome do componente | ✅ recomendado | container `api` (não `app`/`main`) |

### Bloco 2 — Resiliência

| Regra | Status | Onde |
|-------|--------|------|
| 2.1 requests e limits | ✅ obrigatório | `base/deployment.yaml` container `api` (`100m/128Mi` req, `500m/512Mi` lim) |
| 2.2 readiness + liveness (endpoints distintos) | ✅ obrigatório | `/healthz/ready` e `/healthz/live` (liveness não checa banco); ambos 200 em runtime |
| 2.3 replicas >= 2 em prod | ✅ obrigatório | patch `overlays/prod/patches/deployment-prod.yaml` (`replicas: 2`); dev/stg = 1 (permitido) |
| 2.4 Estratégia de atualização | ✅ obrigatório em prod | mesmo patch: `RollingUpdate`, `maxUnavailable: 0`, `maxSurge: 1` |
| 2.5 PodDisruptionBudget | ✅ recomendado | `overlays/prod/pdb.yaml` `minAvailable: 1` (só prod) |
| 2.6 terminationGracePeriodSeconds | ✅ recomendado | `60s` no pod spec da base |

### Bloco 3 — Segurança

| Regra | Status | Onde |
|-------|--------|------|
| 3.1 Sem `:latest` | ✅ proibido → respeitado | `image: registry.metacortex.io/nyx/api:2.9.1` (tag imutável) |
| 3.2 securityContext completo | ✅ obrigatório | `runAsNonRoot`, `runAsUser: 10001`, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem`, `drop: ["ALL"]` + `emptyDir` em `/tmp` e `/var/cache/nyx`; UID 10001 e FS read-only confirmados em runtime |
| 3.3 Sem segredo em texto puro | ✅ proibido → respeitado | `DATABASE_URL` via `secretKeyRef`; Secret só placeholder base64. Exemplos negativos `# ERRADO` usam o placeholder `<SENHA-EM-TEXTO-PURO>` (a regra proíbe valor sensível "nem em comentário" — ver nota abaixo) |
| 3.4 automountServiceAccountToken: false | ✅ obrigatório | `base/serviceaccount.yaml` e pod spec; `automount=false` em runtime |
| 3.5 ServiceAccount dedicada | ✅ recomendado | `nyx-api` (não `default`); nenhum RBAC (app não fala com a API) |
| 3.6 Sem hostNetwork/hostPID/privileged | ✅ proibido → respeitado | ausentes; `hostNetwork=false`/`hostPID=false`/`privileged=false` em runtime |
| 3.7 Imagem de registry interno | ✅ obrigatório | `registry.metacortex.io` |

> **Nota sobre a regra 3.3**: a regra proíbe valor sensível "nem em `env.value`,
> nem em `ConfigMap`, **nem em comentário**". Os exemplos didáticos `# ERRADO` em
> `base/deployment.yaml` e `base/configmap.yaml` usam o placeholder
> `<SENHA-EM-TEXTO-PURO>` em vez de uma string com aparência de credencial, justamente
> para não versionar nenhum texto que se pareça com um segredo real — mesmo sendo um
> exemplo negativo. A decisão está documentada em uma `NOTA DIDATICA` junto ao exemplo.

### Bloco 4 — Vocabulário

Atendido por construção: Deployment (4.3) governa ReplicaSets que governam Pods
(4.1/4.2) que contêm containers; Service (4.4) com `port` != `targetPort` (4.5);
ConfigMap para não-sensível e Secret para sensível (4.7); probes (4.8) com
`readinessProbe` e `livenessProbe` distintas. Hierarquia
Deployment→ReplicaSet→Pod confirmada em runtime nos três ambientes.

### Exceções

Nenhuma exceção foi solicitada nem é necessária: todas as regras **obrigatórias** e
**recomendadas** foram atendidas. Não existe exceção a regra **proibida** para
workload de cliente.

## Decisões de design

- **Base não é aplicável isolada**: falta o rótulo `instance` e o `namespace`,
  injetados pelo overlay. É o modelo esperado do Kustomize — sempre aplique via
  overlay (`kubectl apply -k overlays/<env>`).
- **`commonLabels` adiciona `instance` aos seletores** do Service e do
  Deployment. Como também adiciona ao pod template, o casamento (regra 1.4)
  permanece válido caractere por caractere. (Aviso: `commonLabels` é depreciado
  no Kustomize v5.x em favor de `labels`; funciona corretamente e a migração é
  uma evolução futura.)
- **`readOnlyRootFilesystem: true`** exigiu montar `emptyDir` em `/tmp` e
  `/var/cache/nyx`. O `fsGroup: 10001` no pod `securityContext` torna esses
  volumes graváveis pelo UID 10001 — verificado em runtime.
- **Probes distintas**: liveness não checa banco, para não reiniciar a aplicação
  inteira quando o banco fica lento (regra 2.2).
- **Secret**: `base/secret.yaml` contém apenas placeholder base64
  (`postgres://nyx:CHANGE-ME@...`). Em produção o valor real é injetado pelo
  pipeline/sealed-secret — nunca commitado no Git (regra 3.3). Secret é base64,
  não criptografia.
- **`DB_HOST` por overlay**: cada ambiente patcheia (`pg.nyx-dev.svc` /
  `pg.nyx-stg.svc` / `pg.nyx-prod.svc`) via strategic merge, preservando
  `LOG_LEVEL` do base. Confirmado em runtime (ConfigMap distinto por namespace).
- **Aplicação stdlib-only**: `app/server.py` usa só a biblioteca padrão do Python,
  para que o `Dockerfile` não precise de `pip install` — imagem enxuta e sem
  dependências de terceiros para auditar.
- **`CMD` sem shell**: o processo 1 é o próprio Python, para que o `SIGTERM` do
  `terminationGracePeriodSeconds: 60` chegue direto à aplicação.

## Escopo

- A entrega cobre o workload principal: **Deployment + Service** (com
  ServiceAccount, ConfigMap, Secret e, em prod, PodDisruptionBudget). Não foram
  incluídos `Job`/`CronJob` — a regra 1.3 os cita como exemplos de tipos de
  objeto que devem levar os 4 rótulos, não como objetos obrigatórios; o `nyx-api`
  é um serviço de longa duração, não uma carga batch.
- A imagem `registry.metacortex.io/nyx/api:2.9.1` é materializada localmente por
  `app/` e carregada no cluster kind via `kind load docker-image` (não há push
  para um registry real — `registry.metacortex.io` é fictício, e
  `imagePullPolicy: IfNotPresent` faz o kubelet usar a imagem já presente no nó).