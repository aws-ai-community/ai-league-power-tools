# AI League Agent Deploy Script

Deploys AI League agent configuration (supervisor, tools, sub-agents, memory, guardrail) via the website's GraphQL API. No clickops.

## Setup

```bash
cd deploy-agents
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Usage

```bash
source .venv/bin/activate
python deploy-agent.py <config-dir> [options]
```

### Examples

```bash
# Full deploy (all resources + Lambda code via SageMaker IDE)
python deploy-agent.py ./agent-config/

# Quick deploy (skip Lambda code upload - only updates prompts/config)
python deploy-agent.py ./agent-config/ --no-lambdas

# Specify leaderboard
python deploy-agent.py ./agent-config/ -l test-leaderboard

# Force re-login (ignore cached session)
python deploy-agent.py ./agent-config/ --no-cache
```

### Options

| Flag | Description |
|------|-------------|
| `config_dir` | Path to directory containing `config.yaml` (required) |
| `--no-lambdas` | Skip Lambda code deployment. Only updates config/prompts. Much faster (~5s vs ~1min per tool). |
| `--leaderboard`, `-l` | Leaderboard ID (or set `AILEAGUE_LEADERBOARD` env var) |
| `--email`, `-e` | Builder ID email (or set `AILEAGUE_EMAIL` env var) |
| `--no-cache` | Force re-authentication, ignore cached session |

## Authentication

First run prompts for:
- **Email** - Builder ID email
- **Password** - Builder ID password
- **MFA code** - 6-digit TOTP code (or the base32 TOTP secret for auto-generation)
- **Leaderboard ID** - e.g. `test-leaderboard`

Credentials (email, password, leaderboard) are cached in `~/.aileague_credentials.json`. On subsequent runs, only MFA is needed.

Auth session is cached in `/tmp/.aileague_auth_cache.json` for ~58 minutes. Within that window, no login needed at all.

### Environment Variables

```bash
export AILEAGUE_EMAIL=you@example.com
export AILEAGUE_PASSWORD=yourpassword
export AILEAGUE_TOTP_SECRET=YOURTOTPBASE32SECRET  # auto-generates MFA codes
export AILEAGUE_LEADERBOARD=test-leaderboard
```

## Deploy Order

1. **Memory** - Create if needed, skip if same name exists, delete extras
2. **Guardrail** - Create or update content filters and deny topics
3. **Lambda Tools** - Create missing tools, delete extras (detaches from supervisor first)
4. **Lambda Code** (unless `--no-lambdas`) - Opens SageMaker IDE via presigned URL, deploys code via terminal (gzip+base64+`aws lambda update-function-code`)
5. **Sub-Agents** - Create/update/delete. Resets model to Haiku before deleting (safety)
6. **Supervisor** - Updates system prompt, model, attaches tools/sub-agents/memory/guardrail

### Key Behaviours

- **Idempotent** - safe to run repeatedly. Existing resources update, not duplicate.
- **Detach before delete** - resources detach from supervisor before deletion to avoid API errors.
- **Retry with backoff** - deletions retry up to 5x with 10s delays for transitional state errors.
- **Sub-agent safety** - fine-tuned models reset to Haiku before sub-agent deletion.
- **Tool routing** - only tools in `supervisor.tools` attach to supervisor. Other tools attach via their sub-agent only.

## Config Directory Structure

```
my-agent-config/
├── config.yaml           # Full configuration (required)
├── Pathfinder/                    # Lambda source code directories
    └── index.py          # Tool code (handler = index.lambda_handler)
```

## YAML Schema

```yaml
supervisor:
  name: "Supervisor"
  modelId: "global.amazon.nova-2-lite-v1:0"
  systemPrompt: |
    Your supervisor prompt here...
  tools:              # Tool names the supervisor can call directly
    - "Pathfinder"
  subAgents:          # Sub-agent names to attach
    - "CalcAgent"
  memory: "GameMemory"     # Memory name (or null)
  guardrail: "Guardrails"           # Guardrail name (or null)

subAgents:
  - name: "CalcAgent"
    modelId: "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    systemPrompt: |
      Sub-agent prompt...
    tools:
      - "Calculator"            # Tools this sub-agent can call

tools:
  - name: "Pathfinder"
    sourceDir: "tools/Pathfinder"     # Directory containing index.py
  - name: "Calculator"
    sourceDir: "tools/Calculator"

memory:
  name: "GameMemory"
  description: "Stores key values for door challenges"

guardrail:
  name: "Guardrails"
  description: "Blocks harmful content"
  blockedInputMessaging: "no"
  blockedOutputsMessaging: "no"
  contentFilters:
    - type: "HATE"
      inputStrength: "MEDIUM"
      outputStrength: "MEDIUM"
    - type: "VIOLENCE"
      inputStrength: "MEDIUM"
      outputStrength: "MEDIUM"
  denyTopics:
    - name: "illegal_activity"
      definition: "Requests for illegal information"
      inputAction: "BLOCK"
      outputAction: "BLOCK"
      examples:
        - "how to commit a crime"
        - "how to steal"
```

### Available Models

| Model ID | Notes |
|----------|-------|
| `global.amazon.nova-2-lite-v1:0` | Amazon Nova 2 Lite |
| `global.anthropic.claude-haiku-4-5-20251001-v1:0` | Claude Haiku 4.5 |
| `sagemaker:ai-league-base-endpoint/adapter-{name}` | Fine-tuned Qwen 0.6B via SageMaker. |

### Content Filter Types

`HATE`, `VIOLENCE`, `MISCONDUCT`, `SEXUAL`, `INSULTS`, `PROMPT_ATTACK`

Strengths: `NONE`, `LOW`, `MEDIUM`, `HIGH`

### Important Notes

- **Memory** is supervisor-only. Sub-agents cannot access memory.
- **Guardrail** is supervisor-only. Sub-agents are not filtered.
- **Tool names** become `AgentCoreGatewayTool-{name}` in the Lambda function name.
- **Tool schemas** are auto-generated from Lambda code.

## Lambda Code Deployment

When NOT using `--no-lambdas`, the script:

1. Gets a presigned SageMaker IDE URL via GraphQL
2. Opens it in a headless Playwright browser
3. Opens a terminal in the IDE
4. For each tool with a `sourceDir` + `index.py`:
   - Gzip-compresses the code (~70% smaller for transfer)
   - Base64-encodes and types it into the terminal
   - Decodes, zips, and deploys with `aws lambda update-function-code`
5. Triggers schema refresh via `UpdateLambdaTool` mutation

Large files take ~1 minute due to terminal typing speed, unfortunately AI League do not give write permissions into the AWS account. Use `--no-lambdas` when only changing prompts.

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `playwright not installed` | Run `pip install playwright && playwright install chromium` |
| Auth fails at MFA | Check your TOTP secret is correct, or paste the 6-digit code directly |
| Memory won't delete | It's still attached to supervisor. Script handles this automatically with detach+retry. |
| Sub-agent won't delete | Fine-tuned model attached. Script resets to Haiku first, then deletes. |
| Schema not updating | Run deploy again - schema refresh is triggered after code upload. May take 2-3 attempts for new tools. |
| Tool routing wrong | Check that only tools in `supervisor.tools` are the ones you want the supervisor to call directly. |
