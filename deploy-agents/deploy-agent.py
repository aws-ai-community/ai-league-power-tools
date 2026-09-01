#!/usr/bin/env python3
"""
deploy-agent.py — Deploy AI League agent config via the website GraphQL API.

Authenticates via Builder ID (email + password + TOTP), captures auth headers,
then reads config.yaml and creates/updates all resources.

Usage:
    python deploy-agent.py ./agent-config/
    python deploy-agent.py ./agent-config/ --leaderboard test-leaderboard

Dependencies:
    pip install playwright pyotp requests pyyaml
    playwright install chromium
"""

import argparse
import json
import os
import sys
import time
import getpass
import tempfile
from pathlib import Path

import requests
import yaml

# Cache file for auth headers (avoid re-login within session)
AUTH_CACHE_FILE = Path(tempfile.gettempdir()) / ".aileague_auth_cache.json"
AUTH_CACHE_TTL = 3500  # ~58 minutes (tokens typically expire at 1 hour)

# Credentials cache (persists across sessions, like aws configure)
CREDS_CACHE_FILE = Path.home() / ".aileague_credentials.json"

GRAPHQL_URL = "https://graphql.aileague.aws.dev/graphql"
AUTH_SESSION_URL = "https://api.aileague.aws.dev/auth/session"
BASE_URL = "https://aileague.aws.dev"


# =============================================================================
# Auth
# =============================================================================

def load_cached_auth():
    """Load cached auth headers if still valid."""
    if not AUTH_CACHE_FILE.exists():
        return None
    try:
        data = json.loads(AUTH_CACHE_FILE.read_text())
        if time.time() - data.get("timestamp", 0) < AUTH_CACHE_TTL:
            # Quick validation — try a lightweight query
            headers = data["headers"]
            resp = requests.post(GRAPHQL_URL, json={
                "query": "query { __typename }"
            }, headers=_build_headers(headers), timeout=10)
            if resp.status_code == 200:
                print("[auth] Using cached session")
                return headers
    except Exception:
        pass
    return None


def save_cached_auth(headers):
    """Save auth headers to cache file."""
    AUTH_CACHE_FILE.write_text(json.dumps({
        "headers": headers,
        "timestamp": time.time(),
    }))


def load_saved_credentials():
    """Load saved credentials (email, password, leaderboard) from disk."""
    if not CREDS_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CREDS_CACHE_FILE.read_text())
    except Exception:
        return {}


def save_credentials(creds):
    """Save credentials to disk."""
    CREDS_CACHE_FILE.write_text(json.dumps(creds, indent=2))


def prompt_with_default(prompt_text, default=None, secret=False):
    """Prompt user with a default value shown in brackets. Empty input uses default."""
    if default:
        display = f"{prompt_text} [{default}]: "
    else:
        display = f"{prompt_text}: "

    if secret:
        if default:
            display = f"{prompt_text} [****]: "
        value = getpass.getpass(display)
    else:
        value = input(display).strip()

    return value if value else default


