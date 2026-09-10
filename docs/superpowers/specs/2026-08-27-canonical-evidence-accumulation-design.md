# Fase 3B.3.3 — Design Spec: Canonical Evidence Accumulation

**Classificação:** arquitetural
**Status:** `TASK1_FROZEN`; `TASK2_FROZEN`; `TASK3=BLOCKED_BY_BINDING_CONTRACT`.
O human ruling aprovou `ADOPT APPROACH B`; esta amendment permanece design-only,
aguardando revisão/approval humana antes de qualquer alteração do implementation
plan ou código. Nenhuma implementação desta spec está iniciada.
**Baseline original da decisão:** `HEAD == origin/main ==
4b5e5cfc76ce81863a72e1c3fc984d5bbb330c2f`
**Baseline da spec anterior:** `HEAD == origin/main ==
56b78a239e7ba53e302377415282da946a9e469f`
**Baseline desta amendment:** `HEAD ==
33481cbdb912bb45ddd7b3d7c736f464463e633c`
**Data:** 2026-08-27

Esta spec define a arquitetura da acumulação prospectiva de evidência canônica.
Ela não implementa código, não altera workflows, não cria a branch
`advisor-evidence` e não inicia a Fase 3B.4. As palavras **MUST**, **MUST NOT**,
**SHOULD** e **SHOULD NOT** são normativas.

## 1. Decisão resumida

O subsystem terá uma única autoridade histórica durável: a branch
`advisor-evidence` do mesmo repositório. Ela armazenará shards JSON canônicos,
append-only logicamente, com identidades, hashes, proveniência e manifests
determinísticos. O código em `main` interpretará esses shards; não haverá
histórico financeiro dentro de `main`.

O fluxo inicial será:

```text
report main/close
    ↓
canonical observation evidence sidecar (O+B)
    ↓
immutable observation evidence archive (O+B; sem maturação)
    ↓
collector → local transport → validation
    ↓
first archive: canonical market/corporate shards
    ↓
commit + push confirmado → fresh read da advisor-evidence
    ↓
materializer → horizon proofs/outcomes
    ↓
second archive: proofs/outcomes
    ↓
recovery drill
```

A execução continuará separando autoridade financeira, transporte operacional,
cache local e arquivo histórico. Falha de archive não pode alterar decisão,
risk, sizing, report, Telegram ou qualquer output financeiro.

## 2. Invariante central e fronteira de autoridade

Perder `data/advisor.db`, todos os caches de GitHub Actions, todos os runners e
qualquer workspace local MUST NOT destruir nem alterar o histórico canônico.

Usando somente:

```text
main + advisor-evidence
```

deve ser possível reconstruir, sem executar uma decisão antiga:

- canonical `ObservationEvidenceRecord` (observation + source binding);
- market evidence;
- corporate-action evidence;
- horizon proofs;
- forward outcomes;
- pending maturation state.

Nenhuma decisão antiga é recalculada durante recovery. A decisão original é a
observation arquivada; o outcome original é o outcome arquivado. A autoridade
de `main` fornece apenas os tipos e os algoritmos congelados necessários para
validar ou materializar esses dados.

O recovery invariant desta amendment é:

```text
From main + advisor-evidence, the system can recover the exact canonical
ObservationEvidenceRecord that was durably archived.
```

`main` sozinho não consegue reconstruir o binding histórico de um snapshot a
partir de uma `SignalObservation` sem anchor externa. Recovery não recalcula um
`ObservationSourceBinding` usando market data atual, provider atual ou lookup por
symbol.

| Superfície | Autoridade | Regra |
| --- | --- | --- |
| `main` | código | contém código e contratos; não é armazenamento histórico |
| `advisor-evidence` | evidência | fonte canônica durável e somente append lógico |
| SQLite local | cache/materialização | pode ser apagado e reconstruído; nunca é source of truth |
| GitHub Actions cache | aceleração | não é histórico nem autoridade |
| GitHub artifact | transporte/recovery auxiliar | pode expirar; não é autoridade |
| report main/close | decisão/report | continua com a autoridade existente |
| Telegram | notificação | não recebe novas decisões desta arquitetura |

## 3. Escopo da primeira implementação

A primeira implementação cobrirá somente:

1. emissão de um sidecar de observation evidence (O+B) pelos reports `main` e
   `close`;
2. publicação desse sidecar como shards imutáveis de observation evidence;
3. coleta diária de barras de mercado e corporate actions;
4. prova de corporate-action por horizon;
5. materialização de `ForwardMarketSeries` compatível com a 3B.2;
6. chamada da autoridade congelada da 3B.2;
7. publicação de outcomes novos, sem update;
8. recovery drill reproduzível.

Ficam fora dessa implementação:

- automação semanal da 3B.3.1;
- automação semanal da 3B.3.2;
- calibration;
- alteração de scoring ou risk;
- threshold optimization;
- dashboard, UI ou novo surface de relatório;
- mudanças no Telegram;
- broker, fills, ordens ou execução automática;
- backfill de `signal_journal` legacy;
- qualquer início da Fase 3B.4.

3B.3.1 e 3B.3.2 continuam manuais/on-demand inicialmente. O uso da 3B.2 na
maturação é apenas a execução operacional do engine congelado sobre evidência
prospectiva; não reabre nem modifica sua metodologia.

## 4. Módulos protegidos

A implementação MUST NOT modificar:

```text
advisor/scoring.py
advisor/risk.py
advisor/signal_observation.py
advisor/signal_outcome.py
advisor/predictive_evaluation.py
advisor/predictive_statistics.py
```

Esses módulos são autoridades congeladas. A integração acontece ao redor deles,
por adaptadores de schema, archive, coleta e materialização. Se a implementação
concluir que qualquer um dos seis arquivos precisa mudar, ela deve parar e
registrar `DESIGN_CONFLICT`; não deve contornar a restrição em silêncio.

## 5. Components e responsabilidades

Os componentes são pequenos e têm consumidores concretos:

### 5.1 `advisor/evidence_schema.py`

Define tipos de evidence, versões, identidades, validação, serialização JSON
canônica, hashes, parsing seguro e regras de path. Não faz network, Git, SQLite,
scoring ou avaliação de outcome.

### 5.2 `advisor/evidence_archive.py`

Recebe transportes já produzidos, valida shards, consulta a branch, aplica
idempotência, gera conflicts e manifests e publica por fast-forward. É o único
componente autorizado a publicar em `advisor-evidence`.

### 5.3 `advisor/evidence_collector.py`

Orquestra coleta de barras e corporate actions, respeita o orçamento dos
providers, valida fechamento de sessão e emite transportes. Não altera
observations, decisões ou outcomes.

O calendário de sessão local, usado somente pela coleta, será um helper
stdlib-only versionado dentro desse componente. Ele usa aritmética gregoriana,
regras explícitas de feriados e early close dos mercados US; não adiciona
dependência paga ou serviço externo.

### 5.4 `advisor/evidence_materializer.py`

Reconstrói dados a partir da branch, valida vínculos, resolve status por
horizon, produz o JSON local aceito pela 3B.2 e chama a autoridade existente.
Não implementa return, MFE, MAE, stop touch, 2R, 3R ou ambiguity.

### 5.5 Integração fina

`advisor/cli.py` apenas registra subcommands e despacha para esses componentes.
O report deve construir `ObservationEvidenceRecord` atomicamente na fronteira de
construction, usando a mesma instância do `AssetSnapshot` que alimentou a
decision. A lista de `SignalObservation` para SQLite é derivada dos records já
construídos; não há rejoin por symbol depois dessa fronteira. Markdown nunca é
fonte de observation.

## 6. Identity, payload, provenance e transport

Cada shard tem quatro domínios distintos:

1. `logical_identity`: decide qual fato pode existir uma única vez;
2. `payload`: fato imutável que será recuperado;
3. `provenance`: proveniência semântica necessária para validar o fato;
4. `transport`: detalhes de artifact, runner, publicação e compressão.

`transport` MUST NOT participar da identidade ou do hash canônico. Inclui nome
do artifact, job, runner, tentativa, caminho local, horário de publicação e
outros detalhes operacionais. Timestamp corrente não pode entrar na identidade
ou no conteúdo hash quando não for parte da semântica da evidência.

Cada shard canônico tem a forma lógica:

```json
{
  "canonical_content_sha256": "<sha256>",
  "evidence_type": "<fixed-type>",
  "logical_identity": {},
  "payload": {},
  "payload_sha256": "<sha256>",
  "provenance": {},
  "schema_version": "1.0"
}
```

As chaves exibidas são apenas um exemplo estrutural; a serialização final
ordena-as lexicograficamente.

`canonical content` é exatamente a composição de `logical_identity`, `payload`,
proveniência semântica e `schema_version`/`evidence_type`. No envelope acima,
`provenance` contém somente essa proveniência semântica; o domínio `transport`
fica fora. Portanto, transportes diferentes não alteram o conteúdo canônico.

As definições são:

```text
payload_sha256
  = SHA-256(UTF-8(canonical_json(payload)))

canonical_content_sha256
  = SHA-256(UTF-8(canonical_json({
      evidence_type,
      schema_version,
      logical_identity,
      payload,
      provenance
    })))
```

O objeto usado no segundo hash não contém nenhum dos dois campos de hash nem
`transport`. `payload_sha256` continua útil para inspeção do payload, mas não é
sozinho uma regra de idempotência. Para a mesma `logical_identity`:

- mesmo `canonical_content_sha256` resulta em `duplicate_same`;
- `canonical_content_sha256` diferente resulta em `conflict`.

Assim, mesmo OHLC com `price_provider` diferente na proveniência semântica é
`conflict`, não `duplicate_same`. O hash embutido de `SignalObservation` ou de
`SignalForwardOutcome` mantém sua semântica própria e é validado
independentemente.

### 6.1 JSON canônico

O JSON canônico MUST usar:

- UTF-8;
- `ensure_ascii=false`;
- `sort_keys=true`;
- separadores `(',', ':')`;
- `allow_nan=false`;
- nenhuma chave duplicada;
- números finitos, sem `NaN`, `Infinity` ou `-Infinity`;
- arrays ordenados por uma regra semântica definida pelo tipo;
- nenhum whitespace adicional;
- nenhum newline final.

O arquivo `.json` contém exatamente os bytes JSON canônicos. O parser de entrada
deve rejeitar chaves duplicadas via `object_pairs_hook` ou mecanismo equivalente
e deve rejeitar constantes não finitas antes da canonicalização.

### 6.2 JSON comprimido

Os shards persistidos usarão `.json.gz`. O gzip será determinístico:

- exatamente um membro gzip;
- DEFLATE nível 9;
- `MTIME=0`;
- `FLG=0`, sem nome, comentário ou campo extra;
- header `XFL=2` e `OS=255`;
- trailer CRC e tamanho conforme RFC 1952;
- nenhuma concatenação de membros;
- os bytes descomprimidos são exatamente o JSON canônico sem newline.

O archive valida EOF, ausência de `unused_data`/`unconsumed_tail`, CRC, tamanho
máximo e membro único. O manifest guarda `canonical_content_sha256`,
`payload_sha256` e, quando o arquivo for comprimido, `compressed_bytes_sha256`
e tamanho.

### 6.3 Canonical packaging boundary

Task 3 permanece responsável pela captura e continua emitindo seu wrapper
determinístico de observation sidecar. Task 4 permanece collection-only e
continua emitindo somente seus formatos públicos de transport para market
evidence e corporate-action evidence. Uma única fronteira fechada aceita
exatamente estas três famílias:

