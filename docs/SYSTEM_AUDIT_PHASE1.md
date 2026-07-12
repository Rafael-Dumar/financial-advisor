# SYSTEM AUDIT — FASE 1: DADOS E PROVIDERS

Auditoria executada sobre o checkout real em `2026-07-10` BRT. A execução live controlada usou somente `AMD,NVDA,BTC,ETH,HYPE`, `reports/audit/audit.db` e não chamou scoring por padrão. O `gate-analysis.json` final foi gerado com `--trace-gates`, explicitamente opt-in.

## Resumo executivo

| Pergunta | Resposta baseada no código e nos artefatos |
|---|---|
| O sistema usa preço intraday ou fechamento EOD? | Para ações, o caminho principal usa FMP `historical-price-eod/full`; o snapshot transforma a última linha histórica em preço. Para cripto, usa klines Binance ou fallback CoinGecko/Hyperliquid. O artefato marca candles de ações como EOD; não existe cotação live de ações no loader. |
| O rótulo live significa que o preço é live? | Não. `live` significa modo de coleta/configuração, não que cada preço seja intraday. A auditoria final registrou `cache_hit` individualmente e separou `source_data_latest_timestamp` de `response_received_at`. |
| O timestamp é fetch time ou market-data time? | `AssetSnapshot.data_timestamp` é preenchido com `_now_iso()` em `live_loader.py`; portanto é fetch time. A data do candle é market-data time. O cache original não repassa `fetched_at` ao snapshot. |
| News está realmente configurada? | Alpha Vantage está sem chave no ambiente auditado e não foi chamado. SEC Edgar funciona apenas para os símbolos com CIK mapeado e produz filings, não notícias de mercado. Cripto ficou sem news. |
| Earnings está funcionando? | O endpoint FMP de earnings foi chamado, mas `earnings_data_missing` aparece quando não há data futura reconhecida. Não é uma ausência permanente por definição, mas é um gate recorrente quando o payload não entrega uma data válida. |
| Guidance existe? | Não. `stock_snapshot_from_payloads()` sempre adiciona `guidance_recent_not_collected` e define `guidance_recent=None`. |
| Macro existe? | Não no pipeline de dados. `classify_asset()` define `macro_regime="neutral"` e `macro_status="not_collected"`. |
| Sector relative strength existe? | Não. `derive_relative_strength()` usa QQQ ou SPY para ações; não carrega benchmarks setoriais. O scoring deixa `relative_strength_vs_sector=None`. |
| Crypto flow funciona? | Parcialmente. Binance entregou funding, OI history e taker ratio; Hyperliquid entregou funding/OI para HYPE; Coinbase entregou produto público. Binance liquidation orders retornou HTTP 404 para BTC/ETH. HYPE não tem CVD, OI change ou liquidações no código atual. |
| Liquidações vêm de fonte válida? | O código tenta o endpoint público Binance `/fapi/v1/allForceOrders`, mas a execução auditada recebeu HTTP 404. Portanto não houve dado de liquidação válido nesse run; o sistema marcou `liquidations_unavailable`. |
| Public Equity Investing é executado? | Não. `generate_analyst_final_review()` recebe `public_equity_executed=False` por padrão e declara que é uma revisão baseada em regras locais. O módulo reclassifica Markdown; não há chamada a plugin/serviço de Public Equity Investing. |
| Discovery funciona? | A configuração adiciona candidatos, mas `ADVISOR_MAX_STOCKS_PER_RUN=11` corta a lista depois da adição. Como a watchlist base já tem 11 ações, `--include-discovery` não aumenta o universo efetivamente coletado no workflow atual. |
| Deep analysis reduz chamadas? | Não. A coleta e o scoring ocorrem para os snapshots antes de `deep_analysis_candidates` ser limitado aos cinco primeiros em `advisor/cli.py`. O limite reduz apenas a lista reportada. |
| Principal causa do `no_trade` repetido | Dados ausentes/ambíguos entram como limitações de alta severidade: earnings, guidance, news, macro, sector strength, flow incompleto e freshness. `classify_asset()` aplica caps depois do score base e o main/analyst review ainda opera de forma conservadora. |

Conclusão central: o problema não é apenas o texto do relatório. O pipeline mistura dados EOD, payloads de cache e timestamps de coleta sob o mesmo modo `live`, enquanto vários campos exigidos pelos gates não têm implementação completa.

