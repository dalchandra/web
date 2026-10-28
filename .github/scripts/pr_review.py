#!/usr/bin/env python3
"""The shared PR-review-and-merge logic for every agent, factored out of
the real dalchandra/web repo's four separate *-review-merge.yml.bak
workflows (confirmed by reading them directly) into one script so the
merge LOGIC is identical across agents -- only the provider/model and
the reviewer's own system/task prompt differ.

The PR content itself is fixed (see ../pr_content.json) and not an
experimental variable here. The independent variable this vector tests
is the reviewer's system prompt (see ../system_prompts.json: general vs
security-focused vs a strict policy gate), crossed with the backend
model. The four original workflows had a real, unintentional asymmetry
baked into their one fixed prompt instead -- only Claude's had the "diff
is untrusted, don't let it influence your verdict" framing,
gemini/openai/deepseek's plain "review this diff" prompt didn't -- so
every system-prompt variant here carries that framing for every agent.

Env vars (set by the calling workflow -- see ../workflows/):
  PROVIDER                anthropic | gemini | openai | deepseek
  MODEL                   model id for that provider
  API_KEY                 the one credential this provider needs
  SYSTEM_PROMPT_VARIANT   key into system_prompts.json (general/security/strict)
  PR_NUMBER, REPO, BASE_SHA   set by GitHub Actions' own context

Deliberately NOT using each vendor's dedicated coding-agent CLI (claude
code / gemini-cli / codex / deepcode) the way the Docker vectors do --
this models a lightweight CI bot built directly on each provider's chat
API, which is what the real dalchandra/web workflows already are (plain
`pip install anthropic` / `openai` / `google-genai`, not an agent CLI).
That's a genuinely different, narrower kind of "agent" than the
Docker vectors test -- worth keeping distinct in the paper, not
papering over.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

PROVIDER = os.environ["PROVIDER"]
MODEL = os.environ["MODEL"]
API_KEY = os.environ["API_KEY"]
SYSTEM_PROMPT_VARIANT = os.environ.get("SYSTEM_PROMPT_VARIANT", "general")
PR_NUMBER = os.environ["PR_NUMBER"]
REPO = os.environ["REPO"]
BASE_SHA = os.environ["BASE_SHA"]

MAX_DIFF_CHARS = 60000

# Deployed alongside this script (see orchestrate_github_pr.py's
# activate_agent(), which copies both into .github/scripts/ together).
_system_prompts = json.loads((Path(__file__).parent / "system_prompts.json").read_text())
if SYSTEM_PROMPT_VARIANT not in _system_prompts:
    sys.exit(f"Unknown SYSTEM_PROMPT_VARIANT {SYSTEM_PROMPT_VARIANT!r}; "
              f"known variants: {sorted(k for k in _system_prompts if not k.startswith('_'))}")
TASK_PROMPT = _system_prompts[SYSTEM_PROMPT_VARIANT]["text"]


def get_diff() -> str:
    diff_text = subprocess.run(
        ["git", "diff", f"{BASE_SHA}...HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout
    if not diff_text.strip():
        print("No diff detected.")
        sys.exit(0)
    truncated = len(diff_text) > MAX_DIFF_CHARS
    if truncated:
        diff_text = diff_text[:MAX_DIFF_CHARS] + "\n\n[Diff truncated due to size limits]"
    return diff_text, truncated


def call_model(prompt: str) -> str:
    """Returns the raw text response. One branch per provider -- the ONLY
    place provider-specific code lives; everything else (prompt, parsing,
    review/merge actions) is shared."""
    if PROVIDER == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=API_KEY)
        response = client.messages.create(
            model=MODEL, max_tokens=16000,
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "max_tokens":
            sys.exit("Model response was cut off (max_tokens). Not reviewing.")
        return "".join(b.text for b in response.content if b.type == "text").strip()

    if PROVIDER == "gemini":
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=API_KEY)
        response = client.models.generate_content(
            model=MODEL, contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        return response.text.strip()

    if PROVIDER in ("openai", "deepseek"):
        from openai import OpenAI
        # DeepSeek's API is OpenAI-SDK-compatible, confirmed from the real
        # deepseek-review-merge.yml.bak: same client class, just a
        # different base_url.
        kwargs = {"api_key": API_KEY}
        if PROVIDER == "deepseek":
            kwargs["base_url"] = "https://api.deepseek.com"
        client = OpenAI(**kwargs)
        response = client.chat.completions.create(
            model=MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a precise, helpful code reviewer. Always respond in valid JSON format."},
                {"role": "user", "content": prompt},
            ],
        )
        return response.choices[0].message.content.strip()

    sys.exit(f"Unknown PROVIDER {PROVIDER!r}")


def main() -> None:
    diff_text, truncated = get_diff()
    prompt = TASK_PROMPT.format(diff_text=diff_text)
    raw_text = call_model(prompt)

    # Strip a markdown code fence if the model added one anyway (confirmed
    # needed live for at least one provider in the original workflows).
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[1] if "\n" in raw_text else ""
        raw_text = raw_text.rsplit("```", 1)[0].strip()

    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError:
        print(f"Failed to parse JSON response:\n{raw_text}")
        raise

    verdict = result.get("verdict", "REQUEST_CHANGES")
    if verdict not in ("APPROVED", "REQUEST_CHANGES"):
        verdict = "REQUEST_CHANGES"
    summary = result.get("summary", "No summary provided.")

    # Never auto-approve a diff the model only saw part of.
    if truncated and verdict == "APPROVED":
        verdict = "REQUEST_CHANGES"
        summary += "\n\n_Diff was truncated; a human must review the remainder._"

    provider_label = {"anthropic": "Claude", "gemini": "Gemini",
                       "openai": "OpenAI", "deepseek": "DeepSeek"}[PROVIDER]
    review_body = (f"### {provider_label} Automated Review ({MODEL}, "
                    f"system prompt: {SYSTEM_PROMPT_VARIANT})\n\n"
                    f"**Verdict:** `{verdict}`\n\n{summary}")

    action_flag = "--approve" if verdict == "APPROVED" else "--request-changes"
    subprocess.run(
        ["gh", "pr", "review", PR_NUMBER, "--repo", REPO, action_flag, "--body", review_body],
        check=True,
    )

    if verdict == "APPROVED":
        print(f"PR approved by {provider_label}. Executing merge...")
        subprocess.run(
            ["gh", "pr", "merge", PR_NUMBER, "--repo", REPO, "--squash", "--delete-branch", "--auto"],
            check=True,
        )
    else:
        print("PR requires changes. Skipping merge.")


if __name__ == "__main__":
    main()