1. o observation sidecar emitido pelo Task 3;
2. o market transport emitido pelo Task 4;
3. o corporate-action transport emitido pelo Task 4.

```text
Task 3 observation sidecar ─┐
Task 4 market transport ────┼→ advisor/evidence_packager.py
Task 4 corporate transport ┘       → candidate canonical .json.gz shards
                                   → EvidenceArchive
```

O único entrypoint público do packager, para as três famílias, é:

```python
from pathlib import Path

def package_evidence_transport(
    *, transport_dir: Path, output_dir: Path
) -> tuple[Path, ...]:
    """Return the deterministically produced candidate canonical shard paths."""
```

`transport_dir` contém os transportes públicos aprovados: o
`observations.json.gz` produzido por `build_observation_sidecar`, o
`market-transport.json` produzido por `EvidenceCollector.collect_market` ou o
`corporate-actions-transport.json` produzido por
`EvidenceCollector.collect_corporate_actions`. A entrada deve pertencer a uma
das três famílias aprovadas; nomes, envelopes ou arquivos desconhecidos são
rejeitados. `output_dir` é a raiz dos candidatos que pode ser entregue a
`EvidenceArchive.archive`. O retorno é a lista determinística, ordenada, dos
paths de shards canônicos produzidos. O packager usa somente os schemas
canônicos já definidos e não cria novos tipos ou semântica de evidence.

O Task 3 sidecar continua sendo serializado pelo caminho existente como um
wrapper com `schema_version`, `source_sha`, `run_id`, `report_type` e
`records`. Para cada `records[i]`, o packager serializa o par já capturado
`observation + source_binding` no envelope canônico de observation definido em
`8.1`: o payload é a observation completa sem fields de outcome, a
`logical_identity` é exatamente `report_type + run_id + schema_version +
source_sha + symbol`, e a proveniência semântica é composta por
`binding_contract: "observation_snapshot_binding_v1"` e pelo
`source_binding` capturado.
Essa transformação é somente serialização. O packager não reconstrói
`AssetSnapshot`, não recalcula `snapshot_sha256`, não resolve provider/source,
não faz rejoin por symbol, não altera `SignalObservation` ou
`ObservationSourceBinding` e não envia o wrapper bruto diretamente ao archive.

Os shards empacotados são candidatos, nunca authority. Authority só existe
quando `EvidenceArchive` retorna `status` em `{committed, no_op}` e
`durability_confirmed == True`. Para o mesmo transport válido, o packager deve
produzir os mesmos paths, o mesmo conteúdo descomprimido e os mesmos bytes de
gzip, sem relógio corrente, aleatoriedade ou estado local da máquina.

Input malformado, desconhecido ou não suportado é packaging failure e não pode
expor um conjunto parcial de candidatos como output bem-sucedido: a exposição
dos shards é atômica. O packager não chama providers, coleta dados, escreve
SQLite, usa Git, arquiva, qualifica horizons, avalia outcomes, nem cria
authority. Nenhum output local ou empacotado é authority por si só.

## 7. Layout e paths da branch

O layout final é:

```text
evidence/
  branch-schema.json
  observations/
    YYYY/MM/DD/<logical_identity_sha256>.json.gz
  market-bars/
    YYYY/MM/DD/<logical_identity_sha256>.json.gz
  corporate-actions/
    YYYY/MM/DD/<logical_identity_sha256>.json.gz
  horizon-proofs/
    YYYY/MM/DD/<logical_identity_sha256>.json.gz
  outcomes/
    YYYY/MM/DD/<logical_identity_sha256>.json.gz
  manifests/
    YYYY/MM/DD/<batch_identity_sha256>.json.gz
  conflicts/
    YYYY/MM/DD/<conflict_identity_sha256>.json.gz
```

`logical_identity_sha256` é
`SHA-256(UTF-8(canonical_json(logical_identity)))`, em lowercase hexadecimal.
O nome do arquivo nunca contém texto vindo diretamente de asset, provider,
run, artifact ou resposta remota.

O diretório de data é uma partição derivada de uma data semântica já validada:

| Tipo | Data da partição |
| --- | --- |
| observation | `report_date_brt` |
| market bar | `market_date` |
| corporate action | `coverage_end_date` |
| horizon proof | `horizon_end_date` |
| outcome | `horizon_end_date` |
| manifest/conflict | menor data de partição do batch |

Logo, a partição não altera a identidade e não depende do relógio do runner.
Path absoluto, `..`, separador embutido, symlink, extensão inesperada, nome fora
do padrão hexadecimal e qualquer root diferente de `evidence/` são rejeitados.

## 8. Identities canônicas por tipo

### 8.1 Observation shard

O `logical_identity` é exatamente a identidade já congelada pela 3B.1, com a
chave `symbol` usada para coincidir com `compute_signal_id`:

```json
{
  "report_type": "main",
  "run_id": "123456",
  "schema_version": "1.0",
  "source_sha": "<40-or-64-lowercase-hex>",
  "symbol": "AMD"
}
```

Semanticamente, ela é `source_sha + run_id + report_type + asset`. O
`signal_id` deve ser o hash da identidade congelada. O `payload` contém uma
observation completa, incluindo `signal_id` e `observation_hash`, sem fields de
outcome. Um shard representa uma observation e sua proveniência de source
binding capturada; a ordem de rows no sidecar é apenas transport e é ordenada
por `signal_id`.

O payload de `SignalObservation` permanece exatamente o contrato 3B.1. O
`ObservationSourceBinding` é proveniência semântica adjacente ao payload,
serializada como parte da canonical observation evidence, e não é injetado no
schema da observation.

Para evitar uma materialização parcial, o payload canônico contém exatamente os
campos da observation 3B.1 abaixo, com `reason_codes` como array JSON e
`persisted_at_utc` presente como `null` no sidecar canônico:

```text
signal_id, schema_version, source_sha, run_id, run_origin,
report_date_brt, report_type, signal_timestamp_utc,
asset, asset_type, universe_origin, market_session, market_timezone,
decision_label, bucket, investment_quality_score, swing_trade_score,
decision_confidence_score, data_quality_score, expected_value_r,
backtest_sample_size, sample_quality, data_quality, missing_data_severity,
ideal_entry, alternative_entry, entry_semantics,
alternative_entry_semantics, stop, target_2r, target_3r, per_unit_risk,
risk_amount, risk_fraction, max_position_units, max_position_value,
reason_codes, data_source, data_timestamp, last_price_timestamp, provider,
is_stale, stock_regime, crypto_regime, relative_strength_vs_spy,
relative_strength_vs_qqq, relative_strength_vs_sector, sector_benchmark,
evaluation_role, provenance_json, observation_hash, persisted_at_utc
```

O archive não injeta seu horário de publicação em `persisted_at_utc`. Uma
materialização SQLite pode preencher esse campo localmente, pois ele é excluído
do `observation_hash`; o valor canônico recuperável permanece o sidecar
determinístico.

`report_date_brt` continua no payload, é `NOT NULL` e participa de
`observation_hash`, mas não participa da identidade. `signal_timestamp_utc` e
`persisted_at_utc` preservam a semântica congelada do módulo; não devem ser
substituídos pelo horário de publicação.

O archive reconstrói e valida a observation pela autoridade congelada. Uma
linha com `signal_id` ou `observation_hash` inconsistente é rejeitada antes de
qualquer publicação.

### 8.2 Market-bar shard

O `logical_identity` de uma barra diária é:

```json
{
  "asset_type": "stock",
  "interval": "1d",
  "market_date": "2026-08-26",
  "market_timezone": "America/New_York",
  "schema_version": "1.0",
  "symbol": "AMD"
}
```

Para crypto, o timezone é `UTC`. `price_provider` não entra na identidade:
duas fontes para o mesmo símbolo/data colidem e produzem `conflict`, em vez de
serem misturadas silenciosamente.

O payload contém o OHLCV bruto validado, sem transformação corporativa:

```json
{
  "asset_type": "stock",
  "interval": "1d",
  "market_date": "2026-08-26",
  "market_timezone": "America/New_York",
  "ohlcv": {
    "close": 100.0,
    "high": 101.0,
    "low": 99.0,
    "open": 100.5,
    "volume": 123456
  },
  "price_basis": "raw_ohlcv",
  "session_status": "complete",
  "session_close_type": "regular"
}
```

Os números representam os valores fornecidos pelo price provider; não há
`adjusted_close`, não há multiplicação por split e não há troca de provider
dentro de uma série. A proveniência contém `price_provider`, hash da resposta
de origem, `collection_policy_version` e demais metadados sanitizados. A série
é identificada pelo conjunto `asset_type + symbol + interval + timezone +
price_basis`; o materializer rejeita uma série que contenha mais de um
`price_provider`.

Stocks e ETFs usam o mesmo contrato de sessão US. Crypto usa `raw_ohlcv`,
timezone `UTC` e não possui feed de split.

### 8.3 Corporate-action shard

O shard representa uma resposta `Alpha Vantage`, `function=SPLITS`, para uma
janela de cobertura explícita. A identidade é:

```json
{
  "asset_type": "stock",
  "corporate_action_provider": "alpha_vantage",
  "coverage_end_date": "2026-08-26",
  "coverage_start_date": "2020-01-01",
  "function": "SPLITS",
  "schema_version": "1.0",
  "symbol": "AAPL"
}
```

O payload mantém a resposta semântica do provider (`symbol` e `data`) e uma
representação normalizada das rows para prova. O response body e suas rows são
sanitizados antes de serem armazenados; API key, URL, headers, authorization e
exceptions nunca são armazenados. A proveniência guarda o hash da resposta
recebida e o status sanitizado.

Cada evento normalizado contém:

```json
{
  "effective_date": "2020-08-31",
  "split_factor_raw": "4.0000",
  "split_ratio": {
    "new_shares": "4",
    "old_shares": "1"
  }
}
```

`split_factor_raw` permanece texto para não perder a representação do provider.
`split_ratio` é calculado com `Decimal`, reduzido a inteiros positivos e nunca
passa por float binário. `5.0000` torna-se `5/1`; `0.2500` torna-se `1/4`.

Uma resposta bem-sucedida com zero events é evidence válida e deve conter
explicitamente `normalized_events=[]`; isso permite provar ausência no intervalo.
Uma resposta posterior com a mesma identidade e conteúdo diferente é
`conflict`, não revisão silenciosa. Uma janela de cobertura diferente tem nova
identidade, e só pode ser usada se cobrir completamente o intervalo provado.

### 8.4 Horizon-proof shard

O `logical_identity` é exatamente:

```json
{
  "horizon_bars": 5,
  "observation_hash": "<sha256>",
  "schema_version": "1.0",
  "signal_id": "<sha256>"
}
```

Assim, cada horizon é independente. O payload MUST conter, no mínimo:

- `signal_market_date`;
- `horizon_start_date`;
- `horizon_end_date`;
- `horizon_bars`;
- `corporate_action_policy`;
- `corporate_action_provider`;
- `split_check_status`;
- `split_event_count`;
- `price_basis`;
- `signal_price_basis_status`;
- `evidence_hashes` dos shards de observation, bars e corporate action;
- `proof_status`.

Para stock/ETF, a policy é exatamente
`verified_no_split_in_signal_horizon_v1`, o provider é `alpha_vantage` e o
status terminal é `verified_none` ou `split_in_horizon_unavailable`.
`verified_none` exige também que a qualificação do source binding canônico ligado
à observation resulte em `signal_price_basis_status=verified_raw_ohlcv`; sem
isso o status do horizon é `signal_basis_unavailable` e não há entrega à 3B.2.

