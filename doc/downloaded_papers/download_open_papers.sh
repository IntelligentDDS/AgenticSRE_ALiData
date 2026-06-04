#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="$ROOT_DIR/doc/downloaded_papers"
mkdir -p "$OUT_DIR"

download_url() {
  local url="$1"
  local slug="$2"
  local pdf="$OUT_DIR/${slug}.pdf"
  local txt="$OUT_DIR/${slug}.txt"

  if [[ -s "$pdf" ]] && pdfinfo "$pdf" >/dev/null 2>&1; then
    echo "SKIP valid PDF: $pdf"
  else
    echo "DOWNLOAD $url -> $pdf"
    curl -L --fail --retry 2 --retry-delay 2 --connect-timeout 15 --max-time 120 \
      -A "Mozilla/5.0" \
      "$url" \
      -o "$pdf"
    if ! pdfinfo "$pdf" >/dev/null 2>&1; then
      echo "INVALID PDF: $pdf"
      return 1
    fi
  fi

  if [[ ! -s "$txt" ]]; then
    echo "TEXT $txt"
    pdftotext -layout "$pdf" "$txt"
  fi
}

try_download_url() {
  local url="$1"
  local slug="$2"

  if ! download_url "$url" "$slug"; then
    echo "$slug $url" >> "$OUT_DIR/download_failures.log"
  fi
}

rm -f "$OUT_DIR/download_failures.log"

try_download_url "https://arxiv.org/pdf/2402.01680" "LLM_MultiAgents_IJCAI_2402.01680"
download_url "https://www.ijcai.org/proceedings/2024/0890.pdf" "LLM_MultiAgents_IJCAI_2024_890"
try_download_url "https://arxiv.org/pdf/2410.08115" "OPTIMA_2410.08115"
download_url "https://aclanthology.org/2025.findings-acl.601.pdf" "OPTIMA_ACL_2025_findings_601"
try_download_url "https://arxiv.org/pdf/2309.13007" "ReConcile_2309.13007"
download_url "https://aclanthology.org/2024.acl-long.381.pdf" "ReConcile_ACL_2024_long_381"
try_download_url "https://arxiv.org/pdf/2508.17536" "Debate_or_Vote_2508.17536"
try_download_url "https://arxiv.org/pdf/2503.12434" "LLM_Agent_Optimization_Survey_2503.12434"
try_download_url "https://arxiv.org/pdf/2601.20048" "Insight_Agents_2601.20048"
try_download_url "https://arxiv.org/pdf/2402.02357" "MULAN_RCA_2402.02357"
download_url "https://cdn.techscience.cn/files/csse/2023/TSP_CSSE-46-2/TSP_CSSE_37506/TSP_CSSE_37506.pdf" "AlertInsight_2023_037506"
download_url "https://www.cs.cmu.edu/~juncheny/publication/socc23-latenseer.pdf" "LatenSeer_SoCC2023"
try_download_url "https://par.nsf.gov/servlets/purl/10395746" "VAIF_SoCC2021"

try_download_url "https://dl.acm.org/doi/pdf/10.1145/3746635" "AIOps_LLM_Survey_ACMCSUR_3746635_official"
try_download_url "https://dl.acm.org/doi/pdf/10.1145/3639477.3639754" "FaultProfIT_ICSE_SEIP_2024"

if [[ -s "$OUT_DIR/download_failures.log" ]]; then
  echo "Some downloads did not yield valid PDFs:"
  cat "$OUT_DIR/download_failures.log"
fi