def authenticate(email=None, password=None, totp_secret=None):
    """Perform Builder ID login via Playwright and capture auth headers."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: playwright not installed. Run: pip install playwright && playwright install chromium")
        sys.exit(1)

    try:
        import pyotp
    except ImportError:
        print("ERROR: pyotp not installed. Run: pip install pyotp")
        sys.exit(1)

    if not email:
        email = input("Email: ").strip()
    if not password:
        password = getpass.getpass("Password: ")
    if not totp_secret:
        totp_secret = os.environ.get("AILEAGUE_TOTP_SECRET")
        if not totp_secret:
            totp_secret = getpass.getpass("TOTP Secret (or MFA code if 6 digits): ").strip()

    # If they pasted a 6-digit code instead of the secret
    if len(totp_secret) == 6 and totp_secret.isdigit():
        mfa_code = totp_secret
    else:
        totp = pyotp.TOTP(totp_secret)
        mfa_code = totp.now()

    print(f"[auth] Logging in as {email}...")

    captured_headers = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # Capture auth headers from network requests
        def on_request(request):
            url = request.url
            hdrs = request.headers
            if ("aileague.aws.dev" in url or "graphql.aileague" in url) and "authorization" in hdrs:
                captured_headers["authorization"] = hdrs["authorization"]
                for key in ("x-amz-date", "x-amz-security-token", "x-api-key"):
                    if key in hdrs:
                        captured_headers[key] = hdrs[key]

        page.on("request", on_request)

        # Navigate to trigger Builder ID redirect
        try:
            page.goto(f"{BASE_URL}/leaderboard", wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass  # May timeout on redirect, continue

        # --- Builder ID Login Flow ---
        # Step 1: Click "Sign in with AWS Builder ID" if present
        try:
            sign_in_btn = page.get_by_role("button", name="Sign in with AWS Builder ID")
            sign_in_btn.wait_for(timeout=10000)
            sign_in_btn.click()
            page.wait_for_timeout(3000)
        except Exception:
            pass

        # Dismiss cookie banner if present
        try:
            accept_btn = page.locator('button:has-text("Accept")').first
            if accept_btn.is_visible(timeout=3000):
                accept_btn.click()
                page.wait_for_timeout(1000)
        except Exception:
            pass

        # Step 2: Enter email
        email_input = page.locator(
            'input[placeholder*="example.com"], input[type="email"], input[type="text"]:visible'
        ).first
        email_input.wait_for(timeout=30000)
        email_input.fill(email)
        page.wait_for_timeout(1000)

        # Click Continue
        page.get_by_test_id("test-primary-button").click()

        # Wait for password field
        try:
            page.wait_for_selector('input[type="password"]', timeout=15000)
        except Exception:
            page.wait_for_timeout(3000)

        # Step 3: Enter password
        page.wait_for_selector('input[type="password"]', timeout=30000)
        page.fill('input[type="password"]', password)
        page.get_by_test_id("test-primary-button").click()
        page.wait_for_timeout(5000)

        # Step 4: Enter MFA code
        mfa_input = page.locator(
            'input[placeholder*="code" i], input[placeholder*="Enter code"]'
        ).first
        try:
            mfa_input.wait_for(timeout=30000)
        except Exception:
            raise RuntimeError("MFA input did not appear — check credentials")
        mfa_input.fill(mfa_code)
        page.get_by_test_id("test-primary-button").click()
        page.wait_for_timeout(5000)

        # --- Navigate to capture auth headers ---
        try:
            page.goto(f"{BASE_URL}/artifacts", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(5000)
        except Exception:
            pass

        if not captured_headers.get("authorization"):
            try:
                page.goto(f"{BASE_URL}/leaderboard", wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(5000)
            except Exception:
                pass

        browser.close()

    if not captured_headers.get("authorization"):
        print("ERROR: Login succeeded but no auth headers captured")
        sys.exit(1)

    print("[auth] Authenticated successfully")
    save_cached_auth(captured_headers)
    return captured_headers


# =============================================================================
# GraphQL Helpers
# =============================================================================

def _build_headers(auth_headers):
    """Build request headers for GraphQL calls."""
    headers = {
        "content-type": "application/json; charset=UTF-8",
        "origin": BASE_URL,
        "referer": f"{BASE_URL}/",
        "x-amz-user-agent": "aws-amplify/6.16.2 api/1 framework/1",
    }
    headers.update(auth_headers)
    return headers


def graphql(auth_headers, query, variables):
    """Execute a GraphQL query/mutation."""
    resp = requests.post(GRAPHQL_URL, json={
        "query": query,
        "variables": variables,
    }, headers=_build_headers(auth_headers), timeout=30)

    if resp.status_code != 200:
        print(f"  ERROR: HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    data = resp.json()
    if "errors" in data:
        print(f"  ERROR: {data['errors']}")
        return None

    return data.get("data")


# =============================================================================
# Resource Creation
# =============================================================================

def create_memory(auth_headers, leaderboard_id, config):
    """Create memory resource. If same name exists, skip. If different name exists, delete and recreate."""
    desired_name = config["name"]

    # List existing memory
    existing = graphql(auth_headers,
        """query ListMemory($input: ListMemoryInput) {
          listMemory(input: $input) {
            items { toolId name memoryId }
          }
        }""",
        {"input": {"leaderboardId": leaderboard_id}}
    )

    if existing and existing.get("listMemory", {}).get("items"):
        items = existing["listMemory"]["items"]
        found_match = None

        # Check if desired name already exists
        for item in items:
            if item["name"] == desired_name:
                found_match = item

        # Delete all that DON'T match (cleanup extras)
        # First detach memory from supervisor to allow deletion
        items_to_delete = [item for item in items if item["name"] != desired_name]
        if items_to_delete:
            # Detach memory from supervisor by setting memory to null
            graphql(auth_headers,
                """mutation UpdateSupervisorAgent($input: UpdateSupervisorAgentInput!) {
                  updateSupervisorAgent(input: $input) { statusCode message __typename }
                }""",
                {"input": {"leaderboardId": leaderboard_id, "memory": None}}
            )
            time.sleep(10)  # Wait for detach to complete before deleting

        for item in items_to_delete:
            print(f"  [-] Deleting extra memory: {item['name']} (id={item['memoryId']})")
            # Retry with delay — memory may be in transitional state after detach
            for attempt in range(5):
                result = graphql(auth_headers,
                    """mutation DeleteMemory($input: DeleteMemoryInput!) {
                      deleteMemory(input: $input) { toolId memoryId __typename }
                    }""",
                    {"input": {"toolId": item["toolId"], "leaderboardId": leaderboard_id}}
                )
                if result:
                    break
                print(f"      Retrying in 10s... (attempt {attempt+2}/5)")
                time.sleep(10)

        if found_match:
            print(f"  [=] Memory: {desired_name} already exists (id={found_match['memoryId']}), skipping")
            return found_match

    # Create new
    result = graphql(auth_headers,
        """mutation CreateMemory($input: CreateMemoryInput!) {
          createMemory(input: $input) {
            memory { toolId name memoryId description status }
          }
        }""",
        {"input": {"name": desired_name, "leaderboardId": leaderboard_id}}
    )
    if result:
        mem = result["createMemory"]["memory"]
        print(f"  [+] Memory: {mem['name']} (id={mem['memoryId']})")
        return mem
    return None


def create_guardrail(auth_headers, leaderboard_id, config):
    """Create or update guardrail. Delete extras that don't match desired name."""
    desired_name = config["name"]

    # List existing guardrails
    existing = graphql(auth_headers,
        """query ListGuardrail($input: ListGuardrailInput) {
          listGuardrail(input: $input) {
            items { toolId name guardrailId status }
          }
        }""",
        {"input": {"leaderboardId": leaderboard_id}}
    )

    found_match = None
    items_to_delete = []

    if existing and existing.get("listGuardrail", {}).get("items"):
        items = existing["listGuardrail"]["items"]
        for item in items:
            if item["name"] == desired_name:
                found_match = item
            else:
                items_to_delete.append(item)

    # Delete extras (detach from supervisor first)
    if items_to_delete:
        graphql(auth_headers,
            """mutation UpdateSupervisorAgent($input: UpdateSupervisorAgentInput!) {
              updateSupervisorAgent(input: $input) { statusCode message __typename }
            }""",
            {"input": {"leaderboardId": leaderboard_id, "guardrail": None}}
        )
        time.sleep(5)

        for item in items_to_delete:
            print(f"  [-] Deleting extra guardrail: {item['name']} (id={item['guardrailId']})")
            for attempt in range(5):
                result = graphql(auth_headers,
                    """mutation DeleteGuardrail($input: DeleteGuardrailInput!) {
                      deleteGuardrail(input: $input) { toolId guardrailId __typename }
                    }""",
                    {"input": {"toolId": item["toolId"], "leaderboardId": leaderboard_id}}
                )
                if result:
                    break
                print(f"      Retrying in 10s... (attempt {attempt+2}/5)")
                time.sleep(10)

    # Build policy payload
    guardrail_input = _build_guardrail_input(config, leaderboard_id)

    if found_match:
        # Update existing
        guardrail_input["toolId"] = found_match["toolId"]
        result = graphql(auth_headers,
            """mutation UpdateGuardrail($input: UpdateGuardrailInput!) {
              updateGuardrail(input: $input) {
                guardrail { toolId name guardrailId status __typename }
                __typename
              }
            }""",
            {"input": guardrail_input}
        )
        if result:
            gr = result["updateGuardrail"]["guardrail"]
            print(f"  [~] Guardrail updated: {gr['name']} (id={gr['guardrailId']})")
            return gr
        return found_match
    else:
        # Create new
        result = graphql(auth_headers,
            """mutation CreateGuardrail($input: CreateGuardrailInput!) {
              createGuardrail(input: $input) {
                guardrail { toolId name guardrailId status __typename }
                __typename
              }
            }""",
            {"input": guardrail_input}
        )
        if result:
            gr = result["createGuardrail"]["guardrail"]
            print(f"  [+] Guardrail created: {gr['name']} (id={gr['guardrailId']})")
            return gr
        return None


