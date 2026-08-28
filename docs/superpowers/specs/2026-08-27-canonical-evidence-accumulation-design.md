# Fase 3B.3.3 — Design Spec: Canonical Evidence Accumulation

**Classificação:** arquitetural
**Status:** design aprovado para revisão humana; nenhuma implementação desta
spec está iniciada.
**Baseline desta decisão:** `HEAD == origin/main ==
4b5e5cfc76ce81863a72e1c3fc984d5bbb330c2f`
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
canonical observation sidecar
    ↓
immutable observation archive
    ↓
daily market/corporate-action collection
    ↓
per-horizon split proof
    ↓
materialized ForwardMarketSeries
    ↓
frozen 3B.2
    ↓
immutable outcome archive
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

- canonical observations;
- market evidence;
- corporate-action evidence;
- horizon proofs;
- forward outcomes;
- pending maturation state.

Nenhuma decisão antiga é recalculada durante recovery. A decisão original é a
observation arquivada; o outcome original é o outcome arquivado. A autoridade
de `main` fornece apenas os tipos e os algoritmos congelados necessários para
validar ou materializar esses dados.

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

1. emissão de um sidecar de observations pelos reports `main` e `close`;
2. publicação desse sidecar como shards imutáveis;
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
O report deve reutilizar a lista de `SignalObservation` construída em memória
pela autoridade existente. Markdown nunca é fonte de observation.

## 6. Identity, payload, provenance e transport

Cada shard tem quatro domínios distintos:

1. `logical_identity`: decide qual fato pode existir uma única vez;
2. `payload`: fato imutável que será recuperado;
3. `provenance`: origem semântica necessária para validar o fato;
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
`transport`. O `canonical_content_sha256` é o hash usado para comparar payloads
e para vincular evidence. O hash embutido de `SignalObservation` ou de
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

## 7. Layout e paths da branch

O layout final é:

```text
evidence/
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
outcome. Um shard representa uma observation; a ordem de rows no sidecar é
apenas transport e é ordenada por `signal_id`.

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
- `evidence_hashes` dos shards de observation, bars e corporate action;
- `proof_status`.

Para stock/ETF, a policy é exatamente
`verified_no_split_in_signal_horizon_v1`, o provider é `alpha_vantage` e o
status terminal é `verified_none` ou `split_in_horizon_unavailable`.

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

A proveniência do shard vincula o `horizon-proof` e os market-bar shards usados,
mas essa proveniência não altera a identidade ou o hash congelado do outcome.
Outcome não é atualizado. Mesmo identity e mesmo payload são
`duplicate_same`; mesmo identity e payload divergente são `conflict`.

Uma revisão posterior de corporate action nunca reescreve outcome. Ela gera
conflict/audit evidence e só uma nova policy/version poderá tratar uma
reevaluation em fase separada.

### 8.6 Manifest

Cada operação de archive produz um manifest determinístico, ainda que o
resultado seja `no_op`. O manifest contém:

- `schema_version`;
- `operation`;
- `batch_identity` e `batch_sha256`;
- lista ordenada de shards;
- logical identities;
- `payload_sha256` e `canonical_content_sha256`;
- hashes/tamanhos dos bytes comprimidos quando aplicável;
- counts por `evidence_type` e status;
- `status`.

`batch_identity` é derivada do operation type e da lista ordenada de hashes de
entrada; não contém horário corrente. `status` é um dos
`committed`, `no_op`, `conflict` ou `rejected`. O manifest é um registro de
transação, não um índice autoritativo: remover manifests não pode remover os
shards nem impedir sua reconstrução por varredura da branch.

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
fechada (`divergent_payload`, `hash_mismatch`, `provider_mixing` ou
`identity_collision`); nenhum texto de exception é aceito.

## 9. Observation archival e sidecar

O report `main` ou `close` deve:

1. executar normalmente;
2. produzir os `AssetDecision` normalmente;
3. construir `SignalObservation` pela autoridade existente;
4. persistir no SQLite exatamente como hoje;
5. exportar as mesmas observations daquele run para um sidecar.

O sidecar é construído diretamente da lista em memória, antes de qualquer
releitura do DB. Ele nunca faz parse do Markdown, nunca recalcula decisão,
nunca reconstrói uma observation posterior e nunca modifica timestamp histórico.

O sidecar de transporte pode ser:

```text
reports/evidence/observations.json.gz
```

Ele contém `schema_version=1.0`, `source_sha`, `run_id`, `report_type`, a lista
completa de rows canônicas ordenada por `signal_id` e, quando presente, um
sidecar de proveniência ligado por `signal_id + observation_hash`.

O sidecar de proveniência pode registrar:

- `signal_price_provider`;
- `signal_input_hash`;
- `signal_price_basis_observed`;
- `collection_provenance`.

Esses dados são metadata da observação, não parte de `SignalObservation` e não
entram em `observation_hash`. `signal_input_hash`, quando emitido, é o
SHA-256 do snapshot de input sanitizado efetivamente passado ao builder da
observation; não é inferido a partir do Markdown. Se o snapshot não tiver
representação estável disponível, o campo fica `null` com status explícito
`unavailable`, sem inventar um hash. `signal_price_basis_observed` só pode ser
`raw_ohlcv` quando a proveniência do snapshot declarar isso; nunca se presume
que o provider forneceu adjusted prices.

O archive job explode o sidecar em um shard por observation e valida a linha
contra `signal_id`, `observation_hash` e o schema 3B.1. A serialização JSON do
sidecar usa arrays para `reason_codes`; a representação textual usada pela
tabela SQLite não vira a autoridade canônica.

Ativar ou desativar o archive não pode mudar bytes de report, `AssetDecision`,
risk, sizing, report grade ou Telegram. Um erro de sidecar somente produz
diagnóstico operacional e faz o archive job falhar; o report permanece o
resultado válido já produzido.

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
mesmo que cada resposta isolada seja válida. O collector deve rejeitar a
mistura; não pode aplicar fallback de outro provider sem uma policy explícita
de série que não existe na v1.

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
- zero events explicitamente comprovados resulta em `verified_none`;
- feed ausente nunca é interpretado como zero events;
- split com `effective_date == signal_market_date` invalida todos os horizons;
- não se tenta descobrir se `ideal_entry` já estava pós-split;
- a perda de coverage é aceita para evitar falso outcome.

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
3. stock com bars suficientes mas sem corporate-action coverage válido:
   `feed_unavailable`;
4. stock com split inclusivo: `split_in_horizon_unavailable`;
5. stock com zero split comprovado: `verified_none`;
6. crypto com N bars completas: `not_applicable` para split e basis raw.

`pending`, `feed_unavailable` e `market_data_unavailable` são estados
recalculáveis da materialização. Eles não são ausência definitiva e não
geram proof terminal. `split_in_horizon_unavailable` é terminal para a policy
v1 porque o evento é evidence durável; `verified_none` também é terminal para
aquele horizon porque seu proof específico foi escrito.

Exemplo de split entre a quinta e a décima bar:

| Horizon | Resultado |
| --- | --- |
| 5 | `verified_none`, se a janela inclusiva de 5 não contém split |
| 10 | `split_in_horizon_unavailable` |
| 20 | `split_in_horizon_unavailable` |
| 40 | `split_in_horizon_unavailable` |

Um proof de h5 já publicado não é alterado quando h10 amadurece. Cada proof
carrega somente seus próprios N bars e hashes.

## 13. Materialização e chamada da 3B.2

Para uma observation com horizons aprovados, o materializer:

1. lê somente shards válidos da `advisor-evidence` e transportes recém-coletados;
2. valida observation, bars, provider único e proofs;
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
6. aceita somente os outcomes cujos proofs correspondentes são
   `verified_none` ou crypto `not_applicable`;
7. emite proof/outcome transports para o único writer.

O JSON local é materialização temporária, não autoridade, e não contém URLs,
headers, API keys ou paths do provider. Se um horizon tem split ou feed
indisponível, ele não é entregue ao evaluator como outcome válido. Se o
evaluator devolver um outcome para horizon sem proof, isso é erro de integração
e o batch é rejeitado.

O materializer pode reconstruir uma SQLite vazia para consumo local, mas essa
SQLite é derivada dos shards e não pode ser lida como fonte histórica. A
persistência de outcome usa a identidade/hash da 3B.2 e não implementa uma
segunda fórmula.

## 14. Outcome maturation

O orchestration de maturação é:

1. enumerar observations canônicas, nunca `signal_journal`;
2. determinar bars e `horizon_end_date` por sessões reais;
3. validar corporate-action proof para stock/ETF;
4. construir o JSON local aceito pela 3B.2;
5. chamar a autoridade congelada;
6. receber outcomes e horizons pending;
7. arquivar somente outcomes novos e proofs terminais novos;
8. reconstruir/atualizar apenas a materialização SQLite local depois de archive
   válido;
9. emitir counters sanitizados.

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
| `conflict` | mesma identity tem conteúdo divergente | não sem revisão explícita |
| `verified_none` | zero splits foi comprovado no intervalo | terminal do horizon |

Indisponibilidade transitória não vira `verified_none`. Weekend, holiday, candle
not yet published e `duplicate_same` não são falhas de archive.

## 16. Branch writer, atomicidade e concurrency

Somente `evidence_archive.py`, executado pelo writer job, pode publicar na
branch `advisor-evidence`. A branch será criada somente em implementação
autorizada e conterá evidence, não código. Ela não é checkout padrão do app.

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

Um batch contendo apenas `duplicate_same` pode gerar manifest `no_op`; não há
overwrite. A branch mantém todos os shards anteriores.

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
necessários. Ele produz report e observation sidecar, mas não publica na branch.

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
secrets, publica os shards e executa materialização/maturação somente sobre
evidence já disponível. O mesmo componente writer publica todas as categorias;
nenhum job com secret de provider pode escrever na branch.

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
python -m advisor evidence archive --input-path <transport>
python -m advisor evidence collect --asset-scope <all|stocks|crypto> --output-dir <dir>
python -m advisor evidence materialize --evidence-root <dir> --output-dir <dir>
python -m advisor evidence mature --evidence-root <dir> --output-dir <dir>
```

`archive` é o único que escreve na branch. `collect` pode usar providers e
somente produz transportes. `materialize` reconstrói dados e JSON local.
`mature` chama a 3B.2 e produz proof/outcome transports; não faz push direto.
SQLite, quando usado pelos dois últimos, é explicitamente output reconstruível,
nunca input de autoridade.

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

1. produzir observations, market bars, corporate actions, proofs e outcomes
   sintéticos porém canônicos;
2. arquivar a fixture na branch de evidence do repositório descartável;
3. registrar logical IDs, canonical hashes, byte hashes e counts originais;
4. remover a SQLite temporária;
5. remover caches operacionais e artifacts de transporte;
6. iniciar uma materialização vazia;
7. ler somente a fixture/store de `advisor-evidence`;
8. validar todos os shards e manifests;
9. reconstruir observations e market/corporate evidence;
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

O teste futuro deve cobrir estas propriedades e invariantes:

1. mesma identity + mesmo payload resulta em no-op;
2. mesma identity + payload divergente resulta em conflict;
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
