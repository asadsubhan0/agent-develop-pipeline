import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


class GitPushError(RuntimeError):
    pass


def _git(repository_dir, args, environment, *, check=True):
    result = subprocess.run(
        ["git", *args],
        cwd=repository_dir,
        env=environment,
        text=True,
        capture_output=True,
        timeout=240,
        check=False,
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        token = environment.get("GITHUB_PAT", "")
        if token:
            detail = detail.replace(token, "[redacted]")
        raise GitPushError(detail or f"git {args[0]} failed with exit code {result.returncode}.")
    return result


def normalize_repository_url(value, allowed_hosts):
    if not isinstance(value, str):
        raise ValueError("Repository URL must be a string.")
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.lower() not in allowed_hosts
        or (parsed.port is not None and parsed.port != 443)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Repository URL must use HTTPS on an approved GitHub host.")

    path_parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(path_parts) != 2:
        raise ValueError("Repository URL must identify exactly one owner and repository.")
    owner, repository = path_parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    name_pattern = re.compile(r"^[A-Za-z0-9_.-]+$")
    if any(
        not name_pattern.fullmatch(part) or part in {".", ".."}
        for part in (owner, repository)
    ):
        raise ValueError("Repository owner or name contains unsupported characters.")

    return f"https://{parsed.hostname}/{owner}/{repository}.git", parsed.hostname, owner, repository


def normalize_pipeline_name(value):
    if not isinstance(value, str) or not value.strip():
        value = "pipeline"
    name = value.strip()
    for suffix in (".yml", ".yaml"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", name):
        raise ValueError("pipeline_name must be a simple filename without folders.")
    return name + ".yml"


def push_workflow(repository_url, branch, pipeline_name, workflow):
    token = os.environ.get("GITHUB_PAT")
    if not token:
        raise RuntimeError("GITHUB_PAT is missing from the agent environment.")
    if not isinstance(branch, str) or not branch.strip():
        raise ValueError("branch_name is required.")

    filename = normalize_pipeline_name(pipeline_name)
    workflow_path = f".github/workflows/{filename}"
    with tempfile.TemporaryDirectory(prefix="pipeline-agent-") as temporary_directory:
        temporary_path = Path(temporary_directory)
        repository_dir = temporary_path / "repository"
        askpass = temporary_path / "git-askpass"
        askpass.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  *sername*) printf '%s\\n' 'x-access-token' ;;\n"
            "  *) printf '%s\\n' \"$GITHUB_PAT\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        askpass.chmod(0o700)

        environment = os.environ.copy()
        environment.update(
            {
                "GIT_ASKPASS": str(askpass),
                "GIT_TERMINAL_PROMPT": "0",
                "GITHUB_PAT": token,
                "GIT_CONFIG_NOSYSTEM": "1",
            }
        )

        branch_check = subprocess.run(
            ["git", "check-ref-format", "--branch", branch],
            text=True,
            capture_output=True,
            check=False,
        )
        if branch_check.returncode:
            raise ValueError("branch_name is not a valid Git branch name.")

        _git(
            temporary_path,
            ["-c", "credential.helper=", "clone", "--depth=1", repository_url, str(repository_dir)],
            environment,
        )
        remote_branch = _git(
            repository_dir,
            ["ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
            environment,
        )
        if remote_branch.stdout.strip():
            _git(repository_dir, ["fetch", "--depth=1", "origin", branch], environment)
            _git(repository_dir, ["checkout", "-B", branch, "FETCH_HEAD"], environment)
        else:
            _git(repository_dir, ["checkout", "-b", branch], environment)

        destination = repository_dir / workflow_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(workflow, encoding="utf-8")
        _git(repository_dir, ["config", "user.name", os.environ.get("GIT_COMMIT_NAME", "Pipeline Agent")], environment)
        _git(repository_dir, ["config", "user.email", os.environ.get("GIT_COMMIT_EMAIL", "pipeline-agent@users.noreply.github.com")], environment)
        _git(repository_dir, ["add", "--", workflow_path], environment)
        staged = _git(repository_dir, ["diff", "--cached", "--quiet"], environment, check=False)
        if staged.returncode == 0:
            commit_sha = _git(repository_dir, ["rev-parse", "HEAD"], environment).stdout.strip()
        else:
            _git(
                repository_dir,
                ["commit", "-m", f"Add generated workflow {filename}"],
                environment,
            )
            commit_sha = _git(repository_dir, ["rev-parse", "HEAD"], environment).stdout.strip()

        _git(
            repository_dir,
            ["push", "origin", f"HEAD:refs/heads/{branch}"],
            environment,
        )

    parsed_repository_url = urlsplit(repository_url)
    repository_parts = [part for part in parsed_repository_url.path.strip("/").split("/") if part]
    if parsed_repository_url.hostname and len(repository_parts) == 2:
        repository_web_url = (
            f"https://{parsed_repository_url.hostname}/{repository_parts[0]}/"
            f"{repository_parts[1].removesuffix('.git')}"
        )
    else:
        repository_web_url = repository_url
    return {
        "repository": repository_web_url,
        "branch": branch,
        "pipeline_file": workflow_path,
        "commit_sha": commit_sha,
        "commit_url": f"{repository_web_url}/commit/{commit_sha}",
        "pull_request_url": None,
    }