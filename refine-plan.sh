#!/usr/bin/bash

function runClaude() {
  local prompt="$1"
  claude --print --dangerously-skip-permissions "Read and the implementation-plan.md first. $prompt"
}

runClaude "Check and correct the implementation plan for CRITICAL and HIGH risk inconsistencies/contradictions."
runClaude "Add any important notes for handling edge cases and exceptions to the implementation plan. Limit yourself to HIGH and CRITICAL risks."
runClaude "Summarize the implementation. Bring it back to the essence. Apply economy of words, but keeping the essential facts. Limit the result maximum 2000 words."