import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv
import yaml


ROOT = Path(__file__).resolve().parent
ENTERPRISE_FILE = ROOT / "enterprise-skill.yml"
load_dotenv(ROOT / ".env")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")


def load_enterprise_context():
    raw_context = ENTERPRISE_FILE.read_text(encoding="utf-8")
    settings = yaml.safe_load(raw_context)
    if not isinstance(settings, dict):
        raise ValueError("enterprise-skill.yml must contain a YAML mapping.")

    max_context = settings.get("max_context_characters")
    if isinstance(max_context, int) and len(raw_context) > max_context:
        raise ValueError("enterprise-skill.yml exceeds max_context_characters.")

    allowed_actions = settings.get("allowed_action_repositories")
    if not isinstance(allowed_actions, list):
        raise ValueError("allowed_action_repositories must be a YAML list.")

    return raw_context, settings


def ask_openai(prompt, enterprise_context):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is missing. Add it to the .env file in the project directory."
        )

    system_prompt = (
        "You generate GitHub Actions workflow files for the enterprise described below. "
        "Treat the enterprise YAML as binding policy. Use only the listed allowed actions; "
        "for other tasks use custom scripts or bash. Never print secrets, and do not "
        "delete the broad workspace. Use the listed GHES, runner, and service settings "
        "when relevant. If the request conflicts with policy or lacks essential details, "
        "return a concise explanation instead of a workflow. Return only valid workflow "
        "YAML, without markdown fences or commentary.\n\n"
        f"Enterprise YAML:\n{enterprise_context}"
    )
    payload = json.dumps(
        {
            "model": OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
    ).encode("utf-8")
    request = Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=600) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API returned HTTP {error.code}: {detail}") from error
    except URLError as error:
        raise RuntimeError("Cannot reach the OpenAI API. Check your internet connection.") from error

    try:
        content = result["choices"][0]["message"]["content"].strip()
    except (IndexError, KeyError, TypeError) as error:
        raise RuntimeError("OpenAI returned an unexpected response.") from error

    if content.startswith("```") and content.endswith("```"):
        content = "\n".join(content.splitlines()[1:-1]).strip()
    return content


def find_action_references(value):
    references = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "uses" and isinstance(item, str):
                references.append(item)
            references.extend(find_action_references(item))
    elif isinstance(value, list):
        for item in value:
            references.extend(find_action_references(item))
    return references


def validate_workflow(content, settings):
    errors = []
    max_bytes = settings.get("max_file_bytes")
    if isinstance(max_bytes, int) and len(content.encode("utf-8")) > max_bytes:
        errors.append(f"Workflow exceeds max_file_bytes ({max_bytes}).")

    try:
        workflow = yaml.load(content, Loader=yaml.BaseLoader)
    except yaml.YAMLError as error:
        return [f"Generated output is not valid YAML: {error}"]

    if not isinstance(workflow, dict) or "jobs" not in workflow:
        errors.append("Output must be a GitHub Actions workflow with a jobs section.")

    if isinstance(workflow, dict):
        allowed_actions = set(settings["allowed_action_repositories"])
        for action in find_action_references(workflow):
            if action not in allowed_actions:
                errors.append(f"Action is not on the enterprise allowlist: {action}")

    if settings.get("block_broad_workspace_delete"):
        broad_delete = re.compile(
            r"(?im)^\s*[^\n]*\brm\s+-[^\n]*r[^\n]*f[^\n]*\s+"
            r"(?:['\"]?(?:\.|\.\.|/|\*|\$GITHUB_WORKSPACE|\$\{\{\s*github\.workspace)[/\w*.-]*['\"]?)"
        )
        if broad_delete.search(content):
            errors.append("Workflow may broadly delete the workspace; remove that command.")

    return errors


def generate_workflow(prompt):
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("A pipeline prompt is required.")

    enterprise_context, settings = load_enterprise_context()
    workflow = ask_openai(prompt.strip(), enterprise_context)
    if not workflow:
        raise RuntimeError("OpenAI returned an empty response.")

    errors = validate_workflow(workflow, settings)
    if errors:
        raise ValueError("Generated workflow failed validation: " + "; ".join(errors))
    return workflow.rstrip() + "\n"