#!/usr/bin/bash

set -e # Exit immediately if a command exits with a non-zero status
set -o pipefail # Return the exit status of the last command in the pipeline that failed

LGTM_COUNT=0

function runClaude() {
  local prompt="$1"
  echo "----------------------------------------"
  echo "Running Claude with prompt: $prompt"
  OUTPUT=$(claude --print --dangerously-skip-permissions "Read and the implementation-plan.md first. $prompt. If no changes are needed, respond with 'LGTM'.")
  echo "$OUTPUT"
  HAS_LGTM=$(echo "$OUTPUT" | grep -c "LGTM")
  if [ "$HAS_LGTM" -gt 0 ]; then
    echo "LGTM received. Incrementing count."
    LGTM_COUNT=$((LGTM_COUNT + 1))
  fi

  function claudeNoCount() {
    local prompt="$1"
    echo "----------------------------------------"
    echo "Running Claude with prompt: $prompt"
    claude --print --dangerously-skip-permissions "Read and the implementation-plan.md first. $prompt."
  }
}

for i in {1..7}; do
  LGTM_COUNT=0
  runClaude "Check the implementation plan for completeness and correctness. Focus on HIGH and CRITICAL risks and ensure all necessary steps are included."
  runClaude "Check for any contradictions or inconsistencies in the implementation plan, especially related to HIGH and CRITICAL risks."
  runClaude "Add any important notes for handling edge cases and exceptions to the implementation plan. Limit yourself to HIGH and CRITICAL risks."
  runClaude "Add essential unit and integration test cases for all major components. Focus on HIGH and CRITICAL risk areas and edge cases."
  
  WORD_COUNT=$(wc -w implementation-plan.md)
  while [ "$WORD_COUNT" -gt 2000 ]; do
    echo "The implementation plan is too long ($WORD_COUNT words)."
    claudeNoCount "Rewrite it to be concise and clear, while retaining all critical information. Limit it to 2000 words or less."
    WORD_COUNT=$(wc -w implementation-plan.md)
  done
  
  if [ "$LGTM_COUNT" -ge 4 ]; then
      echo "Received $LGTM_COUNT LGTM responses. Stopping refinement."
      break
  else
      echo "Received $LGTM_COUNT LGTM responses. Continuing refinement."
  fi
done

