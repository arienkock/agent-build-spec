#!/usr/bin/bash

LGTM_COUNT=0

function runClaude() {
  local prompt="$1"
  echo "----------------------------------------"
  echo "Running Claude with prompt: $prompt"
  OUTPUT=$(claude --print --dangerously-skip-permissions "Read and the implementation-plan.md first. $prompt. Correct/address any issues found and UPDATE THE PLAN according to your best judgment. ONLY if no changes are needed, respond with a single 'LGTM'.")
  echo "$OUTPUT"
  HAS_LGTM=$(echo "$OUTPUT" | grep -c "LGTM")
  if [ "$HAS_LGTM" -gt 0 ]; then
    echo "LGTM received. Incrementing count."
    LGTM_COUNT=$((LGTM_COUNT + 1))
  fi
  git add implementation-plan.md
  git commit -m "Refined implementation plan. Prompt: $prompt"
}
function claudeNoCount() {
local prompt="$1"
echo "----------------------------------------"
echo "Running Claude with prompt: $prompt"
claude --print --dangerously-skip-permissions "Read and the implementation-plan.md first. $prompt."
}

for i in {1..7}; do
  LGTM_COUNT=0
  runClaude "Check the implementation plan for completeness and correctness relative to the @agent-build-spec.md specification. Focus on HIGH and CRITICAL omissions and contradictions."
  runClaude "Check for any contradictions or inconsistencies in the implementation plan, especially related to HIGH and CRITICAL risks."
  runClaude "Add any important notes for handling edge cases and exceptions to the implementation plan. Limit yourself to HIGH and CRITICAL risks."
  runClaude "Add essential unit and integration test cases for all major components. Focus on HIGH and CRITICAL risk areas and edge cases."
  
  WORD_COUNT=$(wc -w implementation-plan.md)
  while [ "$WORD_COUNT" -gt 2100 ]; do
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

