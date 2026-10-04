import hmac
import hashlib
import json
import logging
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from agent import ENTERPRISE_FILE, generate_workflow
from github_push import normalize_repository_url, push_workflow


ROOT = Path(__file__).resolve().parent
CAPABILITY_FILE = ROOT / "capability.json"
DEFAULT_REPOSITORY_URL = "https://github.com/Micro-DevOps/service-a"
PIPELINE_TOOL_ID = "ci.github.github_pipeline"
SUCCESS_TOOL_NAME = "ci.github_actions.github_pipeline"
MAX_REQUEST_BYTES = 1024 * 1024
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("pipeline-agent")

app = FastAPI(title="GitHub Pipeline Agent", docs_url=None, redoc_url=None)


def load_enterprise_settings():
    settings = yaml.safe_load(ENTERPRISE_FILE.read_text(encoding="utf-8"))
    if not isinstance(settings, dict):
        raise RuntimeError("Enterprise settings must be a YAML mapping.")
    return settings


def build_capabilities():
    capability = json.loads(CAPABILITY_FILE.read_text(encoding="utf-8"))
    tool = next((item for item in capability.get("tools", []) if item.get("id") == PIPELINE_TOOL_ID), None)
    if tool is None:
        raise RuntimeError(f"Capability manifest is missing {PIPELINE_TOOL_ID}.")
    tool["inputSchema"] = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Pipeline requirements for the workflow generator."},
            "pipeline_name": {"type": "string", "description": "Workflow filename without folders; .yml is added."},
            "branch_name": {"type": "string", "description": "Target Git branch to commit and push."},
            "repo_url": {"type": "string", "description": "Optional HTTPS GitHub repository URL."},
            "organization": {"type": "string", "description": "Repository owner or organization, used with repository."},
            "repository": {"type": "string", "description": "Repository name, used with organization."},
            "org_name": {"type": "string", "description": "Alias for organization."},
            "repo_name": {"type": "string", "description": "Alias for repository."},
        },
        "required": ["prompt", "branch_name"],
        "additionalProperties": False,
    }
    manifest_content = json.dumps(
        {key: value for key, value in capability.items() if key != "manifestDigest"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    capability["manifestDigest"] = "sha256:" + hashlib.sha256(manifest_content).hexdigest()
    return capability


def _candidate_invocation(value):
    if not isinstance(value, dict):
        return None, None
    skill = value.get("skill") or value.get("tool_id") or value.get("toolId")
    arguments = value.get(
        "arguments",
        value.get("args", value.get("input", value.get("parameters", value.get("payload")))),
    )
    if arguments is None and skill:
        arguments = {
            key: item
            for key, item in value.items()
            if key not in {"skill", "tool_id", "toolId"}
        }
    return skill, arguments if isinstance(arguments, dict) else None


def extract_invocation(request_body):
    params = request_body.get("params")
    if not isinstance(params, dict):
        return None, {}, ""

    message = params.get("message")
    candidates = [params]
    free_text = ""
    if isinstance(message, dict):
        if isinstance(message.get("metadata"), dict):
            candidates.append(message["metadata"])
        for part in message.get("parts", []):
            if not isinstance(part, dict):
                continue
            data = part.get("data")
            if isinstance(data, dict):
                candidates.append(data)
            text = part.get("text")
            if isinstance(text, str):
                if not free_text:
                    free_text = text
                if text.strip() == "list_capabilities":
                    candidates.append({"skill": "list_capabilities"})
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    candidates.append(parsed)

    for candidate in candidates:
        skill, arguments = _candidate_invocation(candidate)
        if skill:
            return skill, arguments or {}, free_text
    return None, {}, free_text


def _repo_from_prompt(prompt):
    url_match = re.search(r"https?://(?:github\.com|github\.adib\.co\.ae)/[^\s\"'<>]+", prompt, re.IGNORECASE)
    if url_match:
        return url_match.group(0).rstrip(".,);]")
    named_match = re.search(
        r"\b(?:repo(?:sitory)?|org(?:anization)?/repo)\s*(?:is\s*)?[:=]?\s*([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",
        prompt,
        re.IGNORECASE,
    )
    if named_match:
        return f"https://github.com/{named_match.group(1)}/{named_match.group(2)}"

    owner_match = re.search(
        r"\b(?:organization|org)(?:\s+name)?\s*(?::|=|\bis\s+)\s*([A-Za-z0-9_.-]+)",
        prompt,
        re.IGNORECASE,
    )
    repository_match = re.search(
        r"\b(?:repository|repo)(?:\s+name)?\s*(?::|=|\bis\s+)\s*([A-Za-z0-9_.-]+)",
        prompt,
        re.IGNORECASE,
    )
    if owner_match and repository_match:
        return f"https://github.com/{owner_match.group(1)}/{repository_match.group(1)}"
    return None


def resolve_repository(arguments, prompt, settings):
    repo_url = arguments.get("repo_url") or arguments.get("repository_url")
    owner = arguments.get("organization") or arguments.get("org_name") or arguments.get("owner")
    repository = arguments.get("repository") or arguments.get("repo_name")

    if not repo_url and owner and repository:
        repo_url = f"https://github.com/{owner}/{repository}"
    if not repo_url:
        repo_url = _repo_from_prompt(prompt)
    if not repo_url:
        repo_url = os.environ.get(
            "DEFAULT_REPO_URL", settings.get("pipeline_repository", DEFAULT_REPOSITORY_URL)
        )

    enterprise_host = urlsplit(settings.get("github_server_url", "https://github.com")).hostname
    allowed_hosts = {"github.com"}
    if enterprise_host:
        allowed_hosts.add(enterprise_host.lower())
    return normalize_repository_url(repo_url, allowed_hosts)


def _branch_from_prompt(prompt):
    match = re.search(r"\bbranch(?:_name)?\s*(?:is\s*)?[:=]?\s*([A-Za-z0-9._/-]+)", prompt, re.IGNORECASE)
    return match.group(1).rstrip(".,") if match else ""


def execute_pipeline(arguments, free_text):
    prompt = arguments.get("prompt") or arguments.get("requirements") or free_text
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt is required.")
    prompt = prompt.strip()
    settings = load_enterprise_settings()
    repo_url, _, _, _ = resolve_repository(arguments, prompt, settings)
    branch = arguments.get("branch_name") or arguments.get("branch") or _branch_from_prompt(prompt)
    if not branch:
        raise ValueError("branch_name is required.")
    pipeline_name = arguments.get("pipeline_name") or "pipeline"

    workflow = generate_workflow(prompt)
    result = push_workflow(repo_url, branch, pipeline_name, workflow)
    return {
        "status": "success",
        "tool": SUCCESS_TOOL_NAME,
        "message": "GitHub Actions pipeline committed and pushed successfully.",
        "result": result,
    }


def a2a_message(payload, context_id=None):
    return {
        "messageId": str(uuid.uuid4()),
        "contextId": context_id or str(uuid.uuid4()),
        "role": "ROLE_AGENT",
        "parts": [{"data": payload, "mediaType": "application/json"}],
    }


def rpc_success(request_id, payload, context_id=None):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"message": a2a_message(payload, context_id)},
    }