def _build_guardrail_input(config, leaderboard_id):
    """Build the guardrail input payload from config."""
    # Content policy
    content_policy = None
    if config.get("contentFilters"):
        filters = []
        for f in config["contentFilters"]:
            filters.append({
                "type": f["type"],
                "inputStrength": f.get("inputStrength", "MEDIUM"),
                "outputStrength": f.get("outputStrength", "MEDIUM"),
                "inputModalities": ["TEXT", "IMAGE"],
                "outputModalities": ["TEXT", "IMAGE"],
                "inputAction": "BLOCK",
                "outputAction": "BLOCK",
                "inputEnabled": f.get("inputStrength", "MEDIUM") != "NONE",
                "outputEnabled": f.get("outputStrength", "MEDIUM") != "NONE",
            })
        content_policy = {"filtersConfig": filters, "tierConfig": {"tierName": "CLASSIC"}}

    # Topic policy
    topic_policy = None
    if config.get("denyTopics"):
        topics = []
        for t in config["denyTopics"]:
            topics.append({
                "name": t["name"],
                "definition": t["definition"],
                "examples": t.get("examples", []),
                "type": "DENY",
                "inputAction": t.get("inputAction", "BLOCK"),
                "outputAction": t.get("outputAction", "BLOCK"),
                "inputEnabled": True,
                "outputEnabled": True,
            })
        topic_policy = {"topicsConfig": topics, "tierConfig": {"tierName": "CLASSIC"}}

    return {
        "name": config["name"],
        "description": config.get("description", ""),
        "leaderboardId": leaderboard_id,
        "blockedInputMessaging": config.get("blockedInputMessaging", "No"),
        "blockedOutputsMessaging": config.get("blockedOutputsMessaging", "No"),
        "contentPolicyConfig": content_policy,
        "topicPolicyConfig": topic_policy,
        "wordPolicyConfig": None,
        "sensitiveInformationPolicyConfig": None,
    }