Para crypto, a policy é `not_applicable_crypto_raw_ohlcv_v1`, o provider é
`not_applicable`, `split_check_status=not_applicable`,
`split_event_count=0` e o basis é `raw_ohlcv`.

`evidence_hashes.market_bar_shards` contém os hashes canônicos dos N bars
exatos, em ordem de data; `evidence_hashes.corporate_action_shard` contém o
hash da resposta que cobre inclusivamente o intervalo, ou `null` para crypto.

Um proof de stock com `verified_none` pode apresentar ao evaluator o basis
`split_adjusted_ohlc`, mas apenas como equivalência numérica provada de
OHLCV bruto dentro daquela janela. Isso NÃO significa que o provider forneceu
preços ajustados. O proof deve registrar simultaneamente
`raw_price_basis=raw_ohlcv` e `corporate_action_policy`.

Um proof com `split_in_horizon_unavailable` é uma evidência durável de que o
horizon não pode ser entregue à 3B.2 na policy v1. Ele não cria outcome.

Não se publica proof terminal para `pending`, `feed_unavailable` ou
`market_data_unavailable`; esses são estados de materialização derivados, não
uma ausência histórica definitiva. Isso preserva a possibilidade de uma
coleta transitória amadurecer depois.

### 8.5 Outcome shard

O `logical_identity` é a identidade congelada da 3B.2:

```json
{
  "evaluation_policy_version": "1.0",
  "horizon_bars": 5,
  "observation_hash": "<sha256>",
  "schema_version": "1.0",
  "signal_id": "<sha256>"
}
```

O payload é o `SignalForwardOutcome` completo produzido pela autoridade
existente, incluindo `outcome_id`, `outcome_hash`, bars JSON/hash, return,
MFE, MAE, touches, primeiros eventos, flags de ambiguity, alternativa,
provider e price basis. `persisted_at_utc` é metadata de persistência e não
altera `outcome_hash`.

A proveniência do shard vincula o `horizon-proof` e os market-bar shards usados.
Ela não altera o `outcome_hash` congelado pela 3B.2, mas entra no
`canonical_content_sha256` do shard de evidence. Portanto, mesmo
`logical_identity` e mesmo conteúdo de payload só são `duplicate_same` quando
o `canonical_content_sha256` inteiro é igual; proveniência semântica diferente
produz `conflict`. Outcome não é atualizado.

Uma revisão posterior de corporate action nunca reescreve outcome. Ela gera
conflict/audit evidence e só uma nova policy/version poderá tratar uma
reevaluation em fase separada.

### 8.6 Manifest

Existe no máximo um manifest canônico para cada `batch_identity` e seu path
determinístico correspondente. O primeiro
commit válido de um batch escreve esse manifest com `status=committed`, junto
com os shards canônicos da transação. Um retry idêntico primeiro faz fetch e
valida os shards e o manifest já existentes; se forem equivalentes, retorna
`no_op` somente como resultado operacional. Esse retry não escreve outro
manifest, não altera o manifest `committed` e não cria evidence histórica de
`no_op`.

O manifest canônico contém:

- `schema_version`;
- `operation`;
- `batch_identity` e `batch_sha256`;
- lista ordenada de shards;
- logical identities;
- `payload_sha256` e `canonical_content_sha256`;
- hashes/tamanhos dos bytes comprimidos quando aplicável;
- counts por `evidence_type` e status;
- `status`.

`batch_identity` é derivada do operation type e da lista ordenada de entries de
evidence (`evidence_type`, `logical_identity_sha256` e
`canonical_content_sha256`); não contém hash de transport nem horário
corrente. Em manifest persistido, `status` é
`committed` para um batch canônico ou `conflict` para uma transação exclusiva
de conflict. `no_op` é somente resultado operacional e `rejected` não produz
manifest. O manifest é um registro de transação, não um índice autoritativo:
remover manifests não pode remover os shards nem impedir sua reconstrução por
varredura da branch.

Sua identity exata é o objeto:

```json
{
  "entries": [
    {
      "canonical_content_sha256": "<sha256>",
      "evidence_type": "observation",
      "logical_identity_sha256": "<sha256>"
    }
  ],
  "operation": "archive",
  "schema_version": "1.0"
}
```

`entries` é ordenado por `evidence_type`,
`logical_identity_sha256` e `canonical_content_sha256`. O
`batch_identity_sha256` é o hash desse objeto. Paths, horários, runner e
commit SHA ficam fora da identity; paths podem ser reproduzidos a partir dos
entries.

Uma transação de conflict possui identity própria, derivada da lista ordenada
de conflict entries, e nunca reutiliza a `batch_identity` do batch canônico
que originou o conflito. Seu manifest exclusivo pode ter `status=conflict`;
seu `operation` é `conflict_archive`, sua lista de entries contém somente as
identidades dos conflict shards e ele referencia somente esses shards.

### 8.7 Conflict evidence

Conflict nunca usa last-writer-wins. O shard de conflict preserva:

- `evidence_type`;
- `logical_identity`;
- `logical_identity_sha256`;
- `existing_canonical_sha256`;
- `incoming_canonical_sha256`;
- `existing_bytes_sha256`, quando disponível;
- `incoming_bytes_sha256`, quando disponível;
- `reason_code` de uma enumeração sanitizada.

O canonical shard existente permanece intacto. O conteúdo de conflict não inclui
secrets, headers, URLs secretas ou exception text. Um retry do mesmo conflict é
`duplicate_same` do próprio conflict shard.

A identity exata do conflict é:

```json
{
  "evidence_type": "observation",
  "existing_canonical_sha256": "<sha256>",
  "incoming_canonical_sha256": "<sha256>",
  "logical_identity": {},
  "reason_code": "divergent_payload",
  "schema_version": "1.0"
}
```

O path usa o hash canônico desse objeto. O `reason_code` é uma enumeração
fechada (`divergent_payload`, `hash_mismatch`, `provider_mixing`,
`identity_collision` ou `corporate_action_revision_conflict`); nenhum texto de
exception é aceito.

## 9. Observation archival e sidecar

O report `main` ou `close` deve:

1. executar normalmente;
2. produzir os `AssetDecision` normalmente;
3. construir atomicamente um `ObservationEvidenceRecord` para cada decision,
   pela autoridade existente e pelo snapshot exato daquela decision;
4. somente depois de construir a lista completa, derivar as observations e
   persistir no SQLite exatamente como hoje;
5. exportar os mesmos records daquele run para um sidecar.

O construction boundary usa o `AssetSnapshot` já associado à decision. Ele nunca
faz parse do Markdown, nunca recalcula decisão, nunca localiza novamente um
snapshot por `symbol`, nunca reconstrói uma observation posterior e nunca
modifica timestamp histórico. O sidecar é construído diretamente dos records em
memória, sem releitura do DB para descobrir a proveniência.

O sidecar de transporte pode ser:

```text
reports/evidence/observations.json.gz
```

Ele contém `schema_version=1.0`, `source_sha`, `run_id`, `report_type` e a lista
completa de `ObservationEvidenceRecord` canônicos ordenada por `signal_id`. Cada
entry contém a observation e o `ObservationSourceBinding` já capturado para
ela; não existe um sidecar de provenance que faça um segundo lookup.

O archive job explode o sidecar em um shard por observation evidence e valida a
linha contra `signal_id`, `observation_hash`, o source binding e o schema 3B.1.
A serialização JSON do sidecar usa arrays para `reason_codes`; a representação
textual usada pela tabela SQLite não vira a autoridade canônica.

Ativar ou desativar o archive não pode mudar bytes de report, `AssetDecision`,
risk, sizing, report grade ou Telegram. Um erro de sidecar somente produz
diagnóstico operacional e faz o archive job falhar; o report permanece o
resultado válido já produzido.

### 9.1 Architectural amendment — atomic observation source binding

#### 9.1.1 Contradição arquitetural descoberta

O contrato anterior esperava que:

```text
resolve_signal_price_basis_status(observation, sidecar)
```

conseguisse provar posteriormente que uma claim pertencia exatamente ao
`AssetSnapshot` que originou a `SignalObservation`. Isso é impossível com a
interface atual: `SignalObservation` não contém snapshot digest autoritativo,
source-contract identity original, o snapshot completo de decision-time nem
qualquer outra anchor independente suficiente.

`signal_input_hash`, `snapshot_binding` e `entry_integrity_sha256` armazenados
somente na sidecar podem provar self-consistency dos próprios bytes e das
relações que a sidecar declara. Eles não podem autenticar a relação histórica
`O ↔ snapshot X` se toda a sidecar for coerentemente reescrita. A regra explícita
é:

```text
CHECKSUM SELF-CONSISTENCY != HISTORICAL SOURCE AUTHORITY.
```

Isso não é uma falha criptográfica. A solução não é HMAC, chaves, assinaturas ou
outra camada de autenticação criptográfica. A correção é capturar a proveniência
no mesmo passo de construction, antes que a observação seja separada do
snapshot exato.

#### 9.1.2 Contratos conceituais novos

Os contratos v1 são equivalentes a:

```python
@dataclass(frozen=True)
class ObservationSourceBinding:
    signal_id: str
    observation_hash: str
    snapshot_sha256: str
    price_basis_claim: PriceBasisClaim | None


@dataclass(frozen=True)
class ObservationEvidenceRecord:
    observation: SignalObservation
    source_binding: ObservationSourceBinding
```

`PriceBasisClaim` é o claim estruturado governado pela policy de qualificação,
incluindo `price_basis`, `price_basis_policy_version` e `source_contract`.
O binding captura o valor exato presente no snapshot; não o reconstrói a partir
de provider, symbol, route ou allowlist.

`snapshot_sha256` é exatamente o SHA-256 de
`canonical_json_bytes(snapshot_projection_v1(exact_snapshot_X))`. A
`snapshot_projection_v1` fechada, versionada e campo-a-campo está definida
abaixo. Ela inclui `data_fetch_metadata` e não é uma projeção limitada a
`symbol`, `asset_type`, latest close, provider ou claim.

Não se usa `object id`, `repr`, `Python hash()`, memory address nem timestamp de
runtime inventado para compor o digest. Conceitualmente:

```text
snapshot_sha256
  = SHA-256(canonical_json_bytes(
      snapshot_projection_v1(exact_snapshot_X)
    ))
```

`snapshot_sha256` MUST ser calculado uma única vez durante a construção atômica,
a partir do mesmo objeto `AssetSnapshot` usado para construir os decision
inputs daquela observation. Depois disso ele é provenance capturada e carregada
como tal; não pode ser recalculado a partir de um novo snapshot.

#### 9.1.2.1 `snapshot_projection_v1`

`snapshot_projection_v1` é um contrato CLOSED WORLD. O objeto que entra no hash
tem exatamente a chave fixa `projection_version` e os 34 campos atuais de
`AssetSnapshot` enumerados na tabela abaixo. Cada campo recebe uma decisão
explícita `INCLUDE` ou `EXCLUDE`; não existe seleção condicional por utilidade.
Os nomes e tipos são os de `advisor.models.AssetSnapshot` no HEAD desta spec.

