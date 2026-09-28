# Desafio 3 — Padrão de Manifests da Metacortex

Entrega do Desafio 3: um conjunto de manifests Kubernetes que **passam na revisão**
descrita em [`Desafio3.txt`](./Desafio3.txt) — o padrão interno de Plataforma mantido
por Segurança & Compliance da Metacortex.

A entrega usa **Kustomize** (base + overlays por ambiente), de forma que a mesma
aplicação (`nyx-api` do cliente `nyx`) é renderizada para `nyx-dev`, `nyx-stg` e
`nyx-prod` mudando apenas o overlay. Exemplos **negativos** (o que NÃO fazer)
aparecem como comentários `# ERRADO (...)` dentro dos próprios manifests, no estilo
do documento original.

## Estrutura

```
.
├── Desafio3.txt            # enunciado: padrão de manifests da Metacortex
├── README.md               # este arquivo
├── base/                   # base comum a todos os ambientes (não é aplicada isolada)
│   ├── kustomization.yaml
│   ├── serviceaccount.yaml
│   ├── configmap.yaml
│   ├── secret.yaml         # placeholder base64 — valor real via pipeline/sealed-secret
│   ├── deployment.yaml     # replicas: 1, sem strategy (defaults; prod sobrescreve)
│   └── service.yaml
└── overlays/
    ├── dev/
    │   ├── kustomization.yaml   # namespace: nyx-dev  + instance: nyx-dev
    │   ├── namespace.yaml
    │   └── patches/
    │       └── configmap-dev.yaml   # DB_HOST -> pg.nyx-dev.svc
    ├── stg/
    │   ├── kustomization.yaml   # namespace: nyx-stg  + instance: nyx-stg
    │   ├── namespace.yaml
    │   └── patches/
    │       └── configmap-stg.yaml   # DB_HOST -> pg.nyx-stg.svc
    └── prod/
        ├── kustomization.yaml   # namespace: nyx-prod + instance: nyx-prod
        ├── namespace.yaml
        ├── pdb.yaml             # PodDisruptionBudget (regra 2.5, só prod)
        └── patches/
            ├── deployment-prod.yaml   # replicas: 2 + RollingUpdate (regras 2.3/2.4)
            └── configmap-prod.yaml    # DB_HOST -> pg.nyx-prod.svc (explícito)
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

## Mapeamento de conformidade

### Bloco 1 — Identidade e nomenclatura

| Regra | Status | Onde |
|-------|--------|------|
| 1.1 Nome em kebab-case | ✅ obrigatório | `nyx-api` em todos os objetos |
| 1.2 Namespace `<cliente>-<ambiente>` | ✅ obrigatório | `namespace:` em cada overlay → `nyx-dev`/`nyx-stg`/`nyx-prod` |
| 1.3 Quatro rótulos obrigatórios | ✅ obrigatório | `name`/`part-of`/`managed-by` na base + `instance` via `commonLabels` do overlay, em todos os objetos |
| 1.4 seletor casa com rótulos do pod | ✅ obrigatório | `service.yaml` selector e `deployment.yaml` matchLabels == pod template labels; `commonLabels` adiciona `instance` igualmente a selector e pod template |
| 1.5 Anotação de dono/runbook | ✅ recomendado | `metacortex.io/owner` + `metacortex.io/runbook` |
| 1.6 Container = nome do componente | ✅ recomendado | container `api` (não `app`/`main`) |

### Bloco 2 — Resiliência

| Regra | Status | Onde |
|-------|--------|------|
| 2.1 requests e limits | ✅ obrigatório | `base/deployment.yaml` container `api` |
| 2.2 readiness + liveness (endpoints distintos) | ✅ obrigatório | `/healthz/ready` e `/healthz/live` (liveness não checa banco) |
| 2.3 replicas >= 2 em prod | ✅ obrigatório | patch `overlays/prod/patches/deployment-prod.yaml` (`replicas: 2`); dev/stg ficam com `1` (base) |
| 2.4 Estratégia de atualização | ✅ obrigatório em prod | mesmo patch: `RollingUpdate`, `maxUnavailable: 0`, `maxSurge: 1` |
| 2.5 PodDisruptionBudget | ✅ recomendado | `overlays/prod/pdb.yaml` `minAvailable: 1` (só prod) |
| 2.6 terminationGracePeriodSeconds | ✅ recomendado | `60s` no pod spec da base |

### Bloco 3 — Segurança

| Regra | Status | Onde |
|-------|--------|------|
| 3.1 Sem `:latest` | ✅ proibido → respeitado | `image: registry.metacortex.io/nyx/api:2.9.1` |
| 3.2 securityContext completo | ✅ obrigatório | `runAsNonRoot`, `runAsUser`, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem`, `drop: ["ALL"]` + `emptyDir` em `/tmp` e `/var/cache/nyx` |
| 3.3 Sem segredo em texto puro | ✅ proibido → respeitado | `DATABASE_URL` via `secretKeyRef` |
| 3.4 automountServiceAccountToken: false | ✅ obrigatório | `base/serviceaccount.yaml` e pod spec |
| 3.5 ServiceAccount dedicada | ✅ recomendado | `nyx-api` (não `default`) |
| 3.6 Sem hostNetwork/hostPID/privileged | ✅ proibido → respeitado | ausentes; exemplo negativo comentado no deployment |
| 3.7 Imagem de registry interno | ✅ obrigatório | `registry.metacortex.io` |

### Bloco 4 — Vocabulário

Atendido por construção: Deployment (4.3) governa ReplicaSets que governam Pods
(4.1/4.2) que contêm containers; Service (4.4) com `port` != `targetPort` (4.5);
ConfigMap para não-sensível e Secret para sensível (4.7); probes (4.8) com
`readinessProbe` e `livenessProbe` distintas.

## Decisões de design

- **Base não é aplicável isolada**: falta o rótulo `instance` e o `namespace`,
  injetados pelo overlay. É o modelo esperado do Kustomize — sempre aplique via
  overlay (`kubectl apply -k overlays/<env>`).
- **`commonLabels` adiciona `instance` aos seletores** do Service e do
  Deployment. Como também adiciona ao pod template, o casamento (regra 1.4)
  permanece válido caractere por caractere.
- **`readOnlyRootFilesystem: true`** exigiu montar `emptyDir` em `/tmp` e
  `/var/cache/nyx` para a aplicação poder escrever.
- **Probes distintas**: liveness não checa banco, para não reiniciar a aplicação
  inteira quando o banco fica lento (regra 2.2).
- **Secret**: `base/secret.yaml` contém apenas placeholder base64. Em produção o
  valor real é injetado pelo pipeline/sealed-secret — nunca commitado no Git
  (regra 3.3). Secret é base64, não criptografia.
- **Exceções**: nenhuma exceção foi solicitada. Não existe exceção a regra
  **proibida** para workload de cliente.

## Pendências conhecidas

- Não foi incluído `CronJob`/`Job`; o padrão cita esses objetos na regra 1.3,
  mas a entrega cobre o workload principal (Deployment + Service).
- `DB_HOST` agora é patcheado por overlay (`pg.nyx-dev.svc`, `pg.nyx-stg.svc`,
  `pg.nyx-prod.svc`) via strategic merge — preserva `LOG_LEVEL` do base.
- Validação `kubectl dry-run` pendente: ambiente local sem `kubectl`/`kustomize`.
  YAML validado sintaticamente com PyYAML em todos os 17 arquivos.