def create_lambda_tool(auth_headers, leaderboard_id, tool_config, config_dir):
    """Create Lambda tool if it doesn't exist. Returns tool info with toolId."""
    name = tool_config["name"]

    # List existing tools
    existing = graphql(auth_headers,
        """query ListLambdaTool($input: ListLambdaToolInput) {
          listLambdaTool(input: $input) {
            items { toolId name functionName }
          }
        }""",
        {"input": {"leaderboardId": leaderboard_id}}
    )

    # Check if this tool already exists
    if existing and existing.get("listLambdaTool", {}).get("items"):
        for item in existing["listLambdaTool"]["items"]:
            if item["name"] == name:
                print(f"  [=] Tool: {name} already exists (function={item['functionName']})")
                return item

    # Create new
    result = graphql(auth_headers,
        """mutation CreateLambdaTool($input: CreateLambdaToolInput!) {
          createLambdaTool(input: $input) {
            lambdaTool { toolId name functionName }
          }
        }""",
        {"input": {"name": name, "runtime": "python3.11", "leaderboardId": leaderboard_id}}
    )
    if result:
        tool = result["createLambdaTool"]["lambdaTool"]
        print(f"  [+] Tool created: {name} (function={tool['functionName']})")
        print(f"      NOTE: Code must be edited via SageMaker IDE")
        return tool
    return None