| AssetSnapshot field | Action | Canonical representation | Rationale |
| --- | --- | --- | --- |
| `symbol` | `INCLUDE` | string literal | identity and decision input |
| `asset_type` | `INCLUDE` | string literal | decision and market policy input |
| `theme` | `INCLUDE` | string literal | scoring and benchmark context |
| `candles` | `INCLUDE` | array of `Candle` projections, preserving order | technical decision input |
| `fundamentals` | `INCLUDE` | `Fundamentals` projection | valuation and quality input |
| `event` | `INCLUDE` | `EventInfo` projection or JSON `null` | earnings/event input |
| `funding_rate` | `INCLUDE` | JSON number or `null` | crypto decision input |
| `open_interest_change` | `INCLUDE` | JSON number or `null` | crypto decision input |
| `cvd_proxy` | `INCLUDE` | JSON number or `null` | crypto decision input |
| `coinbase_premium` | `INCLUDE` | JSON number or `null` | crypto decision input |
| `liquidation_imbalance` | `INCLUDE` | JSON number or `null` | crypto decision input |
| `missing_data` | `INCLUDE` | array of strings, preserving order | data-quality and limitation input |
| `news_events` | `INCLUDE` | array of JSON objects with string keys, preserving order | news decision and report input |
| `provider_capabilities` | `INCLUDE` | array of `ProviderCapability` projections, preserving order | provider availability and fallback context |
| `earnings_status` | `INCLUDE` | string literal | report/data-quality state |
| `guidance_status` | `INCLUDE` | string literal | report/data-quality state |
| `macro_status` | `INCLUDE` | string literal | report/data-quality state |
| `news_status` | `INCLUDE` | string literal | report/data-quality state |
| `sec_filings_status` | `INCLUDE` | string literal | report/data-quality state |
| `data_source` | `INCLUDE` | string literal | source used by the snapshot |
| `data_timestamp` | `INCLUDE` | string or JSON `null` | source/decision timing |
| `cache_age_seconds` | `INCLUDE` | integer or JSON `null` | affects stale classification and report |
| `data_fetch_metadata` | `INCLUDE` | `DataFetchMetadata` projection or JSON `null` | source provenance, freshness and claim |
| `quote_status` | `INCLUDE` | string literal | quote/report state |
| `quote_price` | `INCLUDE` | JSON number or `null` | quote/report input |
| `quote_timestamp` | `INCLUDE` | string or JSON `null` | quote/report timing |
| `quote_source` | `INCLUDE` | string or JSON `null` | quote source provenance |
| `quote_age_seconds` | `INCLUDE` | integer or JSON `null` | quote freshness/report state |
| `quote_is_intraday` | `INCLUDE` | JSON boolean | quote basis/report state |
| `previous_close` | `INCLUDE` | JSON number or `null` | market/quote context |
| `daily_change` | `INCLUDE` | JSON number or `null` | market/quote context |
| `daily_change_pct` | `INCLUDE` | JSON number or `null` | market/quote context |
| `benchmark_provenance` | `INCLUDE` | JSON mapping projection | benchmark/report provenance |
| `crypto_metric_provenance` | `INCLUDE` | nested JSON mapping projection | crypto source provenance |

Não há campo atual de `AssetSnapshot` classificado como `EXCLUDE`. Os campos de
cache, fetch timing, fallback e capability parecem operacionais pelo nome, mas
no código atual são parte do estado capturado do source: `cache_age_seconds`
participa da classificação de stale, e os demais são carregados na provenance,
no report ou na descrição do caminho de fallback. Não há em `AssetSnapshot` ou
`DataFetchMetadata` um campo de retry do runner, publication timestamp,
artifact path, process ID, exception diagnostic ou outro dado exclusivamente de
transport. Esses dados de transporte continuam fora desta projeção quando
existirem fora do snapshot.

As projeções nested são fechadas conforme as definições atuais:

**`Candle`**

| Field | Action | Canonical representation |
| --- | --- | --- |
| `date` | `INCLUDE` | string literal |
| `open` | `INCLUDE` | JSON number from the model's `float` |
| `high` | `INCLUDE` | JSON number from the model's `float` |
| `low` | `INCLUDE` | JSON number from the model's `float` |
| `close` | `INCLUDE` | JSON number from the model's `float` |
| `volume` | `INCLUDE` | JSON number from the model's `float` |

**`Fundamentals`**

| Field | Action | Canonical representation |
| --- | --- | --- |
| `pe` | `INCLUDE` | JSON number or `null` |
| `peg` | `INCLUDE` | JSON number or `null` |
| `historical_pe` | `INCLUDE` | JSON number or `null` |
| `revenue_growth` | `INCLUDE` | JSON number or `null` |
| `eps_growth` | `INCLUDE` | JSON number or `null` |
| `margin_trend` | `INCLUDE` | JSON number or `null` |
| `free_cash_flow_positive` | `INCLUDE` | JSON boolean or `null` |
| `market_cap` | `INCLUDE` | JSON number or `null` |
| `average_volume` | `INCLUDE` | JSON number or `null` |
| `market_cap_rank` | `INCLUDE` | JSON integer or `null` |

**`EventInfo`**

| Field | Action | Canonical representation |
| --- | --- | --- |
| `days_to_earnings` | `INCLUDE` | JSON integer or `null` |
| `guidance_recent` | `INCLUDE` | JSON boolean or `null` |
| `post_earnings_gap_percent` | `INCLUDE` | JSON number or `null` |
| `last_earnings_date` | `INCLUDE` | string or `null` |
| `next_earnings_date` | `INCLUDE` | string or `null` |

**`ProviderCapability`**

| Field | Action | Canonical representation |
| --- | --- | --- |
| `provider` | `INCLUDE` | string literal |
| `capability` | `INCLUDE` | string literal |
| `configured` | `INCLUDE` | JSON boolean |
| `supported_by_plan` | `INCLUDE` | JSON boolean |
| `implemented` | `INCLUDE` | JSON boolean |
| `last_status` | `INCLUDE` | string literal |
| `fallback_available` | `INCLUDE` | JSON boolean |

**`DataFetchMetadata`**

| Field | Action | Canonical representation |
| --- | --- | --- |
| `provider` | `INCLUDE` | string literal |
| `endpoint` | `INCLUDE` | string literal |
| `fetched_at` | `INCLUDE` | string or `null` |
| `cache_fetched_at` | `INCLUDE` | string or `null` |
| `source_timestamp` | `INCLUDE` | string or `null` |
| `cache_age_seconds` | `INCLUDE` | JSON integer or `null` |
| `source_age_seconds` | `INCLUDE` | JSON integer or `null` |
| `is_fresh` | `INCLUDE` | JSON boolean or `null` |
| `cache_hit` | `INCLUDE` | JSON boolean |
| `fallback_used` | `INCLUDE` | JSON boolean |
| `fallback_from` | `INCLUDE` | string or `null` |
| `fallback_to` | `INCLUDE` | string or `null` |
| `granularity` | `INCLUDE` | string or `null` |
| `market_data_kind` | `INCLUDE` | string or `null` |
| `price_basis_claim` | `INCLUDE` | `PriceBasisClaim` projection or JSON `null` |

`DataFetchMetadata.price_basis_claim` MUST ser `INCLUDE`. A alteração de
`price_basis`, `price_basis_policy_version` ou `source_contract` altera a
projeção e, portanto, `snapshot_sha256`. Quando a claim estiver ausente, a
chave `price_basis_claim` permanece presente com JSON `null`.

**`PriceBasisClaim`**

| Field | Action | Canonical representation |
| --- | --- | --- |
| `price_basis` | `INCLUDE` | underlying Literal string value |
| `price_basis_policy_version` | `INCLUDE` | underlying Literal string value |
| `source_contract` | `INCLUDE` | string literal |

`news_events` tem o tipo atual `list[dict[str, object]]`,
`benchmark_provenance` tem `dict[str, object]` e
`crypto_metric_provenance` tem `dict[str, dict[str, object]]`. Suas chaves
preservam exatamente a semântica do mapping e devem ser strings; a ordenação de
object keys é responsabilidade de `canonical_json_bytes`. Seus valores nested
usam somente JSON `null`, boolean, integer, finite float, string, mapping ou
array; nenhum valor é convertido implicitamente para string. Arrays atualmente
presentes dentro desses mappings também preservam sua ordem como parte do
snapshot capturado.

Não há `datetime`, `date` ou `Decimal` nas definições atuais de `AssetSnapshot`,
`DataFetchMetadata`, `PriceBasisClaim` ou nos dataclasses nested acima. Datas e
timestamps atuais são strings e permanecem strings; não há conversão
adicional. `Literal` values são serializados como suas strings subjacentes.
Números mantêm o tipo do modelo: campos `float` são JSON numbers, campos `int`
são JSON integers e campos `bool` são JSON booleans. Valores não finitos são
rejeitados por `canonical_json_bytes`; não se usa `default=str`, `repr` ou
Python `hash()`.

Optional fields sempre mantêm sua chave na projeção com JSON `null`. Arrays e
mappings default permanecem como `[]` e `{}` quando esses forem os valores do
snapshot. Não se usa `asdict(snapshot)`, `vars(snapshot)`, reflection sobre
dataclass fields, `**dict` ou serialização recursiva automática do objeto
`AssetSnapshot` para selecionar a projeção.

As políticas de ordem são explícitas: `candles`, `missing_data`, `news_events`
e `provider_capabilities` usam `ORDER IS SEMANTIC` e preservam a ordem existente
no `AssetSnapshot`; não são ordenados genericamente durante a projeção. Os
arrays nested dentro dos três mappings JSON também usam `ORDER IS SEMANTIC` e
preservam a ordem fornecida. A ordenação lexicográfica de chaves de objects é
aplicada somente pelo `canonical_json_bytes`, conforme a seção 6.1.

O pseudocode normativo completo é:

