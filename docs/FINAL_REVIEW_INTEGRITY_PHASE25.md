# Fase 2.5 — Integridade e proveniência do Final Review

## Objetivo

Esta fase corrige a composição do grade, a seleção de artifacts e a fidelidade do Final Review sem alterar scoring, `classify_asset`, thresholds, gates, risco, estratégia, backtest, sizing, providers ou cache.

O contrato operacional passa a ser:

1. o universo configurado recebe `universe_origin=primary_watchlist`;
2. candidatos adicionais recebem `universe_origin=discovery` depois do scoring;
3. o grade primário considera somente o universo primário;
4. falhas de discovery aparecem em `overall_report_grade`, `discovery_coverage_grade` e warnings, sem rebaixar o grade primário;
5. main e close são selecionados como um par determinístico, do mesmo dia BRT e SHA;
6. o Final Review lê campos estruturados do main e nunca usa texto do close para bloquear o main;
7. `source_decision` preserva a decisão do scoring; labels legadas são apenas `legacy_label`;
8. um `tradeable` do main válido pode chegar ao Final Review como `tradeable_confirmed_from_main`.

## Composição de grade

`evaluate_report_grades()` produz dois eixos:

- `primary_report_grade`: autoridade operacional do main;
- `overall_report_grade`: saúde do pacote completo, incluindo discovery.

O grade primário é bloqueado por modo não-live, sessão primária inválida, benchmark obrigatório inválido, dado primário stale ou execução fora da janela regular quando essa validação é exigida. Discovery degradado não bloqueia o primário.

O runtime atual não declara sessão intraday para benchmarks: SPY/QQQ entram como candles EOD no cálculo de regime. Para não inventar uma sessão nem criar um gate novo, a CLI não envia `required_benchmark_sessions`. O avaliador aceita esse requisito de forma estruturada e possui regressão para bloquear quando um chamador com proveniência intraday real declarar benchmark obrigatório inválido.

Campos emitidos no relatório e no analyst input:

- `primary_report_grade`;
- `overall_report_grade`;
- `primary_market_session`;
- `discovery_market_sessions`;
- `discovery_coverage_grade`;
- `stale_asset_count_primary`;
- `overall_data_warnings`;
- `blocking_reasons`.

## Seleção de artifacts

`advisor.artifact_selection.select_artifact_pair()` exige:

- conclusão `success`;
- branch `main`;
- SHA esperado;
- evento `schedule`, salvo opt-in explícito para manual;
- artifact não expirado e nome exato para o run/type;
- `report_type` coerente com o artifact;
- data BRT do conteúdo coerente;
- main e close no mesmo dia e SHA;
- `main.generated_at <= close.generated_at`.

Sem par válido do dia, a execução falha. O uso de artifact anterior só existe com `AllowStaleDiagnostic`; nesse modo o pacote recebe `artifact_valid=false`, `operational_decision_allowed=false`, `source_date` e `artifact_age_seconds`.

## Parsing estruturado do Final Review

`MainReviewContext` contém run ID, SHA, data BRT, timestamp, data mode, grades, sessão primária, grade de discovery, stale primário, status de provider, validade do artifact e blockers concretos.

`main_blocks_operation()` consulta somente esse contexto. Não faz busca por substrings no Markdown e ignora o conteúdo do close. Logo, textos como `blocked_or_diagnostic`, `not_collected` ou `market_session=closed` em tese/close não contaminam o main.

Main e close mantêm listas de decisões separadas. O close é usado somente na seção de comparação (`main_decision`, `close_decision`, `decision_change`).

## Fidelidade de decisão

O Final Review não chama `score_asset` nem `classify_asset`. Para cada ativo, publica:

- `source_decision`;
- `review_status`;
- `review_reason`;
- `legacy_label`;
- `source_report`;
- `universe_origin`;
- stale e blockers estruturados.

`not_implemented` permanece status do campo. Ele não vira bloqueio genérico. As labels `watch_pending_checks`, `research_only`, `crypto_watch_context`, `crypto_research_only`, `rejected` e `blocked` continuam disponíveis apenas para compatibilidade de apresentação.

## Representação de tradeable

O resultado final é `tradeable` quando existe ao menos um ativo primário com `source_decision=tradeable`, sem stale ou blockers do ativo, e o `MainReviewContext` é operacionalmente válido. Caso contrário, é `no_trade`.

A frase “Nenhum ativo aprovado como tradeable” é condicional ao contador ser zero. A saída informa `tradeable_count` e `tradeable_assets`.

## Limites e segurança

- revisão local baseada em regras;
- `public_equity_executed=false` quando nenhuma execução externa ocorreu;
- sem broker, ordens ou compras automáticas;
- Telegram não foi alterado;
- nenhuma mudança na lógica que cria `AssetDecision` no scoring.

## Validação

Testes dedicados cobrem grade primário/discovery, origem atribuída após scoring, seleção de artifacts, rejeição de SHA/event/date inválidos, stale diagnostic explícito, separação main/close, ausência de bloqueio por substring, preservação de decisão e passagem real de `tradeable`.

Artifacts de evidência:

- `reports/audit/phase25-before-after.json`;
- `reports/audit/phase25-artifact-selection-tests.json`;
- `reports/audit/phase25-decision-preservation.json`.