def deploy_sub_agents(auth_headers, leaderboard_id, sub_agents_config, tool_ids_by_name):
    """Deploy sub-agents: create missing, update existing, delete extras.
    Returns list of agentIds for supervisor attachment.
    
    SAFETY: Before deleting a sub-agent, reset its model to Haiku.
    Never delete a sub-agent with a fine-tuned model attached.
    """
    HAIKU_MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    desired_names = {sa["name"] for sa in sub_agents_config}

    # List existing sub-agents
    existing = graphql(auth_headers,
        """query ListSubAgents($input: ListSubAgentsInput!) {
          listSubAgents(input: $input) {
            statusCode
            subAgentList { agentId name __typename }
          }
        }""",
        {"input": {"leaderboardId": leaderboard_id}}
    )

    existing_agents = []
    if existing and existing.get("listSubAgents", {}).get("subAgentList"):
        existing_agents = existing["listSubAgents"]["subAgentList"]

    existing_by_name = {a["name"]: a for a in existing_agents}

    # Delete agents not in YAML (detach from supervisor first, reset model to Haiku)
    agents_to_delete = [a for a in existing_agents if a["name"] not in desired_names]
    if agents_to_delete:
        # Detach all sub-agents from supervisor
        graphql(auth_headers,
            """mutation UpdateSupervisorAgent($input: UpdateSupervisorAgentInput!) {
              updateSupervisorAgent(input: $input) { statusCode message __typename }
            }""",
            {"input": {"leaderboardId": leaderboard_id, "subAgents": []}}
        )
        time.sleep(5)

        for agent in agents_to_delete:
            # Reset model to Haiku before deleting (safety: never delete with fine-tuned model)
            print(f"  [-] Resetting sub-agent model to Haiku: {agent['name']}")
            graphql(auth_headers,
                """mutation UpdateSubAgent($input: UpdateSubAgentInput!) {
                  updateSubAgent(input: $input) { statusCode message __typename }
                }""",
                {"input": {
                    "leaderboardId": leaderboard_id,
                    "agentId": agent["agentId"],
                    "name": agent["name"],
                    "systemPrompt": "placeholder",
                    "lambdaTools": [],
                    "modelId": HAIKU_MODEL_ID,
                }}
            )
            time.sleep(3)

            print(f"  [-] Deleting sub-agent: {agent['name']}")
            for attempt in range(5):
                result = graphql(auth_headers,
                    """mutation DeleteSubAgent($input: DeleteSubAgentInput!) {
                      deleteSubAgent(input: $input) { statusCode message __typename }
                    }""",
                    {"input": {"agentId": agent["agentId"], "leaderboardId": leaderboard_id}}
                )
                if result:
                    break
                print(f"      Retrying in 10s... (attempt {attempt+2}/5)")
                time.sleep(10)

    # Create or update sub-agents
    agent_ids = []
    for sa_config in sub_agents_config:
        name = sa_config["name"]
        # Resolve tool names to toolIds
        sa_tool_ids = []
        for tool_name in sa_config.get("tools", []):
            if tool_name in tool_ids_by_name:
                sa_tool_ids.append(tool_ids_by_name[tool_name])
            else:
                print(f"      WARNING: Tool '{tool_name}' not found for sub-agent '{name}'")

        model_id = sa_config.get("modelId", HAIKU_MODEL_ID)

        if name in existing_by_name:
            # Update existing
            agent_id = existing_by_name[name]["agentId"]
            result = graphql(auth_headers,
                """mutation UpdateSubAgent($input: UpdateSubAgentInput!) {
                  updateSubAgent(input: $input) { statusCode message __typename }
                }""",
                {"input": {
                    "leaderboardId": leaderboard_id,
                    "agentId": agent_id,
                    "name": name,
                    "systemPrompt": sa_config["systemPrompt"],
                    "lambdaTools": sa_tool_ids,
                    "modelId": model_id,
                }}
            )
            if result:
                print(f"  [~] Sub-agent updated: {name} (id={agent_id})")
            agent_ids.append(agent_id)
        else:
            # Create new
            result = graphql(auth_headers,
                """mutation CreateSubAgent($input: CreateSubAgentInput!) {
                  createSubAgent(input: $input) { statusCode message __typename }
                }""",
                {"input": {
                    "leaderboardId": leaderboard_id,
                    "name": name,
                    "systemPrompt": sa_config["systemPrompt"],
                    "lambdaTools": sa_tool_ids,
                    "modelId": model_id,
                }}
            )
            if result:
                # Need to get the agentId — list again
                list_result = graphql(auth_headers,
                    """query ListSubAgents($input: ListSubAgentsInput!) {
                      listSubAgents(input: $input) {
                        subAgentList { agentId name }
                      }
                    }""",
                    {"input": {"leaderboardId": leaderboard_id}}
                )
                if list_result:
                    for a in list_result.get("listSubAgents", {}).get("subAgentList", []):
                        if a["name"] == name:
                            agent_ids.append(a["agentId"])
                            break
                print(f"  [+] Sub-agent created: {name}")

    return agent_ids


