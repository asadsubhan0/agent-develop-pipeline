# GitHub Actions Pipeline Agent

A small command-line agent that uses the OpenAI API to generate GitHub Actions workflow files. It reads `enterprise-skill.yml` as policy context and writes accepted workflows to `GitHubPipeline-Output/`.

## Prerequisites

- Python 3.11 or later
- An OpenAI API key from [the OpenAI API key page](https://platform.openai.com/api-keys)

## Setup on Windows

1. Create an OpenAI API key.
2. In the project directory, create a local `.env` file based on `.env.example` and add your key:

   ```powershell
   Copy-Item .env.example .env
   ```

   Open `.env` and replace `put_your_openai_api_key_here` with your key. Keep the key private; `.env` is excluded from Git.

3. From this project directory, create and activate a Python environment and install the dependencies:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

4. Run the agent:

   ```powershell
   python agent.py
   ```

Enter a description when prompted. A validated workflow is written to `GitHubPipeline-Output/pipeline-<timestamp>.yml`. The agent does not run or publish the generated workflow; review it before using it.

## Configuration

The default model is `gpt-4.1-mini`. To use a different supported OpenAI model, change `OPENAI_MODEL` in `.env`. Alternatively, provide the key through the current PowerShell session without saving it to a file:

```powershell
$env:OPENAI_API_KEY = "your_api_key"
python agent.py
```

The agent rejects invalid workflow YAML, actions not listed in `allowed_action_repositories`, workflows over `max_file_bytes`, and detected broad workspace-delete commands. It does not locally check whether shell commands print secrets, so review generated workflows carefully before using them.