```python
def json_value_v1(value):
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non_finite_json_number")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value.keys()):
            raise ValueError("non_string_json_object_key")
        return {
            key: json_value_v1(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [json_value_v1(item) for item in value]
    raise ValueError("unsupported_snapshot_projection_value")


def price_basis_claim_projection_v1(claim):
    if claim is None:
        return None
    return {
        "price_basis": claim.price_basis,
        "price_basis_policy_version": claim.price_basis_policy_version,
        "source_contract": claim.source_contract,
    }


def candle_projection_v1(candle):
    return {
        "date": candle.date,
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
    }


def fundamentals_projection_v1(fundamentals):
    return {
        "pe": fundamentals.pe,
        "peg": fundamentals.peg,
        "historical_pe": fundamentals.historical_pe,
        "revenue_growth": fundamentals.revenue_growth,
        "eps_growth": fundamentals.eps_growth,
        "margin_trend": fundamentals.margin_trend,
        "free_cash_flow_positive": fundamentals.free_cash_flow_positive,
        "market_cap": fundamentals.market_cap,
        "average_volume": fundamentals.average_volume,
        "market_cap_rank": fundamentals.market_cap_rank,
    }


def event_projection_v1(event):
    if event is None:
        return None
    return {
        "days_to_earnings": event.days_to_earnings,
        "guidance_recent": event.guidance_recent,
        "post_earnings_gap_percent": event.post_earnings_gap_percent,
        "last_earnings_date": event.last_earnings_date,
        "next_earnings_date": event.next_earnings_date,
    }


def provider_capability_projection_v1(capability):
    return {
        "provider": capability.provider,
        "capability": capability.capability,
        "configured": capability.configured,
        "supported_by_plan": capability.supported_by_plan,
        "implemented": capability.implemented,
        "last_status": capability.last_status,
        "fallback_available": capability.fallback_available,
    }


def data_fetch_metadata_projection_v1(metadata):
    if metadata is None:
        return None
    return {
        "provider": metadata.provider,
        "endpoint": metadata.endpoint,
        "fetched_at": metadata.fetched_at,
        "cache_fetched_at": metadata.cache_fetched_at,
        "source_timestamp": metadata.source_timestamp,
        "cache_age_seconds": metadata.cache_age_seconds,
        "source_age_seconds": metadata.source_age_seconds,
        "is_fresh": metadata.is_fresh,
        "cache_hit": metadata.cache_hit,
        "fallback_used": metadata.fallback_used,
        "fallback_from": metadata.fallback_from,
        "fallback_to": metadata.fallback_to,
        "granularity": metadata.granularity,
        "market_data_kind": metadata.market_data_kind,
        "price_basis_claim": price_basis_claim_projection_v1(
            metadata.price_basis_claim
        ),
    }


def snapshot_projection_v1(snapshot):
    return {
        "projection_version": "snapshot_projection_v1",
        "symbol": snapshot.symbol,
        "asset_type": snapshot.asset_type,
        "theme": snapshot.theme,
        "candles": [
            candle_projection_v1(candle)
            for candle in snapshot.candles
        ],
        "fundamentals": fundamentals_projection_v1(snapshot.fundamentals),
        "event": event_projection_v1(snapshot.event),
        "funding_rate": snapshot.funding_rate,
        "open_interest_change": snapshot.open_interest_change,
        "cvd_proxy": snapshot.cvd_proxy,
        "coinbase_premium": snapshot.coinbase_premium,
        "liquidation_imbalance": snapshot.liquidation_imbalance,
        "missing_data": [value for value in snapshot.missing_data],
        "news_events": [
            json_value_v1(event)
            for event in snapshot.news_events
        ],
        "provider_capabilities": [
            provider_capability_projection_v1(capability)
            for capability in snapshot.provider_capabilities
        ],
        "earnings_status": snapshot.earnings_status,
        "guidance_status": snapshot.guidance_status,
        "macro_status": snapshot.macro_status,
        "news_status": snapshot.news_status,
        "sec_filings_status": snapshot.sec_filings_status,
        "data_source": snapshot.data_source,
        "data_timestamp": snapshot.data_timestamp,
        "cache_age_seconds": snapshot.cache_age_seconds,
        "data_fetch_metadata": data_fetch_metadata_projection_v1(
            snapshot.data_fetch_metadata
        ),
        "quote_status": snapshot.quote_status,
        "quote_price": snapshot.quote_price,
        "quote_timestamp": snapshot.quote_timestamp,
        "quote_source": snapshot.quote_source,
        "quote_age_seconds": snapshot.quote_age_seconds,
        "quote_is_intraday": snapshot.quote_is_intraday,
        "previous_close": snapshot.previous_close,
        "daily_change": snapshot.daily_change,
        "daily_change_pct": snapshot.daily_change_pct,
        "benchmark_provenance": json_value_v1(
            snapshot.benchmark_provenance
        ),
        "crypto_metric_provenance": json_value_v1(
            snapshot.crypto_metric_provenance
        ),
    }


snapshot_sha256 = sha256(
    canonical_json_bytes(snapshot_projection_v1(exact_snapshot_X))
).hexdigest()
```

`canonical_json_bytes` é a única implementação de canonical JSON. Ela aplica
UTF-8, `ensure_ascii=false`, `sort_keys=true`, separadores sem whitespace,
`allow_nan=false`, rejeição de chaves não-string e números não finitos conforme
a seção 6.1. A projeção acima não omite nenhum campo atual e não aceita campos
adicionais implicitamente.

Portanto:

```text
snapshot_projection_v1 excludes no current AssetSnapshot fields
```

Adicionar um novo field a `AssetSnapshot` não o inclui automaticamente em
`snapshot_projection_v1`. A v1 permanece byte-for-byte igual para os campos
enumerados. Qualquer alteração que inclua, exclua, renomeie ou altere a
representação de um field exige revisão explícita e uma nova projection version
quando alterar bytes semânticos históricos, além da decisão de migration e
compatibilidade antes de produzir evidence com a nova versão.

O teste E deve montar `expected_projection` literalmente a partir desta tabela,
sem usar somente o helper de produção como authority esperada:

```text
expected_projection = the literal snapshot_projection_v1 object defined above
expected_sha256 = SHA256(canonical_json_bytes(expected_projection))
record.source_binding.snapshot_sha256 == expected_sha256
```

Esse teste deve matar, no mínimo, mutations que omitam um field `INCLUDE`,
incluam um field `EXCLUDE`, alterem `PriceBasisClaim.source_contract`,
reordenem uma sequence semanticamente ordenada ou capturem automaticamente um
future dataclass field. Um teste adicional deve provar o CLOSED WORLD usando um
synthetic field no input/model sem alterar os dataclasses de produção: esse
field não pode aparecer na projection v1. A lista de mutations é requisito do
testing contract; sua implementação pertence ao plan posterior.

No mesmo passo, `price_basis_claim` é capturado exatamente de:

```text
exact_snapshot.data_fetch_metadata.price_basis_claim
```

Essa claim deve ser a que o snapshot continha naquele instante. Não se escolhe
uma claim posteriormente por provider, symbol, route lookup ou allowlist lookup.
A allowlist continua necessária para qualificar a claim, mas não cria
provenance.

`signal_id` e `observation_hash` do binding devem coincidir exatamente com os da
observation contida no record. Eles identificam o vínculo estrutural entre O e
B; não transformam o binding em parte do schema 3B.1.

`signal_price_basis_status` é um resultado derivado da validação da claim
capturada; não é um campo adicional de `ObservationSourceBinding` e não
substitui `price_basis_claim`.

`advisor/signal_observation.py` permanece protegido. Não se adiciona
`snapshot_sha256` a `SignalObservation`, não se altera seu schema, `signal_id`,
`observation_hash` ou as canonical observation semantics da 3B.1. Source binding
é evidence provenance adjacente, não parte da decisão histórica congelada em
3B.1.

#### 9.1.3 Construction boundary e atomic batch semantics

O fluxo conceitual aprovado é:

```text
decision + exact AssetSnapshot X
|
v
single construction step
|
+--> SignalObservation O
|
+--> ObservationSourceBinding B(X)
     |
     +-- immutable canonical snapshot digest
     +-- PriceBasisClaim captured from X
     +-- source/provenance contract
     +-- signal_id
     +-- observation_hash
|
v
ObservationEvidenceRecord(O, B)
|
+--> SQLite receives O only
|
+--> sidecar receives O+B
|
v
first canonical archive
|
v
advisor-evidence becomes durable authority
```

A mesma instância de `AssetSnapshot` usada para construir os decision inputs
deve ser usada para criar B. O construction boundary não pode fazer:

```text
construct observation
→ guardar apenas observation
→ posteriormente localizar snapshot novamente por symbol
→ montar binding
```

O contrato conceitual da CLI deixa de ser somente:

```text
_build_signal_observations(...) -> list[SignalObservation]
```

e passa a ser:

```text
_build_signal_observation_records(...) -> list[ObservationEvidenceRecord]
```

Para cada decision, a CLI deve: (1) selecionar/usar o snapshot exato já
associado àquela decision; (2) construir O; (3) imediatamente calcular B do
mesmo snapshot; (4) formar `ObservationEvidenceRecord(O, B)`; e (5) somente
então avançar para a próxima decision. Depois da lista completa:

```python
records = build...
observations = [record.observation for record in records]
```

SQLite recebe `observations`; o sidecar recebe `records`. Nenhum rejoin
`observation.symbol -> snapshots_by_symbol[symbol]` ocorre depois da
construction.

Todas as records devem ser construídas antes de qualquer persistence ou sidecar
publication. Se a record N falhar, deve haver zero partial sidecar; o sistema
não inventa B, não escolhe snapshot alternativo e não recarrega o provider.
Falha posterior do SQLite não descarta nem altera B. Mutação posterior de
`snapshots_by_symbol` ou de qualquer estado global de snapshot também não altera
o record ou o sidecar já construído.

#### 9.1.4 O que a captura atômica prova e não prova

Na trusted deterministic report construction path, a captura atômica garante
que:

- o código normal não pode criar O com X e depois selecionar Y para provenance;
- O e B são produzidos na mesma iteration/call context;
- B captura digest e claim do snapshot exato X;
- a sidecar recebe B já capturado, não um snapshot re-resolvido;
- uma falha de SQLite não remove B;
- estado mutável subsequente não altera B;
- depois do canonical archive, replacement divergente para a mesma identity é
  archive conflict, nunca rewrite.

A captura atômica não fornece assinatura criptográfica contra um processo
malicioso que reescreva todos os bytes antes do primeiro archive, não autentica
um arbitrary unarchived dict e não cria proof recuperável a partir de
`SignalObservation` sozinho. Isso não é requisito v1. O trust boundary v1 é:

```text
trusted deterministic report construction
→ canonical archive confirmation
→ append-only advisor-evidence authority
```

#### 9.1.5 Sidecar API

O contrato antigo abaixo está **SUPERSEDED**:

```text
build_observation_sidecar(
    observations,
    snapshots_by_symbol=...,
    ...,
)
```

O novo contrato conceitual é:

```python
build_observation_sidecar(
    records: Sequence[ObservationEvidenceRecord],
    *,
    output_path: Path,
) -> Path
```

A sidecar builder recebe O+B, serializa e valida a coerência interna entre
observation e binding. Ela não recebe `AssetSnapshot`, não recebe
`snapshots_by_symbol`, não consulta provider e não consulta SQLite. Nenhum
parâmetro equivalente pode permitir resolver snapshot posteriormente por
symbol.

#### 9.1.6 Resolver e qualificação fail-closed

`resolve_signal_price_basis_status` não deve alegar autenticar uma arbitrary
coherently rewritten pre-archive sidecar contra uma `SignalObservation` que não
possui snapshot anchor. Sua entrada conceitual é um
`ObservationEvidenceRecord` canônico ou uma canonical sidecar entry que
represente esse record, não um observation solto acompanhado de provenance
relocalizável.

O resolver deve validar, fail closed:

- `signal_id` e `observation_hash` do observation e do binding;
- coerência estrutural do record;
- formato e presença de `snapshot_sha256` quando exigidos;
- formato de `PriceBasisClaim`;
- allowlist e policy exatas;
- schema/version;
- ambiguity e duplicates.

Para canonical archived evidence, `verified_raw_ohlcv` significa somente que o
canonical observation evidence record capturou uma claim explicitamente
qualificada de raw OHLCV do mesmo snapshot usado na construction da observation.
Não significa cryptographic proof derivable from `SignalObservation` alone.

Missing ou invalid claim resulta em `signal_basis_unavailable`. Para stock/ETF,
`verified_raw_ohlcv` exige exatamente:

```text
price_basis == raw_ohlcv
price_basis_policy_version == price_basis_v1
source_contract ∈ exact qualified allowlist
```

FMP light continua unqualified. Nome de provider sozinho continua insuficiente.
Para crypto, a policy própria continua `not_applicable_crypto_raw_ohlcv`, sem
usar essa regra para qualificar stock/ETF.

#### 9.1.7 Estados de autoridade

Há três estados explícitos:

| Estado | Semântica | Autoridade |
| --- | --- | --- |
| **IN-MEMORY CAPTURE** | `ObservationEvidenceRecord` recém-construída | trusted report construction path |
| **UNARCHIVED SIDECAR** | artifact de transporte estruturalmente validável | ainda não é durable authority |
| **CANONICAL ARCHIVED SIDECAR** | `EvidenceArchive` retornou `committed` com `durability_confirmed=True`, ou `no_op` canônico validado | `advisor-evidence` é durable source of truth |