def deploy_lambda_tools(auth_headers, leaderboard_id, tools_config, config_dir):
    """Deploy all Lambda tools: create missing, delete extras, return toolIds."""
    desired_names = {t["name"] for t in tools_config}

    # List existing tools
    existing = graphql(auth_headers,
        """query ListLambdaTool($input: ListLambdaToolInput) {
          listLambdaTool(input: $input) {
            items { toolId name functionName }
          }
        }""",
        {"input": {"leaderboardId": leaderboard_id}}
    )

    existing_items = existing.get("listLambdaTool", {}).get("items", []) if existing else []
    existing_by_name = {item["name"]: item for item in existing_items}

    # Delete tools not in YAML (detach from supervisor first)
    items_to_delete = [item for item in existing_items if item["name"] not in desired_names]
    if items_to_delete:
        graphql(auth_headers,
            """mutation UpdateSupervisorAgent($input: UpdateSupervisorAgentInput!) {
              updateSupervisorAgent(input: $input) { statusCode message __typename }
            }""",
            {"input": {"leaderboardId": leaderboard_id, "lambdaTools": []}}
        )
        time.sleep(5)

        for item in items_to_delete:
            print(f"  [-] Deleting tool: {item['name']} (function={item['functionName']})")
            for attempt in range(5):
                result = graphql(auth_headers,
                    """mutation DeleteLambdaTool($input: DeleteLambdaToolInput!) {
                      deleteLambdaTool(input: $input) { toolId functionName __typename }
                    }""",
                    {"input": {"toolId": item["toolId"], "leaderboardId": leaderboard_id}}
                )
                if result:
                    break
                print(f"      Retrying in 10s... (attempt {attempt+2}/5)")
                time.sleep(10)

    # Create missing tools, collect all toolIds
    tool_ids = []
    for tool_config in tools_config:
        name = tool_config["name"]
        if name in existing_by_name:
            print(f"  [=] Tool: {name} already exists (function={existing_by_name[name]['functionName']})")
            tool_ids.append(existing_by_name[name]["toolId"])
        else:
            result = graphql(auth_headers,
                """mutation CreateLambdaTool($input: CreateLambdaToolInput!) {
                  createLambdaTool(input: $input) {
                    lambdaTool { toolId name functionName }
                  }
                }""",
                {"input": {"name": name, "runtime": "python3.11", "leaderboardId": leaderboard_id}}
            )
            if result:
                tool = result["createLambdaTool"]["lambdaTool"]
                print(f"  [+] Tool created: {name} (function={tool['functionName']})")
                print(f"      NOTE: Code must be edited via SageMaker IDE")
                tool_ids.append(tool["toolId"])

    return tool_ids


def deploy_lambda_code(auth_headers, leaderboard_id, tools_config, config_dir):
    """Deploy Lambda code via SageMaker IDE terminal.
    Opens the IDE via presigned URL, uses the terminal to upload and deploy code.
    Only deploys tools that have a sourceDir with an index.py file."""
    import base64

    # Check which tools have code to deploy
    tools_with_code = []
    for tool_config in tools_config:
        source_dir = tool_config.get("sourceDir")
        if source_dir:
            code_path = Path(config_dir) / source_dir / "index.py"
            if code_path.exists():
                tools_with_code.append({
                    "name": tool_config["name"],
                    "functionName": f"AgentCoreGatewayTool-{tool_config['name']}",
                    "code": code_path.read_text(),
                })

    if not tools_with_code:
        print("  [=] No Lambda code to deploy (no sourceDir/index.py found)")
        return

    # Get presigned SageMaker URL
    result = graphql(auth_headers,
        """query GetPresignedDomainUrl($input: GetPresignedDomainUrlInput!) {
          getPresignedDomainUrl(input: $input) { authorizedUrl __typename }
        }""",
        {"input": {
            "leaderboardId": leaderboard_id,
            "landingUri": "app:CodeEditor:?origin=https://aileague.aws.dev"
        }}
    )
    if not result:
        print("  ERROR: Could not get presigned SageMaker URL")
        return

    presigned_url = result["getPresignedDomainUrl"]["authorizedUrl"]
    print(f"  [*] Opening SageMaker IDE for code deployment...")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ERROR: playwright not installed")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)  # Use headful for debugging; change to True once working
        page = browser.new_page()

        # Navigate to presigned URL
        print(f"  [*] Navigating to SageMaker URL...")
        try:
            page.goto(presigned_url, wait_until="domcontentloaded", timeout=90000)
            print(f"  [*] Page loaded: {page.title()}")
        except Exception as e:
            print(f"  [*] Navigation timeout (expected): {str(e)[:60]}")

        # Wait for VS Code to load
        print(f"  [*] Waiting 30s for IDE to initialize...")
        page.wait_for_timeout(30000)
        print(f"  [*] Current URL: {page.url[:80]}")

        # Open terminal — try multiple approaches
        # Method 1: Keyboard shortcut
        print(f"  [*] Opening terminal (Ctrl+Shift+`)...")
        page.keyboard.press("Control+Shift+`")  # Opens new terminal
        page.wait_for_timeout(5000)

        # Check if terminal is visible by looking for terminal input
        # If not, try the menu approach
        try:
            terminal_visible = page.locator('.terminal-wrapper, .xterm').first.is_visible(timeout=5000)
            print(f"  [*] Terminal visible: {terminal_visible}")
        except Exception:
            terminal_visible = False
            print(f"  [*] Terminal not detected via selector")

        if not terminal_visible:
            # Method 2: Command palette
            print(f"  [*] Trying command palette method...")
            page.keyboard.press("Control+Shift+p")
            page.wait_for_timeout(1000)
            page.keyboard.type("Terminal: Create New Terminal", delay=20)
            page.wait_for_timeout(1000)
            page.keyboard.press("Enter")
            page.wait_for_timeout(5000)

        print(f"  [*] Terminal should be open, deploying code...")

        # Deploy each tool's code
        for tool in tools_with_code:
            func_name = tool["functionName"]
            code = tool["code"]

            print(f"  [*] Deploying code for {tool['name']} ({func_name})...")

            # Gzip + base64 encode the code for faster transfer
            import gzip
            compressed = gzip.compress(code.encode())
            code_b64 = base64.b64encode(compressed).decode()

            # Build command sequence — decode gzip+base64 via python, zip, deploy
            # Split into: write payload, decode it, zip, deploy
            commands = [
                f"echo '{code_b64}' | python3 -c \"import sys,base64,gzip; open('/tmp/lambda_function.py','wb').write(gzip.decompress(base64.b64decode(sys.stdin.read().strip())))\"",
                f"cd /tmp && python3 -c \"import zipfile; z=zipfile.ZipFile('code.zip','w'); z.write('lambda_function.py'); z.close()\"",
                f"aws lambda update-function-code --function-name {func_name} --zip-file fileb:///tmp/code.zip --no-cli-pager",
                "echo 'DEPLOY_DONE'",
            ]

            # Type each command and press Enter
            for i, cmd in enumerate(commands):
                print(f"      Typing cmd {i+1}/{len(commands)} ({len(cmd)} chars)...")
                page.keyboard.type(cmd, delay=2)
                page.keyboard.press("Enter")
                # Wait longer for the Lambda deploy command
                if 'update-function-code' in cmd:
                    page.wait_for_timeout(8000)
                else:
                    page.wait_for_timeout(3000)

            # Wait for deployment to complete
            print(f"      Waiting for deployment to finish...")
            page.wait_for_timeout(5000)
            print(f"  [+] Code deployed: {tool['name']}")

        # Trigger schema refresh for each tool
        for tool in tools_with_code:
            graphql(auth_headers,
                """mutation UpdateLambdaTool($input: UpdateLambdaToolInput!) {
                  updateLambdaTool(input: $input) { toolId functionName schemaS3Uri __typename }
                }""",
                {"input": {"functionName": tool["functionName"], "leaderboardId": leaderboard_id}}
            )
            print(f"  [~] Schema refresh triggered: {tool['name']}")

        browser.close()

    print(f"  [+] All Lambda code deployed ({len(tools_with_code)} tools)")


