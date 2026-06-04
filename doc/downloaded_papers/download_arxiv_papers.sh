#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="$ROOT_DIR/doc/downloaded_papers"
mkdir -p "$OUT_DIR"

download_one() {
  local id="$1"
  local slug="$2"
  local pdf="$OUT_DIR/${slug}_${id}.pdf"
  local txt="$OUT_DIR/${slug}_${id}.txt"

  if [[ -s "$pdf" ]] && pdfinfo "$pdf" >/dev/null 2>&1; then
    echo "SKIP valid PDF: $pdf"
  else
    echo "DOWNLOAD arXiv:$id -> $pdf"
    curl -L --fail --retry 3 --retry-delay 2 \
      "https://arxiv.org/pdf/${id}" \
      -o "$pdf"
    pdfinfo "$pdf" >/dev/null
  fi

  if [[ ! -s "$txt" ]]; then
    echo "TEXT $txt"
    pdftotext -layout "$pdf" "$txt"
  fi
}

download_one "2308.11432" "LLM_Autonomous_Agents_Survey"
download_one "2302.04761" "Toolformer"
download_one "2201.11903" "Chain_of_Thought"
download_one "2210.03629" "ReAct"
download_one "2305.04091" "Plan_and_Solve"
download_one "2303.11366" "Reflexion"
download_one "2305.10601" "Tree_of_Thoughts"
download_one "2308.09687" "Graph_of_Thoughts"
download_one "2401.14295" "Demystifying_Thought_Graphs"
download_one "2402.02716" "LLM_Agent_Planning_Survey"
download_one "2501.06322" "Multi_Agent_Collaboration_Survey"
download_one "2412.17481" "LLM_MAS_Application_Survey"
download_one "2503.23037" "Agentic_LLMs_Survey"
download_one "2505.11765" "OMAC"
download_one "2505.16086" "MAS_Textual_Feedback"
download_one "2307.07924" "ChatDev"
download_one "2308.00352" "MetaGPT"
download_one "2308.08155" "AutoGen"
download_one "2303.17760" "CAMEL"
download_one "2304.03442" "Generative_Agents"
download_one "2503.01935" "MultiAgentBench"
download_one "2308.03688" "AgentBench"
download_one "2507.21504" "LLM_Agents_Evaluation_Survey"
download_one "2305.16291" "Voyager"
download_one "2402.14034" "AgentScope"
download_one "2507.08616" "AgentsNet"
download_one "2310.03714" "DSPy"
download_one "2410.10762" "AFlow"
download_one "2408.08435" "ADAS"
download_one "2402.16823" "GPTSwarm"
download_one "2502.02533" "MASS"
download_one "2409.15254" "Archon"
download_one "2510.05592" "AgentFlow"
download_one "2508.07407" "Self_Evolving_Agents_Survey"
download_one "2310.08560" "MemGPT"
download_one "2308.10144" "ExpeL"
download_one "2502.12110" "A_MEM"
download_one "2603.07670" "Memory_for_Autonomous_LLM_Agents"
download_one "2601.01885" "Agentic_Memory_Unified"
download_one "2510.12635" "Memory_as_Action"
download_one "2505.16067" "Memory_Management_Impacts"
download_one "2602.22769" "AMA_Bench"
download_one "2502.03671" "Reasoning_Methods_Survey"
download_one "2508.17692" "Agentic_Reasoning_Frameworks_Survey"
download_one "2502.11221" "PlanGenLLMs"
download_one "2406.11213" "AIOps_Failure_Management_LLM"
download_one "2408.00803" "RCA_Microservices_Survey"
download_one "2407.01710" "Microservice_Failure_Diagnosis_Survey"
download_one "2512.22113" "PRAXIS"
download_one "2502.08224" "Flow_of_Action"
download_one "2602.08804" "RC_LLM"