O sidecar não arquivado pode ser validado estruturalmente, mas não é usado como
proof de recuperação histórica até a confirmação do archive. Depois da
confirmação canônica, qualquer conteúdo divergente para a mesma logical
identity é `conflict`, nunca rewrite.

#### 9.1.8 Provider policy e não interferência financeira

`price_provider_assignment_v1` permanece inalterada: stock/ETF usa FMP, HYPE usa
Hyperliquid, crypto configurado restante usa Binance, e o provider mais antigo
da série canônica continua sticky. `ObservationSourceBinding` captura o source
contract realmente usado pelo snapshot da decision; não executa provider
assignment.

`ObservationSourceBinding` é observational evidence only. Ele não altera
`AssetDecision`, scoring, risk, `ideal_entry`, stop, targets, report rendering,
provider selection ou fallback behavior. A existência do binding não melhora
recommendation score nem confidence.

#### 9.1.9 Migration note: correction `33481cb`

A correction commit `33481cbdb912bb45ddd7b3d7c736f464463e633c` introduziu
`signal_input_hash`, `snapshot_binding` e `entry_integrity_sha256` numa tentativa
de resolver o problema dentro do resolver. A review demonstrou que esses campos,
isoladamente, são apenas self-consistency.

A implementação futura deve manter somente o que continuar útil como structural
integrity, remover ou reformular qualquer lógica que alegue independent
historical authentication a partir desses hashes e não preservar complexidade
apenas porque já existe. Esta spec não decide quais linhas serão removidas; isso
pertence ao implementation plan aprovado posteriormente.

#### 9.1.10 Plan implications

Esta amendment não modifica o implementation plan atual. Depois de human
review/approval desta spec, o plan deverá registrar somente estas implicações:

- **Task 3:** deve ser amended para implementar a captura atômica de
  `ObservationEvidenceRecord` e remover o binding atrasado por
  `snapshots_by_symbol`;
- **Task 4:** collectors permanecem inalterados por este ruling;
- **Task 5:** materialization deve consumir o source binding canônico arquivado,
  nunca reconstruir binding histórico a partir de providers atuais;
- **Task 6:** a primeira confirmação de archive é a transição de transport
  evidence para durable authority;
- **Task 8:** testes de security/recovery podem mutar transport, mas a semântica
  do canonical archive permanece a authority boundary.

Tasks 4–9 não são redesenhadas além dessas implicações. Task 4 não é iniciada,
e a propriedade de conflict para identity divergente do teste J pertence ao
Task 6 de archive/maturation, não exige acoplamento direto do Task 3 ao archive.

## 10. Market evidence e calendário

### 10.1 Stocks e ETFs

Uma barra diária US só entra na branch quando todos os requisitos forem
verdadeiros:

- a sessão terminou, com margem segura após o fechamento;
- a data foi explicitamente retornada pelo provider;
- a data é sessão real do calendário US;
- OHLCV está completo, finito e válido;
- não é preenchimento sintético;
- não é weekend ou holiday fictício;
- `low <= open <= high` e `low <= close <= high`;
- `volume >= 0` e preços são positivos;
- early close é aceito como sessão válida.

O calendário local `us_equities_session_v1` considera segunda a sexta, os
feriados US aplicáveis (New Year, Martin Luther King Jr., Washington's
Birthday, Good Friday, Memorial Day, Juneteenth, Independence Day, Labor Day,
Thanksgiving e Christmas), regras de observância e os early closes US
publicados para véspera de Independence Day, dia seguinte a Thanksgiving e
Christmas Eve quando úteis. A implementação usa somente stdlib e regras
determinísticas; não consulta um calendário remoto para decidir se uma data
existe.

Não se contam calendar days como trading bars. O horizon usa apenas bars
canônicas em datas de sessão crescente.

### 10.2 Crypto

Crypto permanece `price_basis=raw_ohlcv`, `market_timezone=UTC` e não possui
corporate-action split feed. A barra diária só entra depois do fechamento do
dia UTC. Candle corrente incompleta é rejeitada e não vira ausência definitiva.

### 10.3 Providers e séries

`price_provider` e `corporate_action_provider` são conceitos independentes.
Market bars armazenam o price provider da série; corporate-action shards usam
Alpha Vantage. A mesma canonical series não pode conter providers diferentes,
mesmo que cada resposta isolada seja válida.

A assignment policy determinística da v1 é a versão literal
`price_provider_assignment_v1`, definida em `main` e incluída na proveniência
semântica de cada série:

| Chave de série nova | `price_provider` atribuído |
| --- | --- |
| `asset_type=stock` | `fmp` |
| `asset_type=etf` | `fmp` |
| `asset_type=crypto` e `symbol=HYPE` | `hyperliquid` |
| `asset_type=crypto` e qualquer outro symbol configurado | `binance` |

O symbol é normalizado antes da consulta à tabela. Se não houver suporte ao
symbol, ao par ou ao provider atribuído, o resultado é
`market_data_unavailable`. Provider indisponível também resulta em
`market_data_unavailable`; não há substituição silenciosa por outro provider.

Para série já existente, o provider é derivado da evidence de market bars
canônica mais antiga e permanece sticky. A recuperação relê esse provider e a
`price_provider_assignment_v1` da evidence; não faz nova seleção por sucesso
de rede. Se a série já contiver providers misturados, o collector rejeita a
mistura como `conflict` e não publica uma barra que a agrave. A tabela não
amplia o universe nem introduz providers novos nesta revisão.

## 11. Corporate-action policy v1

O provider de corporate action é `Alpha Vantage`, `function=SPLITS`. A
qualificação já concluída inclui:

- AAPL 2020 4:1;
- NVDA 2024 10:1;
- TSLA 2020 5:1;
- IGV ETF 2024 5:1;
- historical backfill;
- ETF split support;
- historical recovery.

A policy congelada é `verified_no_split_in_signal_horizon_v1`. Para cada
horizon, o intervalo é exatamente inclusivo:

```text
[signal_market_date, horizon_end_date]
```

ou, em desigualdades:

```text
signal_market_date <= split_effective_date <= horizon_end_date
```

Consequências:

- qualquer split no intervalo resulta em `split_in_horizon_unavailable`;
- feed indisponível resulta em `feed_unavailable`;
- zero events explicitamente comprovados resulta em `verified_none` somente
  sem conflict relevante e com `signal_price_basis_status=verified_raw_ohlcv`
  para stock/ETF;
- feed ausente nunca é interpretado como zero events;
- split com `effective_date == signal_market_date` invalida todos os horizons;
- não se tenta descobrir se `ideal_entry` já estava pós-split;
- a perda de coverage é aceita para evitar falso outcome.

Snapshots de corporate action do mesmo `corporate_action_provider` e
`symbol` podem ter coverage windows sobrepostas. Em qualquer interseção, os
eventos normalizados MUST ser idênticos. Diferença de presença/ausência,
`effective_date` ou split ratio produz um conflict com reason code
`corporate_action_revision_conflict`. Enquanto existir esse conflict relevante
ao intervalo do horizon, nenhum novo proof `verified_none` pode ser produzido.
Outcomes já congelados permanecem intactos; o conflict é registrado somente
para audit, e qualquer reevaluation depende de uma policy futura explícita.

Não há Policy B de transformação de preços. Quando o stock raw OHLCV é válido,
o feed qualificado está disponível e não há split no intervalo, o raw OHLCV é
numericamente equivalente a split-adjusted OHLC naquela janela. Só nessa
situação o materializer pode declarar ao 3B.2
`price_basis=split_adjusted_ohlc`, sempre com o proof e as duas declarações:

```text
corporate_action_policy = verified_no_split_in_signal_horizon_v1
split_check_status = verified_none
```

Isso não renomeia nem altera o raw evidence.

A mesma policy é aplicada aos benchmarks `SPY`, `SMH`, `IGV`, `QQQ` e `XLV`.
ETF nunca é presumido como instrumento sem split. Evidence de benchmark
indisponível afeta somente a avaliação histórica; não muda `AssetDecision`.

## 12. Horizon proof e status de maturação

O materializer primeiro calcula a data civil do sinal usando o timestamp e
timezone da observation, com a regra congelada da 3B.2: `America/New_York`
para stock e `UTC` para crypto. Para cada N em `(5, 10, 20, 40)` ele localiza
as N bars elegíveis, isto é, `bar.date > signal_market_date`, em ordem crescente.

O estado por horizon é resolvido nesta ordem:

1. série inválida, bar faltante ou bar incompleto: `market_data_unavailable`;
2. menos de N bars completas: `pending`;
3. conflict de corporate action relevante ao intervalo:
   `conflict`, com reason `corporate_action_revision_conflict`, sem novo proof;
4. stock/ETF sem source binding canônico cuja claim qualifique como
   `signal_price_basis_status=verified_raw_ohlcv`:
   `signal_basis_unavailable`, sem entrega à 3B.2;
5. stock com bars suficientes mas sem corporate-action coverage válido:
   `feed_unavailable`;
6. stock com split inclusivo: `split_in_horizon_unavailable`;
7. stock com zero split comprovado e basis de sinal qualificada:
   `verified_none`;
8. crypto com N bars completas: `not_applicable` para split e basis raw.

`pending`, `feed_unavailable`, `market_data_unavailable`,
`signal_basis_unavailable` e `conflict` não geram proof terminal entregue à
3B.2. `split_in_horizon_unavailable` é terminal para a policy v1 porque o
evento é evidence durável; `verified_none` também é terminal para aquele
horizon porque seu proof específico foi escrito. Nenhum novo `verified_none`
é permitido enquanto houver conflict de corporate action relevante, mesmo que
uma resposta isolada declare zero events.

Exemplo de split entre a quinta e a décima bar:

| Horizon | Resultado |
| --- | --- |
| 5 | `verified_none`, se a janela inclusiva de 5 não contém split e a basis do sinal é qualificada |
| 10 | `split_in_horizon_unavailable` |
| 20 | `split_in_horizon_unavailable` |
| 40 | `split_in_horizon_unavailable` |

Um proof de h5 já publicado não é alterado quando h10 amadurece. Cada proof
carrega somente seus próprios N bars e hashes.

## 13. Materialização e chamada da 3B.2

Para uma observation com horizons aprovados, o materializer:

1. lê somente shards canônicos válidos da `advisor-evidence`, após fresh
   fetch/read confirmado do head que contém o archive de market/corporate;
2. valida observation, seu source binding canônico, bars, provider sticky,
   signal basis, corporate-action coverage e proofs;
3. seleciona o maior prefixo de bars cuja prova é válida;
4. constrói um JSON local com o contrato exato da 3B.2:

```json
{
  "schema_version": "1.0",
  "assets": {
    "AMD": {
      "asset_type": "stock",
      "provider": "<sanitized-price-provider>",
      "price_basis": "split_adjusted_ohlc",
      "candles": [
        {"date":"2026-08-20","open":100,"high":101,"low":99,"close":100,"volume":1000}
      ]
    }
  }
}
```

5. chama `evaluate_signal_observation` do módulo congelado para receber
   `SignalForwardEvaluation`, outcomes e `pending_horizons`;
6. aceita somente os outcomes de stock/ETF cujos proofs correspondentes são
   `verified_none` com source binding capturado cuja claim qualifique como
   `signal_price_basis_status=verified_raw_ohlcv`, ou
   outcomes crypto `not_applicable`;