def update_supervisor(auth_headers, leaderboard_id, config,
                      memory_tool_id=None, guardrail_tool_id=None, lambda_tool_ids=None, sub_agent_ids=None):
    """Update supervisor agent configuration."""
    variables = {
        "input": {
            "leaderboardId": leaderboard_id,
            "name": config.get("name", "Supervisor"),
            "systemPrompt": config["systemPrompt"],
            "modelId": config.get("modelId", "global.amazon.nova-2-lite-v1:0"),
            "lambdaTools": lambda_tool_ids or [],
            "subAgents": sub_agent_ids or [],
            "memory": memory_tool_id,
            "guardrail": guardrail_tool_id,
        }
    }

    result = graphql(auth_headers,
        """mutation UpdateSupervisorAgent($input: UpdateSupervisorAgentInput!) {
          updateSupervisorAgent(input: $input) { statusCode message }
        }""",
        variables
    )
    if result:
        msg = result["updateSupervisorAgent"]
        print(f"  [+] Supervisor updated: {msg.get('message', 'OK')}")
        return True
    return False


# =============================================================================
# Main
# =============================================================================

def deploy(config_dir, leaderboard_id, auth_headers, skip_lambdas=False):
    """Deploy all resources from config.yaml."""
    config_path = Path(config_dir) / "config.yaml"
    if not config_path.exists():
        print(f"ERROR: {config_path} not found")
        sys.exit(1)

    config = yaml.safe_load(config_path.read_text())
    print(f"\n[deploy] Deploying from {config_path}")
    print(f"[deploy] Leaderboard: {leaderboard_id}\n")

    # Track resource IDs for supervisor attachment
    memory_tool_id = None
    guardrail_tool_id = None

    # 1. Create Memory
    if config.get("memory"):
        mem = create_memory(auth_headers, leaderboard_id, config["memory"])
        if mem:
            memory_tool_id = mem.get("toolId")

    # 2. Create Guardrail
    if config.get("guardrail"):
        gr = create_guardrail(auth_headers, leaderboard_id, config["guardrail"])
        if gr:
            guardrail_tool_id = gr.get("toolId")

    # 3. Create/Delete Lambda Tools
    tool_ids = []
    tool_ids_by_name = {}
    if config.get("tools"):
        tool_ids = deploy_lambda_tools(auth_headers, leaderboard_id, config["tools"], config_dir)
        # Deploy Lambda code via SageMaker IDE
        if not skip_lambdas:
            deploy_lambda_code(auth_headers, leaderboard_id, config["tools"], config_dir)
        else:
            print("  [=] Skipping Lambda code deployment (--no-lambdas)")
        # Build name->toolId mapping for sub-agent tool resolution
        existing_tools = graphql(auth_headers,
            """query ListLambdaTool($input: ListLambdaToolInput) {
              listLambdaTool(input: $input) { items { toolId name } }
            }""",
            {"input": {"leaderboardId": leaderboard_id}}
        )
        if existing_tools:
            for item in existing_tools.get("listLambdaTool", {}).get("items", []):
                tool_ids_by_name[item["name"]] = item["toolId"]

    # 4. Create/Update/Delete Sub-Agents
    sub_agent_ids = []
    if config.get("subAgents"):
        sub_agent_ids = deploy_sub_agents(auth_headers, leaderboard_id, config["subAgents"], tool_ids_by_name)
    else:
        # No sub-agents in YAML — delete any existing
        sub_agent_ids = deploy_sub_agents(auth_headers, leaderboard_id, [], tool_ids_by_name)

    # 5. Update Supervisor (pass resource IDs, not names)
    if config.get("supervisor"):
        # Only attach tools listed in supervisor.tools, not all tools
        supervisor_tool_names = set(config["supervisor"].get("tools", []))
        supervisor_tool_ids = [tool_ids_by_name[n] for n in supervisor_tool_names if n in tool_ids_by_name]

        update_supervisor(auth_headers, leaderboard_id, config["supervisor"],
                         memory_tool_id=memory_tool_id,
                         guardrail_tool_id=guardrail_tool_id,
                         lambda_tool_ids=supervisor_tool_ids,
                         sub_agent_ids=sub_agent_ids)

    print("\n[deploy] Done!")


