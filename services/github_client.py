"""
services/github_client.py — GitHub API client para leer archivos y abrir PRs.
"""

import base64
import logging
import os
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "carreraprogrammer/daniel15k-api")
GH_BASE      = "https://api.github.com"
TIMEOUT      = 15


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_file(path: str, ref: str = "main") -> str | None:
    url = f"{GH_BASE}/repos/{GITHUB_REPO}/contents/{path}"
    try:
        r = httpx.get(url, headers=_headers(), params={"ref": ref}, timeout=TIMEOUT)
        r.raise_for_status()
        content = r.json().get("content", "")
        return base64.b64decode(content).decode("utf-8")
    except Exception as e:
        logger.warning("[github_client] get_file %s failed: %s", path, e)
        return None


def get_file_sha(path: str, ref: str = "main") -> str | None:
    url = f"{GH_BASE}/repos/{GITHUB_REPO}/contents/{path}"
    try:
        r = httpx.get(url, headers=_headers(), params={"ref": ref}, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json().get("sha")
    except Exception as e:
        logger.warning("[github_client] get_file_sha %s failed: %s", path, e)
        return None


def get_main_sha() -> str | None:
    url = f"{GH_BASE}/repos/{GITHUB_REPO}/git/ref/heads/main"
    try:
        r = httpx.get(url, headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()["object"]["sha"]
    except Exception as e:
        logger.error("[github_client] get_main_sha failed: %s", e)
        return None


def create_branch(branch_name: str, from_sha: str) -> bool:
    url = f"{GH_BASE}/repos/{GITHUB_REPO}/git/refs"
    try:
        r = httpx.post(url, headers=_headers(), json={
            "ref": f"refs/heads/{branch_name}",
            "sha": from_sha,
        }, timeout=TIMEOUT)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.error("[github_client] create_branch %s failed: %s", branch_name, e)
        return False


def update_file(path: str, content: str, message: str, branch: str) -> bool:
    sha = get_file_sha(path, ref=branch)
    url = f"{GH_BASE}/repos/{GITHUB_REPO}/contents/{path}"
    body = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "branch":  branch,
    }
    if sha:
        body["sha"] = sha

    try:
        r = httpx.put(url, headers=_headers(), json=body, timeout=TIMEOUT)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.error("[github_client] update_file %s failed: %s", path, e)
        return False


def create_pr(title: str, body: str, branch: str) -> str | None:
    url = f"{GH_BASE}/repos/{GITHUB_REPO}/pulls"
    try:
        r = httpx.post(url, headers=_headers(), json={
            "title": title,
            "body":  body,
            "head":  branch,
            "base":  "main",
        }, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json().get("html_url")
    except Exception as e:
        logger.error("[github_client] create_pr failed: %s", e)
        return None


def hotfix_main(error_id: int, file_path: str, new_content: str,
                fix_description: str) -> bool:
    """Push directo a main — Railway despliega automáticamente."""
    commit_msg = (
        f"hotfix: {fix_description[:70]}\n\n"
        f"Auto-hotfix for error_report #{error_id}\n\n"
        f"Co-Authored-By: Debugger Agent <noreply@anthropic.com>"
    )
    return update_file(file_path, new_content, commit_msg, "main")


def open_fix_pr(error_id: int, error_hash: str, file_path: str, new_content: str,
                diagnosis: str, fix_description: str) -> str | None:
    """Crea branch + PR para revisión. Retorna la PR URL o None."""
    main_sha = get_main_sha()
    if not main_sha:
        return None

    branch = f"fix/auto-{error_hash}"
    if not create_branch(branch, main_sha):
        return None

    commit_msg = f"fix: {fix_description[:70]}\n\nAuto-fix for error_report #{error_id}"
    if not update_file(file_path, new_content, commit_msg, branch):
        return None

    pr_body = (
        f"## Diagnóstico\n{diagnosis}\n\n"
        f"## Fix aplicado\n`{file_path}` — {fix_description}\n\n"
        f"**Error report ID:** #{error_id}  \n"
        f"**Hash:** `{error_hash}`\n\n"
        f"🤖 Auto-generado por el agente debugger."
    )

    return create_pr(
        title=f"fix: {fix_description[:60]}",
        body=pr_body,
        branch=branch,
    )