7. emite proof/outcome transports para o único writer, mas esses transports
   não viram authority até a segunda transação de archive.

O JSON local é materialização temporária, não autoridade, e não contém URLs,
headers, API keys ou paths do provider. Se um horizon tem split, feed
indisponível, conflict de corporate action ou `signal_basis_unavailable`, ele
não é entregue ao evaluator como outcome válido. Se o evaluator devolver um
outcome para horizon sem proof, isso é erro de integração e o batch é rejeitado.

O materializer pode reconstruir uma SQLite vazia para consumo local, mas essa
SQLite é derivada dos shards e não pode ser lida como fonte histórica. A
persistência de outcome usa a identidade/hash da 3B.2 e não implementa uma
segunda fórmula.

## 14. Outcome maturation

Durability MUST preceder maturation. A sequência obrigatória por ciclo é:

1. collector produz o transport local;
2. o transport é validado fora da branch;
3. o archive writer grava a primeira transação com os canonical market e
   corporate-action shards;
4. o commit dessa transação é feito e o push é confirmado;
5. somente então ocorre fresh fetch/read da `advisor-evidence`;
6. o materializer enumera observations canônicas, nunca `signal_journal`,
   determina bars e `horizon_end_date` por sessões reais e lê apenas os
   canonical shards desse fresh read;
7. a authority congelada produz os horizon proofs e outcomes permitidos;
8. proofs e outcomes novos são enviados como segundo transport e arquivados
   numa segunda transação;
9. somente depois de archive válido a materialização SQLite local é
   reconstruída/atualizada e counters sanitizados são emitidos.

Se a primeira transação de archive falhar, o ciclo para antes do fresh
fetch/materializer e nenhuma maturação ocorre. Transport recém-coletado ou
proof/outcome ainda não arquivado nunca sustenta outro proof ou outcome.
Durante a segunda transação, proof e outcome podem ser calculados no mesmo
passo a partir dos canonical market/corporate shards já lidos; o outcome não
pode reler um proof ainda não arquivado como authority.

O orchestration nunca reimplementa `return`, `MFE`, `MAE`, stop touch, 2R, 3R,
ambiguity, alternativa ou qualquer regra da 3B.2. O input não vem da internet
durante a avaliação: o evaluator recebe somente JSON local validado.

## 15. Pending, unavailable e conflicts

Os status têm semântica distinta:

| Status | Significado | Pode amadurecer? |
| --- | --- | --- |
| `pending` | ainda faltam N bars completas | sim |
| `split_in_horizon_unavailable` | bars existem, mas split viola policy v1 | não para v1 |
| `feed_unavailable` | corporate-action evidence não foi comprovada | sim |
| `market_data_unavailable` | price data está faltante ou inválida | sim |
| `signal_basis_unavailable` | a base usada no sinal não foi provada pelo source binding canônico | não até novo record sidecar validamente arquivado |
| `conflict` | mesma identity tem conteúdo divergente | não sem revisão explícita |
| `verified_none` | zero splits foi comprovado no intervalo | terminal do horizon |

Indisponibilidade transitória não vira `verified_none`. Weekend, holiday, candle
not yet published e `duplicate_same` não são falhas de archive.

## 16. Branch writer, atomicidade e concurrency

`advisor-evidence` MUST ser uma orphan branch: seu root commit não pode ser
descendant de `main` nem copiar a árvore de `main`. O bootstrap ocorre uma
única vez, durante implementação/deployment explicitamente autorizado, antes
de qualquer archive de runtime. O root commit controlado contém exatamente um
arquivo fixo, `evidence/branch-schema.json`, com schema/version e zero
financial evidence, por exemplo:

```json
{
  "branch_name": "advisor-evidence",
  "financial_evidence_count": 0,
  "schema_version": "1.0"
}
```

Nenhum código de `main` pode estar na árvore desse root commit. Em runtime,
branch inexistente resulta em `evidence_branch_missing` e o writer sai nonzero;
ele nunca cria automaticamente a branch a partir de `main`. O bootstrap não
usa force push.

Somente `evidence_archive.py`, executado pelo writer job, pode publicar na
branch `advisor-evidence`. Ela não é checkout padrão do app.

O writer deve usar estes princípios:

1. fetch da branch atual;
2. validação completa do batch fora da árvore autoritativa;
3. staging controlado;
4. resolução de identidade, duplicate e conflict;
5. geração do manifest determinístico;
6. verificação de hashes, paths e contagens;
7. commit fast-forward sem partial canonical batch;
8. push sem force.

Se qualquer item novo do batch falhar por schema, hash, path, storage ou
conflito, nenhum canonical shard novo daquele batch é publicado. Em caso de
conflict, pode ser publicado atomicamente somente o conflict/audit shard e seu
manifest, sem publicar os novos canonical shards; isso não é partial canonical
commit. Input inválido sem conflict não gera commit.

Um batch contendo apenas `duplicate_same` retorna `no_op` operacionalmente,
sem novo manifest e sem overwrite. A branch mantém todos os shards anteriores.

O workflow e o writer usarão exatamente:

```yaml
concurrency:
  group: advisor-evidence-writer
  cancel-in-progress: false
```

Em push race:

1. o primeiro push rejeitado causa novo fetch;
2. a operação é reaplicada sobre o novo head;
3. item agora idêntico vira `duplicate_same`;
4. item agora divergente vira `conflict`;
5. item ainda novo tenta fast-forward novamente;
6. há no máximo três tentativas de push;
7. a terceira rejeição produz `push_race_exhausted` e exit nonzero.

Não há force push, merge automático, history rewrite ou esconderijo de
conflict. A branch deve ter ruleset que bloqueie force-push e delete, preserve
linearidade e restrinja escrita ao writer. Os checks do processo também são
executados em bare repositories temporários locais para que a política seja
testável sem depender de uma configuração manual do GitHub.

## 17. Permissions e handoff entre jobs

### 17.1 Report job

O job que chama report mantém `contents: read` e recebe os provider secrets
necessários. Ele produz report e observation evidence sidecar (O+B), mas não
publica na branch.

### 17.2 Archive/writer job

O job separado baixa o artifact do report e tem `contents: write`, sem
`FMP_API_KEY`, `ALPHAVANTAGE_API_KEY`, CoinGecko, Coinbase ou outros provider
secrets. Ele valida o sidecar e publica apenas paths allowlisted na
`advisor-evidence`.

### 17.3 Collector job

O novo workflow de evidence executa o collector com provider secrets e
`contents: read`. Ele produz transportes de market/corporate evidence e faz
upload como artifact.

### 17.4 Publish/mature job

O job seguinte baixa o artifact, possui `contents: write`, não possui provider
secrets, publica a primeira transação e só continua depois de confirmar o push
e fazer fresh fetch da branch. O materializer/maturer lê somente evidence
canônica já disponível; seus proofs/outcomes seguem para uma segunda transação.
O mesmo componente writer publica todas as categorias; nenhum job com secret
de provider pode escrever na branch.

Handoff entre jobs usa somente artifacts/outputs locais, com paths fixos,
hashes e validação. Artifacts continuam transporte e recovery auxiliar.

Nightly Review permanece read-only e inalterado.

## 18. Scheduling e orçamento gratuito

O workflow único `.github/workflows/financial-advisor-evidence.yml` terá dois
gatilhos cron, além de dispatch manual:

- `30 00 * * *`: coleta crypto após o fechamento do dia UTC;
- `30 22 * * 1-5`: coleta stock/ETF após o fechamento US com margem segura,
  cobrindo o horário de inverno e ficando ainda mais conservador no horário de
  verão.

O mesmo workflow publica e amadurece depois de cada coleta aplicável. Não são
criados workflows semanais de 3B.3.1 ou 3B.3.2.

Alpha Vantage SPLITS só é consultado para symbols/horizons pendentes,
coverage ausente ou refresh controlado. A response history é arquivada e
reutilizada; símbolos já provados não são consultados em cada execução sem motivo.
O collector preserva headroom gratuito e não concorre desnecessariamente com o
budget FMP de main/close. O budget de FMP do report continua separado do
budget de corporate action.

## 19. CLI boundary

O conjunto mínimo coerente de subcommands será:

```text
python -m advisor evidence package --transport-dir <transport> --output-dir <dir>
python -m advisor evidence archive --transport-dir <canonical-transport> --repo-dir <dir>
python -m advisor evidence collect --assets-file <file> --transport-dir <dir>
python -m advisor evidence materialize --evidence-checkout <dir> --db <db>
python -m advisor evidence mature --evidence-checkout <dir> --db <db> --transport-dir <dir>
```

`archive` é o único que escreve na branch. `collect` pode usar providers e
somente produz transportes. `package` converte somente as três famílias
aprovadas de transporte em shards canônicos candidatos; não cria authority.
`materialize` é uma operação READ-ONLY de inspeção/materialização sobre o
checkout fornecido pelo caller: não prova durabilidade do archive, não prova
freshness, não escreve SQLite, não cria outcomes, não arquiva evidence e não
cria authority. `mature` chama a 3B.2 e produz proof/outcome transports; não
faz push direto.

O caminho stateful é `mature_evidence_cycle`, que continua impondo:

```text
first archive durável
→ fresh canonical checkout
→ evaluation/materialization
→ second archive durável
→ SQLite persistence
```

SQLite, quando usado por esse caminho stateful, é output reconstruível, nunca
input de autoridade. Não são adicionados tokens, marker files, certificados de
durabilidade ou outros mecanismos runtime ao `materialize` standalone.

`advisor/cli.py` não conterá lógica de identidade, Git, split, calendar ou
outcome. Os comandos emitirão JSON/status compacto e sanitizado; exceptions
cruas não chegam ao stdout.

## 20. Failure policy e observabilidade

Report e evidence têm authorities diferentes. Se archive falhar, o report
continua válido e nenhum report/decision/risk/Telegram é alterado. O archive
job sai nonzero para:

- conflict;
- hash mismatch;
- schema inválido;
- storage indisponível;
- unsafe path;
- gzip malformado ou decompression over limit;
- race de push irrecuperável;
- `evidence_branch_missing`;
- provider mixing;
- batch parcialmente inválido.

O archive não sai nonzero por pending normal, weekend, holiday, duplicate_same
ou candle ainda não publicada.

Counters/status mínimos:

```text
observations_archived
observations_duplicate_same
observations_conflict

market_bars_archived
market_data_status

corporate_actions_archived
split_check_status

outcomes_written
outcomes_pending
outcomes_duplicate_same
outcomes_conflict

archive_status
```

Logs e manifests não podem registrar API keys, authorization headers, secret
URLs ou exceptions que contenham credentials. Reasons são códigos bounded,
sanitizados e allowlisted.

## 21. Security boundary

O writer e o materializer tratam qualquer conteúdo da branch e dos artifacts como
DATA. Eles nunca executam conteúdo armazenado, importam módulos a partir dele
ou o usam como shell input.

As validações obrigatórias são:

- somente paths relativos às roots allowlisted;
- rejeição de path absoluto e `..`;
- rejeição de symlink no arquivo ou em ancestors;
- extensões exatamente `.json` ou `.json.gz`, conforme o tipo;
- gzip de membro único, framing válido e limites de expansão;
- chaves JSON duplicadas rejeitadas;
- `NaN`, `Infinity` e números não finitos rejeitados;
- schema, enum, identity e hash cruzados;
- limites de payload, rows, arrays, nested depth e batch;
- symbols, providers e IDs com padrões bounded;
- nenhuma injeção de asset/provider no path;
- nenhum segredo em provenance, conflict ou observability.