## Escopo e método

Não foram alterados scoring, thresholds, labels, risk engine, analyst-final-review, Telegram, prompts, estratégia, alavancagem ou formato dos relatórios principais. Foram adicionados somente instrumentação opcional, comando de auditoria, artefatos, testes e esta documentação.

Execuções realizadas:

```powershell
.\.venv\Scripts\python.exe -m advisor audit data --no-network --source-db data/advisor.db --output-dir reports/audit
.\.venv\Scripts\python.exe -m advisor audit data --require-live --symbols AMD,NVDA,BTC,ETH,HYPE --trace-gates --audit-db reports/audit/audit.db --output-dir reports/audit
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

O teste completo terminou com **250 testes, 0 falhas**.

## Arquitetura atual

Fluxo observado no código:

```text
GitHub Actions
  -> AdvisorConfig.default() / .env / secrets
  -> LiveDataLoader
       -> provider sources
       -> SQLiteCache / ApiLimiter
       -> AssetSnapshot
  -> derive_market_regimes() / derive_relative_strength()
  -> score_asset()
  -> classify_asset()
  -> render_markdown_report() / analyst-review-input
  -> nightly analyst review local
```

O workflow `.github/workflows/financial-advisor-reports.yml` agenda main às `14:15 UTC` (`11:15 BRT`) e close às `20:15 UTC` (`17:15 BRT`), restaura `data/advisor.db`, executa `advisor report main` ou `advisor report close --from-main`, publica `reports/` e salva o mesmo banco em cache do GitHub.

O caminho normal chama scoring e grava reports/journal. O caminho novo `advisor audit data` não chama `_scan`, `_report`, `_write_reports`, Telegram nem `save_signal_journal`; em live usa `reports/audit/audit.db`. Em no-network, `data/advisor.db` é aberto por URI SQLite read-only e não é criado se estiver ausente.

## Matriz provider → endpoint → dado

Todas as URLs dos artefatos são sanitizadas; chaves aparecem apenas como `present`/`missing`. A auditoria runtime registra a tentativa efetiva em `reports/audit/provider-audit.json`.

| Provider / classe-função | Endpoint sanitizado e método | Auth | Cache / freshness | Dado e campo | Fallback / erro | Impacto |
|---|---|---|---|---|---|---|
| FMP `FmpSource.historical_prices_url()` | `/stable/historical-price-eod/full?symbol=AMD&apikey=REDACTED` GET | FMP key | `prices` / 21600s | candles, latest close, volume | light → Alpha → Yahoo → Stooq; 402/429/error | Sem preço/histórico, snapshot bloqueado ou usa fallback |
| FMP `historical_prices_light_url()` | `/stable/historical-price-eod/light?...` GET | FMP key | `prices` / 21600s | OHLC histórico leve | segue fallback de preço | Pode perder campos do payload full |
| FMP profile/ratios/metrics/growth | `/stable/profile`, `/stable/ratios-ttm`, `/stable/key-metrics-ttm`, `/stable/key-metrics`, `/stable/income-statement-growth` GET | FMP key | `fundamentals` / 86400s | market cap, volume, PE/PEG, historical PE, growth, margin, FCF | payload vazio vira `fundamentals_unavailable` | degrada investment quality e confiança |
| FMP earnings | `/stable/earnings-calendar?symbol=AMD&apikey=REDACTED` GET | FMP key | `earnings` / 43200s | next/last earnings | payload vazio/sem data → `earnings_data_missing` | gate de alta severidade |
| CoinGecko `CoinGeckoSource.markets_url()` | `/api/v3/coins/markets?...` GET | chave configurada; rota pública | `fundamentals` / 86400s | market cap/volume cripto | erro → snapshot cripto falha ou fica incompleto | limita market cap e liquidez |
| CoinGecko `market_chart_url()` | `/api/v3/coins/{id}/market_chart?...&interval=daily` GET | chave configurada | `prices` / 21600s | candles fallback | usado quando Binance klines é restrito | candles são séries diárias agregadas |
| Binance `klines_url()` | `/fapi/v1/klines?symbol=BTCUSDT&interval=1d&limit=500` GET | público | `prices` / 21600s | candles | HTTP 451 → CoinGecko | 451 é temporário/local; sem fallback não há preço |
| Binance funding/OI/taker | `/fapi/v1/fundingRate`, `/fapi/v1/fundingInfo`, `/futures/data/openInterestHist`, `/futures/data/takerlongshortRatio` GET | público | `crypto_flow` / 900s | funding, OI, OI change, CVD proxy | erro opcional vira limitation | flow parcial reduz confiança |
| Binance liquidation | `/fapi/v1/allForceOrders?symbol=BTCUSDT&limit=100` GET | público | `crypto_flow` / 900s | liquidation imbalance | execução auditada: HTTP 404 | `liquidations_unavailable` |
| Hyperliquid `info()` | `https://api.hyperliquid.xyz/info` POST com `candleSnapshot` | público | `prices` / 21600s | HYPE candles | erro obrigatório | sem HYPE histórico |
| Hyperliquid `metaAndAssetCtxs` | mesmo `/info` POST com `metaAndAssetCtxs` | público | `crypto_flow` / 900s | HYPE funding/OI/mark | CVD, OI change e liquidações não implementados | flow HYPE incompleto |
| Coinbase `public_product_url()` | `/api/v3/brokerage/market/products/BTC-USD` GET | público; chave opcional não usada | `prices` / 21600s | preço para Coinbase premium | payload vazio → premium unavailable | premium ausente |
| Alpha Vantage news | `/query?function=NEWS_SENTIMENT&tickers=AMD,...&apikey=REDACTED` GET | Alpha key opcional | `news` / 21600s | news sentiment | sem key → não chamado | equities sem news; cripto sem news |
| Alpha Vantage prices | `/query?function=TIME_SERIES_DAILY_ADJUSTED&symbol=AMD&apikey=REDACTED` GET | Alpha key opcional | `prices` / 21600s | preço fallback | sem key não tentado | fallback indisponível |
| SEC Edgar `submissions_url()` | `https://data.sec.gov/submissions/CIK0000002488.json` GET | público + User-Agent | `news` / 21600s | filings 8-K/10-Q/10-K/20-F/6-K | sem CIK → nenhum evento | filing não equivale a news |
| Yahoo `daily_chart_url()` | `/v8/finance/chart/AMD?range=1y&interval=1d` GET | público | `prices` / 21600s | preço fallback | só chamado após FMP/Alpha falharem | não chamado no run final |
| Stooq `daily_csv_url()` | `/q/d/l/?s=amd.us&i=d` GET texto | público | `prices` / 21600s | preço fallback CSV | parse vazio → sem histórico | não chamado no run final |