def rpc_error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/.well-known/agent-card.json")
async def agent_card(request: Request):
    public_url = os.environ.get("PUBLIC_AGENT_URL", str(request.base_url).rstrip("/"))
    return {
        "name": "GitHub Pipeline Agent",
        "description": "Generates enterprise-compliant GitHub Actions pipelines and pushes them to a requested repository branch.",
        "supportedInterfaces": [
            {"url": f"{public_url.rstrip('/')}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "securitySchemes": {"bearerAuth": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}},
        "securityRequirements": [{"schemes": {"bearerAuth": {}}}],
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {
                "id": PIPELINE_TOOL_ID,
                "name": "Create and push GitHub Actions pipeline",
                "description": "Generate a workflow from a prompt and enterprise context, then commit and push it to .github/workflows on the target branch.",
                "tags": ["github", "actions", "pipeline", "ci", "workflow"],
                "inputModes": ["application/json", "text/plain"],
                "outputModes": ["application/json"],
            }
        ],
    }


@app.post("/a2a")
async def a2a_endpoint(request: Request):
    request_id = None
    expected_token = os.environ.get("A2A_BEARER_TOKEN", "")
    supplied_header = request.headers.get("authorization", "")
    supplied_token = supplied_header[7:].strip() if supplied_header.lower().startswith("bearer ") else ""
    if not expected_token:
        return JSONResponse(rpc_error(None, -32603, "A2A bearer token is not configured."), status_code=503)
    if not supplied_token or not hmac.compare_digest(supplied_token, expected_token):
        return JSONResponse(rpc_error(None, -32000, "Unauthorized."), status_code=401)

    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_REQUEST_BYTES:
        return JSONResponse(rpc_error(None, -32600, "Request body is too large."), status_code=413)
    body = await request.body()
    if len(body) > MAX_REQUEST_BYTES:
        return JSONResponse(rpc_error(None, -32600, "Request body is too large."), status_code=413)
    try:
        request_body = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(rpc_error(None, -32700, "Invalid JSON payload."), status_code=400)

    if not isinstance(request_body, dict):
        return JSONResponse(rpc_error(None, -32600, "Invalid JSON-RPC request."), status_code=400)
    request_id = request_body.get("id")
    if request_body.get("jsonrpc") != "2.0" or request_id is None or request_body.get("method") != "SendMessage":
        return JSONResponse(rpc_error(request_id, -32600, "Expected JSON-RPC 2.0 SendMessage with an id."), status_code=400)

    skill, arguments, free_text = extract_invocation(request_body)
    params = request_body.get("params", {})
    message_data = params.get("message", {}) if isinstance(params, dict) else {}
    context_id = message_data.get("contextId") if isinstance(message_data, dict) else None
    if skill == "list_capabilities":
        try:
            return rpc_success(request_id, build_capabilities(), context_id)
        except Exception:
            logger.exception("Capability discovery failed.")
            return JSONResponse(rpc_error(request_id, -32603, "Capability discovery failed."), status_code=500)
    if skill not in {PIPELINE_TOOL_ID, SUCCESS_TOOL_NAME, "github_pipeline"}:
        return JSONResponse(rpc_error(request_id, -32602, "Unknown or missing skill. Use list_capabilities to discover tools."), status_code=400)

    try:
        result = await run_in_threadpool(execute_pipeline, arguments, free_text)
    except Exception as error:
        logger.exception("GitHub pipeline tool call failed.")
        result = {
            "status": "failure",
            "tool": SUCCESS_TOOL_NAME,
            "message": "GitHub Actions pipeline not created.",
            "result": {"error": str(error)},
        }
    return rpc_success(request_id, result, context_id)