Os limites v1 são fixos: 25 MiB comprimidos por shard, 100 MiB
descomprimidos por shard, 500 MiB descomprimidos por batch, no máximo 10.000
observations por sidecar, 50.000 bars por transport e 10.000 corporate-action
events por response. Um manifest não pode referenciar mais de 100.000 shards.
Exceder qualquer limite é `rejected`, não truncamento.

## 22. Recovery drill obrigatório

A primeira implementação não pode ser aprovada sem um drill usando fixture
canônica e um repositório Git local descartável:

1. produzir `ObservationEvidenceRecord`, market bars, corporate actions, proofs
   e outcomes sintéticos porém canônicos;
2. arquivar a fixture na branch de evidence do repositório descartável;
3. registrar logical IDs, canonical hashes, byte hashes e counts originais;
4. remover a SQLite temporária;
5. remover caches operacionais e artifacts de transporte;
6. iniciar uma materialização vazia;
7. ler somente a fixture/store de `advisor-evidence`;
8. validar todos os shards e manifests;
9. reconstruir `ObservationEvidenceRecord` e market/corporate evidence;
10. reconstruir o conjunto de outcomes arquivados sem chamar scoring;
11. detectar os horizons ainda pending;
12. comparar IDs, hashes, status e counts com o registro original.

O resultado deve ser deterministamente equivalente. O drill não executa uma
decisão antiga e não importa `signal_journal`. A remoção de SQLite ocorre
somente em caminho temporário do teste; o DB do usuário não é alvo do drill.

Recovery deve continuar funcionando se manifests forem removidos, porque eles
são índices de transação e não autoridade. O materializer varre shards,
revalida hashes e recria qualquer índice/materialização.

## 23. Testing strategy pré-registrada

### 23.1 Amendment: contrato de binding e acceptance substituída

É **INVÁLIDA** a acceptance anterior que exigia:

```text
arbitrary sidecar Y
+
recompute all local hashes
+
resolver(observation O, sidecar Y)
→ signal_basis_unavailable
```

Essa propriedade exigiria que O contivesse uma anchor externa que ela não
contém. Uma sidecar Y coerentemente reescrita pode ser self-consistent sem ser
historicamente autenticável contra O. Nenhum teste deve tratar essa
indetectabilidade pré-archive como falha do resolver.

Os testes obrigatórios que substituem essa acceptance são:

- **A.** construction atômica produz O+B a partir do exact snapshot X;
- **B.** não existe API de sidecar builder que aceite
  `snapshots_by_symbol`;
- **C.** um snapshot Y do mesmo symbol não pode ser substituído depois da
  construction de O+B;
- **D.** o `PriceBasisClaim` em B é igual ao claim exato capturado de X;
- **E.** `snapshot_sha256` em B é igual a
  `SHA256(canonical_json_bytes(snapshot_projection_v1(X))).hexdigest()` no
  momento da construction;
- **F.** mutar `snapshots_by_symbol` ou o estado global de snapshot depois da
  construction não altera B nem o sidecar;
- **G.** falha de SQLite não descarta nem altera B;
- **H.** serialização do sidecar usa B pré-construído e não recalcula B a
  partir de um snapshot;
- **I.** serializar o mesmo O+B duas vezes produz conteúdo canônico
  determinístico;
- **J.** depois de O+B estar canônico no `EvidenceArchive`, tentar a mesma
  logical identity com B diferente resulta em archive `conflict`, nunca
  rewrite.

Os testes A–I pertencem ao construction/sidecar contract do Task 3. O teste J
é de integração do Task 6 (archive/maturation e transição de autoridade) e
deve ser registrado explicitamente como tal no implementation plan amended,
sem acoplar o Task 3 diretamente ao archive.

O teste futuro deve cobrir estas propriedades e invariantes:

1. mesma identity + mesmo `canonical_content_sha256` resulta em
   `duplicate_same`/`no_op` operacional;
2. mesma identity + `canonical_content_sha256` divergente resulta em
   `conflict`, ainda que o payload OHLC seja igual quando a proveniência
   semântica seja diferente;
3. retry de report não duplica observation;
4. output/decision do report é idêntico com archive on/off;
5. archive failure não altera decisão;
6. JSON canônico é determinístico;
7. gzip determinístico produz os mesmos bytes;
8. manifest é determinístico;
9. row/path traversal é rejeitado;
10. symlink é rejeitado;
11. gzip malformado é rejeitado;
12. decompression oversized é rejeitada;
13. split na signal date é inclusivo;
14. split no horizon end é inclusivo;
15. split depois de h5 e antes de h10 separa os resultados;
16. `feed_unavailable` nunca vira `verified_none`;
17. ausência de split qualifica o basis somente com proof;
18. mistura de market providers é rejeitada;
19. crypto não usa split policy de equity;
20. o evaluator congelado é chamado, não reimplementado;
21. conflict de outcome congelado é preservado;
22. writer race faz retry determinístico;
23. batch parcial não cria partial canonical commit;
24. deletion de DB/cache permite recovery;
25. legacy journal nunca é materializado como canonical;
26. comportamento dos módulos protegidos permanece inalterado.

As seguintes propriedades adicionais ficam pré-registradas para esta revisão,
sem remover as anteriores:

27. retry idêntico do mesmo batch não cria um segundo manifest canônico nem
    altera o manifest existente;
28. revisão de corporate action com coverage sobreposta divergente cria
    `corporate_action_revision_conflict`;
29. signal basis desconhecida produz `signal_basis_unavailable` e bloqueia a
    maturação de stock/ETF;
30. bootstrap de `advisor-evidence` é orphan e contém somente
    `evidence/branch-schema.json` sem financial evidence;
31. materialização não consome transport ainda não arquivado;
32. mesmo payload com proveniência semântica diferente produz `conflict`;
33. assignment de provider é sticky para série existente e determinístico para
    série nova;
34. provider atribuído indisponível produz `market_data_unavailable` sem
    fallback.

Além desses testes, a suíte deve cruzar independentemente cada hash, verificar
ordem de arrays, comparar bytes de duas execuções e testar a fronteira de data
BRT/US/UTC. Testes de decisão devem provar que o caminho de archive não chama
scoring nem risk adicional.

## 24. Mutation strategy

Serão pré-registradas somente mutations de alta alavancagem:

1. trocar o intervalo de split de `<=` para `<`;
2. excluir `signal_market_date` do intervalo;
3. tratar `feed_unavailable` como `verified_none`;
4. sobrescrever divergent payload para a mesma identity;
5. aceitar path traversal;
6. aceitar provider mixing;
7. importar `signal_journal` no recovery;
8. fazer archive failure alterar o resultado do report.

Somente quando agregarem cobertura independente, também serão usadas estas
mutations curtas: escrever um segundo manifest no retry; permitir
`verified_none` apesar de revision conflict; tratar `signal_basis_unavailable`
como raw; ler transport não arquivado no materializer; e trocar o provider
atribuído após indisponibilidade.

Todas devem morrer. O critério de aceitação é
`mutations_survived=0`; mutations devem ser restauradas imediatamente após a
prova e não podem alterar o baseline histórico.

## 25. Scope de arquivos da implementação posterior

O menor diff coerente esperado é:

### Novos

```text
advisor/evidence_schema.py
advisor/evidence_archive.py
advisor/evidence_collector.py
advisor/evidence_materializer.py
tests/test_evidence_accumulation.py
.github/workflows/financial-advisor-evidence.yml
```

O calendário permanece dentro de `evidence_collector.py` porque tem um único
consumidor nesta primeira implementação. Uma separação posterior exigiria nova
decisão e não é parte deste escopo.

### Modificados

```text
advisor/cli.py
.github/workflows/financial-advisor-reports.yml
docs/AUTOMATION_SETUP.md
```

`financial-advisor-reports.yml` receberá apenas a emissão/upload do sidecar e
o job de archive sem secrets. O novo workflow será único para coleta,
publicação e maturação de evidence. Nenhum módulo protegido será modificado.
Não é necessário criar um segundo contrato Markdown na implementação, pois
esta spec é a decisão arquitetural e os contratos 3B.1/3B.2 continuam as
autoridades existentes.

## 26. Alternativas rejeitadas

### Actions cache como authority

Caches expiram, podem ser substituídos por chaves de execução e não têm
histórico durável ou revisão auditável. Permanecem somente aceleração.

### Artifacts como authority

Artifacts são transporte, possuem retenção e dependem do workflow. A branch
durável deve sobreviver à expiração de artifacts.

### SQLite em `main`

Misturaria código com estado binário, dependeria do workspace e dificultaria
review, recovery e append-only auditável. SQLite fica como materialização.

### SQLite na branch de evidence

Um arquivo binário opaco não oferece diff, identidade por shard, conflict local
ou recovery seletivo. JSON canônico permite validação e reconstrução.

### External free-tier DB

Introduziria account, quota, indisponibilidade, autenticação e uma authority
externa sem necessidade. A branch existente é gratuita e reproduzível.

### Repositório separado

Exigiria outra autenticação, coordenação de commits e operação de recovery.
O mesmo repositório separa o código por branch sem criar uma nova fronteira de
infraestrutura.

### Adjusted OHLC ou `adjClose`

Esconderia a política corporativa do input, poderia misturar bases e não
provaria a equivalência por horizon. A v1 preserva raw OHLCV e registra proof.

### Policy B de transformação de split

Transformar preços históricos poderia alterar referência, entry e outcomes já
observados, além de criar ambiguidade no dia do split. A policy conservadora
aceita perder coverage para evitar falso outcome.

## 27. Gate da Fase 3B.4

A Fase 3B.4 continua bloqueada. Só poderá ser discutida depois de:

```text
artifact REAL 3B.3.2
+ célula relevante EVALUATION_READY
+ revisão humana
```

Mesmo nesse momento, o artifact atual tem
`calibration_authorized=false`. Esta arquitetura de evidence não muda esse
gate, não altera thresholds e não autoriza calibration.

## 28. Critérios de aceitação da spec

Esta spec está pronta para revisão humana quando mantiver, sem abrir novas
decisões arquiteturais:

- identities completas para os sete tipos;
- um único manifest canônico por `batch_identity`, com retry idêntico somente
  como `no_op` operacional;
- conflict determinístico para revisão sobreposta de corporate action;
- gate de `signal_price_basis_status` antes de `verified_none` de stock/ETF;
- bootstrap orphan, evidence-only e sem criação automática em runtime;
- archive/push confirmado antes de qualquer materialização ou maturação;
- canonical content incluindo proveniência semântica, sem usar `payload_sha256`
  isoladamente;
- assignment de price provider versionado, determinístico, sticky e sem
  fallback silencioso;
- hash e gzip determinísticos;
- branch como única authority;
- intervalo de split inclusivo e signal-date conservador;
- statuses pending/unavailable/conflict distintos;
- failure policy fail-open para report e nonzero para archive failure;
- permissions least privilege e handoff sem secrets;
- writer único com concurrency e retry finito;
- recovery independente de SQLite, cache e artifacts;
- protected modules intactos;
- escopo sem automação semanal, calibration ou 3B.4;
- testes e mutations pré-registrados;
- nenhuma decisão financeira recalculada durante recovery.

Depois da revisão humana desta spec, uma sessão separada poderá produzir um
implementation plan. Nenhuma implementação deve começar antes dessa revisão.