As rotas de CoinGecko `/coins/{id}/market_chart` e `/coins/markets` correspondem à documentação oficial; `market_chart` entrega séries de preço, market cap e volume com timestamps UNIX e granularidade dependente de `days`/`interval` ([CoinGecko market chart](https://docs.coingecko.com/reference/coins-id-market-chart), [overview de endpoints](https://docs.coingecko.com/reference/endpoint-overview)). O endpoint Hyperliquid `/info` com `candleSnapshot` é documentado como POST e aceita intervalos incluindo `1d` ([Hyperliquid Info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)). Os endpoints públicos Coinbase usados são compatíveis com o namespace Advanced Trade público; a documentação confirma que endpoints públicos não requerem autenticação e que o produto público pode ser consultado ([Coinbase public products](https://docs.cdp.coinbase.com/coinbase-business/advanced-trade-apis/rest-api)). FMP confirma o prefixo `/stable/` e o endpoint histórico EOD ([FMP quickstart](https://site.financialmodelingprep.com/developer/docs/quickstart)).

## Data lineage

O arquivo `reports/audit/data-lineage.json` registra, por ativo e campo, presença, preview seguro, provider, endpoint, timestamp do dado, timestamp da coleta, idade do cache, fallback e limitation. A auditoria não grava payload integral.

### Ações

| Campo | Fonte/parsing atual | Ausência e efeito |
|---|---|---|
| candles / latest candle / latest close / volume | FMP EOD; fallback Alpha/Yahoo/Stooq; `stock_snapshot_from_payloads()` | vazio → histórico insuficiente, bloqueia |
| live quote | nenhuma função/endpoint no loader | permanentemente ausente no design atual; impede afirmar preço intraday |
| daily change | derivado das duas últimas candles | ausente com menos de duas candles; informativo/technical |
| market cap / average volume | FMP profile | payload sem campos → `fundamentals_unavailable`; limita score |
| PE / PEG / margin | FMP ratios; aliases aceitos pelo parser | missing_*; limita qualidade |
| historical PE | FMP historical key metrics; `_historical_pe()` também deriva de earnings yield | sem série → `missing_pe_history`; limita validação |
| revenue/EPS growth | FMP growth | missing → fundamentos incompletos |
| free cash flow | FMP key metrics/ratios, `_positive_or_none()` | missing → limita qualidade |
| earnings date | FMP earnings calendar; `_next_earnings_date()` | sem data futura → `earnings_data_missing`; gate high |
| guidance | nenhum parser; `guidance_recent=None` | `guidance_recent_not_collected`; gate/limita confiança |
| post-earnings gap | `_post_earnings_gap()` sobre candles EOD e earnings date | sem data/candles → `post_earnings_gap_not_collected` |
| news | Alpha Vantage feed e SEC submissions recentes | Alpha sem key; SEC somente filing/CIIK; ausente limita confiança |
| macro regime | não coletado; scoring fixa `neutral`/`not_collected` | ausência sistêmica; limita confiança |
| benchmark | FMP SPY/QQQ em `load_benchmarks()` | FMP QQQ retornou 402 no run; regime/relative strength degradam |
| sector benchmark / sector RS | nenhum loader de benchmark setorial | ausência permanente por design; limita confiança e ranking |

### Cripto

| Campo | Fonte/parsing atual | Ausência e efeito |
|---|---|---|
| candles / latest candle | Binance klines; CoinGecko market_chart; Hyperliquid candleSnapshot | Binance 451 usa CoinGecko; sem fallback bloqueia preço |
| spot price | última candle do snapshot, não uma quote separada | pode ser EOD/diário para fallback CoinGecko |
| market cap / volume | CoinGecko markets | ausente limita liquidez/market cap |
| RSI / EMA / SMA | derivados das candles por `advisor/indicators.py` | não são payload provider; dependem de histórico |
| funding | Binance funding normalizado para 8h; Hyperliquid hourly × 8 | absent → funding unavailable |
| open interest | Binance OI history; Hyperliquid asset context | HYPE chega no payload, mas não existe campo correspondente em `AssetSnapshot` |
| OI change | `_open_interest_change()` em lista Binance | Hyperliquid retorna `None` por design |
| CVD | `_cvd_proxy()` sobre taker buy/sell volume | não é CVD de trades; para HYPE não há fonte |
| Coinbase premium | produto Coinbase contra última candle de referência | HYPE não usa produto no fluxo atual; absent |
| liquidations | `_liquidation_imbalance()` sobre `allForceOrders` | BTC/ETH receberam 404; HYPE não tem endpoint; absent |
| news | somente Alpha Vantage, chave ausente | não coletado |
| crypto regime | `scan_engine._derive_crypto_regime()` exige BTC/ETH/SOL e histórico | não é executado no data audit padrão; regime normal depende de universo completo |

## Limitações permanentes por design

| Campo | Evidência | Classificação |
|---|---|---|
| `guidance_recent` | `advisor/data_pipeline.py:77-86` adiciona `guidance_recent_not_collected` e fixa `None` | permanente até nova implementação; limita confiança |
| `macro_status` | `advisor/scoring.py:244-247` grava `macro_regime="neutral"`, `macro_status="not_collected"` | permanente; limita confiança |
| sector relative strength | `advisor/scan_engine.py:33-37` escolhe QQQ/SPY; `advisor/scoring.py:251-254` deixa sector RS `None` | permanente por design atual; limita confiança |
| ações intraday/live quote | `advisor/live_loader.py:486-524` usa somente histórico EOD e fallbacks diários | permanente no código atual; risco de rótulo live enganoso |
| HYPE CVD/liquidação/OI change | `hyperliquid_crypto_flow_from_payload()` retorna esses campos como `None` e adiciona limitations | permanente no fluxo Hyperliquid atual |
| Public Equity Investing | `advisor/analyst_review.py:49-82` apenas recebe Markdown e escreve “revisão baseada em regras locais” | permanente nesta integração; não é análise externa |

Limitações temporárias ou dependentes do ambiente:

- FMP 402 para QQQ e Binance 404 para liquidation orders podem ser plano, região, rota ou mudança de provider; a auditoria registra o erro, mas não o corrige.
- Alpha Vantage missing key é configuração temporária, embora sem a chave o comportamento seja determinístico: news não é chamada.
- FMP earnings sem data futura pode variar por símbolo/data; o parser existe, portanto não é correto classificar earnings como sempre inexistente.
- Cache expirado, rate limit e HTTP 429 são estados temporários; o problema permanente é a falta de idade real no snapshot original.

## Auditoria do cache

### Evidência do código

1. `SQLiteCache` armazena `namespace`, `key`, `payload` e `fetched_at`.
2. `SQLiteCache.get_json()` verifica a idade, mas retorna somente o payload.
3. `LiveDataLoader._fetch()` usa esse retorno e incrementa apenas contadores de hit/miss.
4. `load_stock()` e `load_crypto()` passam `data_timestamp=_now_iso()` e `cache_age_seconds=0` para os snapshots.
5. Portanto, um EOD antigo pode ser buscado recentemente, reaparecer como “live” e ter `cache_age_seconds=0` no snapshot, embora a candle mantenha uma data antiga.
6. A instrumentação nova lê `fetched_at` somente para auditoria e não altera o contrato do loader normal.

### Achados de freshness

- `prices=21600` (6 horas) é amplo para uma execução main durante o pregão: um preço EOD ou candle antigo pode ser aceito por várias horas.
- `fundamentals=86400`, `earnings=43200`, `news=21600` e `crypto_flow=900` refletem classes de dado diferentes, mas o snapshot original não carrega a idade por namespace.
- O workflow restaura o banco entre runs via `restore-keys`; close pode reutilizar objetos salvos por main ou por outro run do mesmo branch.
- `reports/audit/cache-audit.json` mantém separadas as linhas do source DB e do audit DB e marca a reutilização entre main/close como não determinável apenas pela linha de cache.

## Gate analysis

O trace opt-in gerado em `reports/audit/gate-analysis.json` executou as funções atuais apenas em memória. No run controlado:

| Ativo | Base | Final | Principais gates/limitações observados |
|---|---|---|---|
| AMD | tradeable | technical_unvalidated | confidence data gap, market not risk-on, recent gap, earnings missing, guidance missing, valuation extreme |
| NVDA | watch_buy | wait | confidence data gap, market not risk-on, earnings missing, guidance missing |
| BTC | wait | wait | confidence data gap, market not risk-on, CVD proxy, liquidations unavailable |
| ETH | tradeable | technical_unvalidated | confidence data gap, market not risk-on, CVD proxy, liquidations unavailable |
| HYPE | watch_buy | technical_unvalidated | confidence data gap, market not risk-on, CVD unavailable, OI change unavailable, liquidations unavailable, Coinbase premium unavailable |

A sequência responsável está em cada ativo no JSON: score base → bloqueio de data gap → cap de confiança/hard gate → high severity cap → stale/technical validation → decisão final. O trace não modifica `AssetDecision`, `signal_journal`, reports ou Telegram.

## Endpoint e schema validation

Resultado runtime do run final:

| Provider | Status | Calls | Cache hits | Erros |
|---|---:|---:|---:|---:|
| FMP | `http_error` | 16 | 15 | 1 — QQQ historical EOD HTTP 402 |
| CoinGecko | `ok` | 3 | 3 | 0 |
| Binance | `http_error` | 11 | 9 | 2 — liquidation orders BTC/ETH HTTP 404 |
| Hyperliquid | `ok` | 2 | 1 | 0 |
| Coinbase | `ok` | 2 | 2 | 0 |
| Alpha Vantage | `missing_key` | 0 | 0 | 0 |
| SEC Edgar | `ok` | 2 | 2 | 0 |
| Yahoo | `not_called` | 0 | 0 | 0 |
| Stooq | `not_called` | 0 | 0 | 0 |

O schema drift final ficou `false` depois que a auditoria passou a distinguir respostas FMP em lista dos envelopes `{"historical": [...]}` aceitos pelo parser. Isso é ajuste da instrumentação de auditoria, não correção do parser nem do provider.

Validações específicas:

- FMP full respondeu 200 para AMD/NVDA no run e 402 para QQQ; o loader não possui batch/live quote.
- Binance klines/funding/OI/taker entregaram dados ou foram reutilizados do audit cache; liquidation orders entregou 404.
- CoinGecko `markets` e `market_chart` entregaram schemas compatíveis e timestamps de séries.
- Coinbase product entregou preço público usado para premium; o código não chama o endpoint público de candles.
- Hyperliquid `candleSnapshot` e `metaAndAssetCtxs` responderam; o parser local só extrai funding/OI/mark.
- Alpha Vantage não pôde ser validado runtime porque a chave estava ausente; o parser e o mapeamento `CRYPTO:BTC` existem em `live_loader.py`.
- SEC respondeu submissions; o parser filtra forms 8-K/10-Q/10-K/20-F/6-K dos últimos 45 dias.
- Yahoo e Stooq são somente fallback e não foram alcançados na execução final.

## Auditoria do GitHub Actions

Arquivo analisado: `.github/workflows/financial-advisor-reports.yml`.

- Horários: 11:15 BRT main e 17:15 BRT close, via 14:15/20:15 UTC.
- Secrets: FMP e CoinGecko são necessários para `--require-live`; Alpha Vantage e Coinbase são opcionais no código. O workflow também injeta parâmetros de capital/risco.
- Cache: `data/advisor.db` é restaurado antes do report e salvo sempre que existe; `restore-keys` permite reuso de um cache anterior do branch.
- Main chama `report main --include-discovery --require-live`; close chama `report close --from-main --require-live`.
- `ADVISOR_MAX_STOCKS_PER_RUN=11` corta os 10 candidatos discovery depois que as 11 ações base já foram adicionadas; o efeito efetivo de discovery para ações é zero.
- O limite FMP por run configurado é 90, enquanto `estimated_live_calls_with_discovery` pode refletir um universo maior antes de outros cortes; a auditoria deve comparar orçamento estimado com `provider_call_counts` real.
- Close pode usar baseline main e cache restaurado de outro run; o código marca `cache_reused_from_main`, mas o cache SQLite não contém uma relação semântica main→close para cada payload.
- O artifact `reports/` é correto para o report, mas não há no workflow atual um artifact separado de provider audit; a execução local live mostrou que esse caminho deve permanecer separado para não alterar main/close.

## Findings

| ID | Severidade | Área | Finding | Evidência | Impacto | Correção sugerida para fase futura |
|---|---|---|---|---|---|---|
| DATA-001 | CRITICAL | Preço/freshness | `live` não significa preço intraday/live | `live_loader.py:_stock_historical_payload`; `data_pipeline.py:100-101`; auditoria mostra candles EOD e timestamp de coleta separado | preço EOD/cache pode ser interpretado como atual durante pregão | separar market-data timestamp de fetch timestamp e adicionar quote intraday; não aplicar agora |
| CACHE-001 | CRITICAL | Cache | snapshot original perde idade real e recebe `cache_age_seconds=0` | `cache.py:get_json()` retorna só payload; loader injeta `_now_iso()`/0 | freshness e gates podem avaliar idade incorretamente | transportar metadata de cache até snapshot; não aplicar agora |
| FMP-001 | HIGH | FMP/benchmark | QQQ falhou com HTTP 402, reduzindo benchmark/regime | `provider-audit.json`, call FMP QQQ; `load_benchmarks()` | macro/relative strength ficam incompletos | validar plano/endpoint e registrar benchmark ausente explicitamente |
| EARN-001 | HIGH | Earnings/guidance | guidance é inexistente por design e earnings sem data vira missing | `data_pipeline.py:77-86` | gate recorrente de alta severidade | implementar fonte/semântica de guidance e melhorar earnings freshness |
| NEWS-001 | HIGH | News | Alpha Vantage depende de chave opcional ausente; SEC cobre filings, não news | `live_loader.py:_news_events_by_symbol()` e `_sec_events_by_symbol()`; provider audit | news não verificada e confiança limitada | configurar provider de news e separar filing/news |
| DATA-002 | HIGH | Macro/setor | macro é sempre not_collected e sector RS não tem benchmark | `scoring.py:244-254`; `scan_engine.py:33-37` | gates repetidos e ranking incompleto | adicionar coleta/lineage de macro e benchmarks setoriais |
| CRYPTO-001 | HIGH | Binance/liquidações | `allForceOrders` retornou 404 para BTC/ETH | provider audit, status HTTP 404 | liquidation imbalance sempre ausente nesse caminho | validar rota/availability e usar fonte pública compatível |
| CRYPTO-002 | MEDIUM | CVD | CVD é proxy de taker buy/sell volume | `data_pipeline.py:_cvd_proxy()`; `scoring.py:81-82` | sinal pode ser interpretado como CVD real | nomear origem/semântica com precisão e, se necessário, coletar trades |
| CRYPTO-003 | HIGH | HYPE flow | Hyperliquid não produz CVD, OI change ou liquidações | `hyperliquid_crypto_flow_from_payload()` | HYPE termina em `technical_unvalidated`/research | implementar séries históricas equivalentes ou manter gate explícito |
| GATE-001 | HIGH | Scoring gates | limitações de providers viram caps sequenciais | `scoring.py:123-207`; gate artifact | ativos com score base positivo não avançam | revisar gates somente em fase posterior, com dados válidos |
| WORKFLOW-001 | MEDIUM | Discovery | max stocks 11 neutraliza discovery de ações | `config.py:138-146`; workflow env line 36 | `--include-discovery` não expande cobertura real | separar limite base/discovery e registrar universo efetivo |
| DEEP-001 | MEDIUM | Orçamento | deep analysis é limitada depois da coleta/scoring completa | `cli.py:157-213` | não reduz calls nem custo de provider | selecionar candidatos antes de chamadas caras |
| CACHE-002 | MEDIUM | Freshness | prices 6h pode ser amplo para main intraday e cache é compartilhado por runs | `config.py:63-69`; workflow cache restore/save | dados antigos reaparecem como atuais | freshness por sessão/namespace e identidade main/close |
| ANALYST-001 | LOW | Analyst review | revisão final é reclassificação local de Markdown | `analyst_review.py:49-82,689-700` | não há análise Public Equity Investing externa apesar do label | integrar explicitamente ou remover a expectativa; não aplicar agora |

Nenhuma correção desses findings foi aplicada nesta fase. A cadeia de fallback e os hooks adicionados servem somente para tornar o comportamento observável.

## Artifacts entregues

- `advisor/audit.py`
- `advisor/cache.py` — inspeção read-only e metadata opcional
- `advisor/http_client.py` — observer HTTP opcional
- `advisor/live_loader.py` — recorder/fallback lineage opcional
- `advisor/cli.py` — `advisor audit data`
- `tests/test_data_audit.py`
- `docs/SYSTEM_AUDIT_PHASE1.md`
- `reports/audit/provider-audit.json`
- `reports/audit/data-lineage.json`
- `reports/audit/cache-audit.json`
- `reports/audit/gate-analysis.json`
- `reports/audit/audit-summary.json`
- `reports/audit/audit.db` — banco isolado da auditoria live

Os arquivos `docs/superpowers/specs/2026-07-10-data-provider-audit-design.md` e `docs/superpowers/plans/2026-07-10-data-provider-audit.md` registram o desenho e o plano aprovados. Não foi criado workflow novo porque a execução live local foi possível; o workflow principal não foi alterado.

## Como executar

Modo estático, sem rede e sem tocar no banco de produção:

```powershell
.\.venv\Scripts\python.exe -m advisor audit data --no-network --source-db data/advisor.db --output-dir reports/audit
```

Modo live controlado, com banco separado:

```powershell
.\.venv\Scripts\python.exe -m advisor audit data `
  --require-live `
  --symbols AMD,NVDA,BTC,ETH,HYPE `
  --audit-db reports/audit/audit.db `
  --output-dir reports/audit
```

Adicionar `--trace-gates` somente quando for necessário observar scoring/classification em memória. Adicionar `--fail-on-schema-drift` para tornar schema drift não-zero depois que os JSONs forem escritos.

## Validação final

```text
250 testes executados
0 falhas
0 alterações em scoring/report/analyst review/Telegram por esta auditoria
network_mode final: live
schema_drift final: false
source DB: somente leitura pelo audit path
audit DB: reports/audit/audit.db
```
