# Paper Download Status

Date: 2026-05-06

## Summary

- Valid local PDF files: 68
- Text extracts generated with `pdftotext`: 68
- Invalid legacy file retained for traceability: `AIOps_LLM_Survey_ACMCSUR_3746635.pdf`
- Download scripts:
  - `download_arxiv_papers.sh`
  - `download_open_papers.sh`

## Completed Coverage

The first batch covers all report references with explicit arXiv identifiers that
were reachable during collection. These include the LLM agent foundations,
multi-agent collaboration, workflow/topology optimization, memory/evolution, and
AIOps/RCA arXiv papers cited in the report.

The second batch adds open publisher or author-hosted PDFs for references that
were cited without being in the initial arXiv batch:

- `LLM_MultiAgents_IJCAI_2024_890.pdf`
- `LLM_MultiAgents_IJCAI_2402.01680.pdf`
- `OPTIMA_2410.08115.pdf`
- `OPTIMA_ACL_2025_findings_601.pdf`
- `ReConcile_2309.13007.pdf`
- `ReConcile_ACL_2024_long_381.pdf`
- `Debate_or_Vote_2508.17536.pdf`
- `LLM_Agent_Optimization_Survey_2503.12434.pdf`
- `Insight_Agents_2601.20048.pdf`
- `MULAN_RCA_2402.02357.pdf`
- `AlertInsight_2023_037506.pdf`
- `LatenSeer_SoCC2023.pdf`

## Failed Or Restricted Downloads

These sources did not yield valid PDFs through automated download:

| Target | Attempted Source | Result |
|--------|------------------|--------|
| VAIF SoCC 2021 | `https://par.nsf.gov/servlets/purl/10395746` | connection timeout |
| AIOps LLM Survey ACM CSUR | `https://dl.acm.org/doi/pdf/10.1145/3746635` | ACM returned 403 |
| FaultProfIT ICSE-SEIP 2024 | `https://dl.acm.org/doi/pdf/10.1145/3639477.3639754` | ACM returned 403 |

The file `AIOps_LLM_Survey_ACMCSUR_3746635.pdf` is a legacy bad download and is
not a valid PDF. Do not use it as paper evidence.

## Items Requiring Manual Verification

The report still contains several references labeled as project literature
library manuscripts, industry reports, anonymous preprints, or technical reports.
They should not be treated as peer-reviewed or fully verified papers unless a
valid local source is added later:

- Orq.ai multi-agent evaluation industry guide
- OpenAI self-evolving agents cookbook
- Google DeepMind AlphaEvolve technical report
- Dynamic Cheatsheet
- multimodal long-term-memory agent manuscript
- Hero
- WeRCA
- OpsLLM
- OpsLens
- MetaKube
- STRATUS
- EvoAgentOps
- Revealing Multimodal Causation for RCA
- FSE 2026 RCA manuscript

