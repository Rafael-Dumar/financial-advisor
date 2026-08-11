# FASE 3B.2 — Deterministic Forward Outcome Contract

Status: implementação isolada, sem integração com `scan`, `report`, Final
Review, Telegram ou workflows. A tabela `signal_observations` continua sendo
a autoridade imutável do que o advisor decidiu; esta camada registra somente o
que ocorreu nas barras diárias posteriores.

## Policy 1.0

`schema_version = "1.0"` e `evaluation_policy_version = "1.0"`.

Uma barra é elegível somente quando `bar.date > signal_market_date`. A data do
sinal é a data civil obtida de `signal_timestamp_utc` usando o timezone
persistido na observation: `America/New_York` para stock e `UTC` para crypto.
O helper Eastern é determinístico e implementa as regras modernas de DST sem
dependência de tzdata externo.

Os únicos horizontes são 5, 10, 20 e 40 barras completas. Cada horizon usa a
N-ésima barra elegível, em ordem crescente de data. Se não houver N barras, o
horizon fica `pending` e nenhuma row parcial é criada. Barras após a 40ª não
participam do outcome de 40 barras.

Para horizon N:

```text
forward_return_pct = (close_da_N_ésima_barra / ideal_entry) - 1
mfe_pct = max(0, max(high / ideal_entry - 1 nas N barras))
mae_pct = min(0, min(low / ideal_entry - 1 nas N barras))
```

`ideal_entry` é persistido como `reference_price`. A semântica é
`reference_price_observation_not_execution`, e `entry_semantics` permanece
`reference_close_not_fill`. Não há custo, slippage, sizing, fill, realized R,
P&L, position return ou decisão de trade.

## Input local

O único input de mercado é JSON local:

```json
{
  "schema_version": "1.0",
  "assets": {
    "AMD": {
      "asset_type": "stock",
      "provider": "fixture",
      "price_basis": "unknown",
      "candles": [
        {"date":"2026-08-09","open":100,"high":101,"low":99,"close":100,"volume":1000}
      ]
    }
  }
}
```

Não há network, provider client, URL, querystring, header, Authorization,
API key, path ou exception na row. `provider` e `price_basis` são bounded e
sanitizados; valores que contêm marcadores proibidos tornam-se `unknown`.

Cada candle exige `date` em `YYYY-MM-DD`, OHLCV numérico finito, preços
positivos, volume não negativo e:

```text
low <= open <= high
low <= close <= high
low <= high
```

Datas duplicadas são `invalid_input`; a ordem de entrada é irrelevante e a
série é canonicalizada por data crescente.

CLI:

```text
python -m advisor outcomes evaluate --input-path <file> --db <db>
```

O stdout válido é um JSON compacto com:
`observations_considered`, `signals_written`, `signals_duplicate_same`,
`signals_conflict`, `signals_pending`, `signals_unavailable` e
`outcomes_written`. Input inválido retorna exit code 2; conflito ou
indisponibilidade por signal retorna exit code 1 depois de continuar os demais
signals; execução válida apenas com writes, duplicatas ou pending retorna 0.

## Outcomes e hashes

`outcome_id` é o SHA-256 do JSON canônico UTF-8, com exatamente:

```json
{
  "evaluation_policy_version":"1.0",
  "horizon_bars":5,
  "observation_hash":"...",
  "schema_version":"1.0",
  "signal_id":"..."
}
```

O JSON usa `sort_keys=True`, separadores compactos, UTF-8 e `allow_nan=False`.

`asset_bars_json` contém exatamente as N candles usadas, em data crescente,
com chaves ordenadas e sem valores NaN. `asset_bars_hash` é o SHA-256 dos
bytes desse JSON.

`outcome_hash` cobre todo o conteúdo imutável do outcome: outcome identity,
schema/policy, binding do signal, observation hash, asset e asset type,
decision/evaluation role, timezone e market dates, horizon, reference price e
semânticas, bars JSON/hash, forward return, MFE, MAE, níveis, touches e
primeiros eventos, flags de ambiguidade, threshold alternativo e metadata
sanitizada de provider/price basis. Somente `outcome_hash` e
`persisted_at_utc` ficam fora do payload hash.

## Barriers e alternativa

Cada candle é escaneada independentemente contra `stop`, `target_2r` e
`target_3r`. O scan nunca para no primeiro evento. São persistidos os três
booleans e, para cada nível, o primeiro `bar` 1-based e a primeira `date`.
Não existe `result_final`.

Se uma candle tiver `low <= stop` e `high >= target_2r`,
`same_bar_stop_target_2r = true`. A mesma regra vale para 3R. Nenhuma ordem
intraday é inferida.

Quando existe `alternative_entry`, `low <= alternative_entry` marca somente
`alternative_entry_threshold_reached` e seu primeiro bar/date. Isso não é
fill nem ativação de trade. Sem alternativa, o threshold é `false` e os
campos do primeiro evento são `null`.

Todos os `evaluation_role` canônicos recebem a mesma fórmula matemática,
inclusive `observational_avoid` e `observational_blocked`; o retorno nunca é
invertido.

## SQLite, append-only e replay

`SQLiteCache` cria `signal_forward_outcomes` sem alterar `signal_observations`
ou `signal_journal`. A tabela contém, além da identidade/hash, binding,
evaluation role, horizon, market dates, reference/semantics, bars JSON/hash,
forward return, MFE/MAE, levels, first touches, ambiguity flags, alternative
threshold e metadata sanitizada. `horizon_bars` é fechado em `(5, 10, 20, 40)`.

A identidade lógica é:

```text
(signal_id, observation_hash, evaluation_policy_version, horizon_bars)
```

`outcome_id` é primary key e a identidade lógica é unique. A existência do
`signal_id` em `signal_observations` é validada explicitamente antes do
insert. Triggers `BEFORE UPDATE` e `BEFORE DELETE` fazem `RAISE(ABORT)`.

Uma tentativa para um signal é uma transação única:

- identity nova: `written`;
- mesma identity e mesmo `outcome_hash`: `duplicate_same`, no-op;
- mesma identity e hash divergente: `conflict`, rollback integral dos novos
  horizons desse signal e preservação das rows antigas;
- storage inválido/indisponível: `unavailable`, rollback.

Signals diferentes têm transações independentes. Reproduzir a observation, a
mesma policy e as mesmas candles produz os mesmos IDs/hashes; revisar uma
candle histórica não sobrescreve o passado.

## Exclusões desta fase

Não há benchmark, `benchmark_return`, alpha, calibração, threshold ótimo,
confidence nova, expected value novo, sizing, recomendação, execução simulada,
`realized_r`, realized P&L, fill, actual entry/exit, win/loss de trade,
profit factor ou strategy expectancy. Também não há integração automática ou
agendamento de aquisição histórica. A 3B.3 não faz parte deste contrato.
