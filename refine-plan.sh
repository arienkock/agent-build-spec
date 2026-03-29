#!/usr/bin/bash

set -e # Exit immediately if a command exits with a non-zero status
set -o pipefail # Return the exit status of the last command in the pipeline that failed

function runClaude() {
  local prompt="$1"
  echo "----------------------------------------"
  echo "Running Claude with prompt: $prompt"
  claude --print --dangerously-skip-permissions "Read and the implementation-plan.md first. $prompt"
}

runClaude "Check it against agent-build-spec.md and the original problem statement to ensure it addresses all requirements and constraints. Focus on HIGH and CRITICAL risks omissions and inconsistencies."
runClaude "Check and correct the implementation plan for CRITICAL and HIGH risk inconsistencies/contradictions."
runClaude "Add any important notes for handling edge cases and exceptions to the implementation plan. Limit yourself to HIGH and CRITICAL risks."
runClaude "Add essential unit and integration test cases for all major components. Focus on HIGH and CRITICAL risk areas and edge cases."

WORD_COUNT=$(wc -w implementation-plan.md)
while [ "$WORD_COUNT" -gt 2000 ]; do
  echo "The implementation plan is too long ($WORD_COUNT words). Please shorten it to 2000 words or less."
  runClaude "Rewrite it to be concise and clear, while retaining all critical information. Limit it to 2000 words or less."
  WORD_COUNT=$(wc -w implementation-plan.md)
done
