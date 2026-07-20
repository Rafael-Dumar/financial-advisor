# Phase 3A.2.3 Fechamento dos Bloqueadores do Framework Forense

- Ocorrências: `original_occurrence_set_unavailable`; o pacote não contém o artefato runtime original. Nenhuma ocorrência sintética é promovida.
- Trace: `runtime_trace_unavailable`; o detector `same_axis_later_weaker_or_override` existe, ordena por `sequence` e é aplicado apenas às fixtures controladas.
- News: decision, capability e collection são resolvidos por marcadores semânticos dos blocos reais do relatório; `quote_status` não define escopo. Locators são 1-based e apontam para a linha bruta.
- Crypto: `funding`, `open_interest_current`, `open_interest_change`, `cvd`, `premium` e `liquidations` aparecem sempre para ETH, SOL, HYPE e BTC; ausência é `null/unavailable` com `source=null`.
- Replay: paridade com `classify_asset` é 0/15; os 45 contrafactuais são `null/unavailable` porque os inputs equivalentes não existem. Nenhum simulador paralelo é usado.
- Mutações: um único `workspace_base` é criado e manifestado antes do control run; o control run e cada mutação usam clones idênticos desse mesmo estado inicial, com o mesmo comando, interpretador e `PYTHONPATH`. O artefato só é escrito quando 9/9 falham pelo teste esperado, os hashes dos alvos mudam e os manifests pré-mutação coincidem.
- Determinismo: duas gerações isoladas são normalizadas para dados estruturados e exigem hashes byte a byte idênticos; duração, caminhos temporários, endereços de memória, PIDs e line endings não são persistidos.
- Sample quality: o valor é um wrapper observado (`value`, `raw_value`, `provenance`, `source`), validado por snippets brutos independentes para AMD, ETH, HIMS, MSFT e USAR. HIMS é derivado de `hims_source_raw.md`, sem conclusões pré-preenchidas.
- HIMS bruto: `tests/fixtures/phase3/hims_source_raw.md` é exatamente o trecho das linhas 839–847 do relatório original, comparado linha a linha no teste independente.
- Contrato de ocorrências: `phase3-original-suspected-occurrences.json` declara diretamente status indisponível, contagem `null`, reconciliação falsa e duplicidades zero; não há recuperação artificial.
- Estado forense: `runtime_trace_unavailable`, `shadowed_count=null`, `classify_asset` parity `0/15`, zero contrafactuais calculáveis e `45` indisponíveis. Esses valores não demonstram ausência de duplicidades ou shadowing.
- Replay: o run antigo não contém inputs equivalentes suficientes para replay fiel; portanto não produz contrafactuais financeiros nem recomendações de política.
- Próximo passo: Runtime Scoring Observability. Esta fase não inicia observabilidade, ledger nem alteração de política.
- Integridade fixa: `advisor/scoring.py` SHA-256 `16B7A0A4C93ECD0E633B7DF560C585F10398426EDA8861D7D06C38E3449BAFAD`; relatório SHA-256 `410DCADEA7EB22DCB71C53A45B05E1C70484B038D87565847C966351805D4E8A`.
- Rede: não utilizada. Scoring não alterado.
