---
name: tool-invocation-rules
description: |
  Enforces correct MCP tool invocation format. Ensures the agent uses
  function-calling for every action instead of generating Python, shell,
  or pseudocode. Applies to all tool interactions. [STRICT]
metadata:
  author: red-hat
  version: "1.0"
---

## Tool invocation format [STRICT]

Capabilities are exposed only as MCP tools with registered names (e.g.,
vulnerability__get_system_cves, inventory__list_hosts). You MUST invoke tools through
the model's function-calling mechanism: each action is a separate tool call with JSON
arguments matching the tool schema. Do NOT output Python, shell scripts, OpenAPI client
code (e.g., default_api.*), or pseudocode loops to perform tool actions — those forms
are not executed here. For paginated APIs, issue successive tool calls in sequence,
advancing pagination parameters per each tool's schema until the response indicates
no further pages or a partial/empty page; do not express pagination as executable code.
