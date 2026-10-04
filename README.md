# GitHub Pipeline Agent

An authenticated A2A JSON-RPC service. Orchestra discovers the `github_pipeline` capability, submits pipeline requirements, and receives a structured result after the agent commits and pushes `.github/workflows/<pipeline_name>.yml` to the target branch.

## OpenShift Deployment

The manifests target namespace `dis-poc-514020`. The BuildConfig builds branch `release/1.0` from the agent source repository and publishes the image to an ImageStream. The Deployment rolls out new images through the OpenShift ImageStream trigger; the Service is internal and the HTTPS Route exposes the A2A service.

1. Create `k8s/secret.yaml` from the example and fill all three values locally:

   ```powershell
   Copy-Item k8s/secret.example.yaml k8s/secret.yaml
   ```

   Set `OPENAI_API_KEY`, `GITHUB_PAT`, and a long random `A2A_BEARER_TOKEN`. `k8s/secret.yaml` is git-ignored. Do not commit it. The GitHub token needs read/write Contents access to whichever target repository is selected and Metadata read access.

2. Log in to OpenShift and confirm the namespace exists and your account can build and deploy there:

   ```powershell
   oc project dis-poc-514020
  oc whoami
  ```

3. Create the runtime secret, then apply the image and application resources:

  ```powershell
  oc apply -f k8s/secret.yaml
  oc apply -f k8s/imagestream.yaml -f k8s/buildconfig.yaml
  oc apply -f k8s/deployment.yaml -f k8s/service.yaml -f k8s/route.yaml
   oc start-build github-pipeline-agent --follow
  oc rollout status deployment/github-pipeline-agent
   oc get route github-pipeline-agent
   ```

The configured route host is `github-pipeline-agent-dis-poc-514020.apps.ocp.systemsltd.local`. If the cluster uses a different router domain or that host is unavailable, remove `spec.host` in `k8s/route.yaml`, apply it, and use the host returned by `oc get route`. The standard Kubernetes Deployment is updated automatically by the OpenShift ImageStream trigger annotation after a successful build.

The BuildConfig clones the public agent source repository, so it does not need a GitHub PAT. If that source repository becomes private, create a separate read-only Git source secret and reference it under `spec.source.sourceSecret`; do not use the runtime `GITHUB_PAT` for the build. The runtime PAT is used by the agent to push workflows to target repositories.

## Orchestra Integration

Share these values with the parent-agent team:

- A2A endpoint: `https://github-pipeline-agent-dis-poc-514020.apps.ocp.systemsltd.local/a2a`
- Discovery: send JSON-RPC `SendMessage` with `skill: "list_capabilities"`; the result contains the tool manifest.
- Tool ID: `ci.github.github_pipeline` (legacy alias `ci.github_actions.github_pipeline` is also accepted for execution).
- Authentication: HTTP `Authorization: Bearer <A2A_BEARER_TOKEN>`.
- Content type: `application/json`.
- Agent card: `https://github-pipeline-agent-dis-poc-514020.apps.ocp.systemsltd.local/.well-known/agent-card.json`
- Health check: `https://github-pipeline-agent-dis-poc-514020.apps.ocp.systemsltd.local/healthz`.

The request is JSON-RPC 2.0 `SendMessage`; the invocation can be passed as `params.skill` plus `params.arguments`, message metadata, or a message data part:

```json
{
  "jsonrpc": "2.0",
  "id": "request-1",
  "method": "SendMessage",
  "params": {
    "skill": "ci.github.github_pipeline",
    "arguments": {
      "prompt": "Build the Java 8 service, run SonarQube, then build and deploy the OCP image.",
      "pipeline_name": "java-service-ci",
      "branch_name": "agent/pipeline-development",
      "repo_url": "https://github.com/example/service"
    }
  }
}
```

The response is a JSON-RPC A2A message. Its `result.message.parts[0].data` contains the success/failure object with repository, branch, workflow path, commit SHA, and commit URL. A push does not create a pull request, so `pull_request_url` is `null`.

## Repository Selection

An explicit `repo_url` takes precedence; otherwise `organization` + `repository` (or `org_name` + `repo_name`) are combined. A GitHub URL or labeled `repo: owner/name` in the prompt is also recognized. If none is supplied, [`enterprise-skill.yml`](enterprise-skill.yml) selects `https://github.com/Micro-DevOps/service-a`. `branch_name` is required. The workflow is committed only under `.github/workflows/`.

The service needs outbound network access to `api.openai.com` and the selected GitHub host. The OpenShift router must permit the parent agent to reach the route; the parent Integration Hub may also require an endpoint allowlist entry. Review each generated workflow before running it.

For local development, create `.env` from `.env.example`, install `requirements.txt`, then run `uvicorn server:app --host 127.0.0.1 --port 8080`.