def main():
    parser = argparse.ArgumentParser(description="Deploy AI League agent config via GraphQL API")
    parser.add_argument("config_dir", help="Path to agent-config directory containing config.yaml")
    parser.add_argument("--leaderboard", "-l", default=None,
                        help="Leaderboard ID (or set AILEAGUE_LEADERBOARD env var)")
    parser.add_argument("--email", "-e", help="Builder ID email (or set AILEAGUE_EMAIL env var)")
    parser.add_argument("--no-cache", action="store_true", help="Skip cached auth, force re-login")
    parser.add_argument("--no-lambdas", action="store_true", help="Skip Lambda code deployment (only update config/prompts)")
    args = parser.parse_args()

    email = args.email or os.environ.get("AILEAGUE_EMAIL")
    password = os.environ.get("AILEAGUE_PASSWORD")
    totp_secret = os.environ.get("AILEAGUE_TOTP_SECRET")
    leaderboard_id = args.leaderboard or os.environ.get("AILEAGUE_LEADERBOARD")

    # Load saved credentials as defaults
    saved = load_saved_credentials()

    # Try cached auth first
    auth_headers = None
    if not args.no_cache:
        auth_headers = load_cached_auth()

    if not auth_headers:
        # Prompt with saved defaults (like aws configure)
        email = email or prompt_with_default("Email", saved.get("email"))
        password = password or prompt_with_default("Password", saved.get("password"), secret=True)
        totp_secret = totp_secret or getpass.getpass("MFA code (6 digits) or TOTP secret: ").strip()

        # Save credentials for next run (not TOTP — that changes)
        save_credentials({"email": email, "password": password, "leaderboard": leaderboard_id or saved.get("leaderboard")})

        auth_headers = authenticate(email, password, totp_secret)

    if not leaderboard_id:
        leaderboard_id = prompt_with_default("Leaderboard ID", saved.get("leaderboard"))
        # Update saved leaderboard
        saved["leaderboard"] = leaderboard_id
        save_credentials(saved)

    deploy(args.config_dir, leaderboard_id, auth_headers, skip_lambdas=args.no_lambdas)


if __name__ == "__main__":
    main()
