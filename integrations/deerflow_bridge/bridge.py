from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


@dataclass
class DeerflowBridgeConfig:
    backend_dir: Path
    harness_dir: Path
    python_bin: Path
    enabled: bool = True


class DeerflowBridge:
    def __init__(self) -> None:
        self.config = self._build_config()

    def _build_config(self) -> DeerflowBridgeConfig:
        root_dir = Path(__file__).resolve().parents[2]
        backend_dir = Path(os.getenv("DEERFLOW_BACKEND_DIR", str(root_dir / "deer-flow" / "backend"))).expanduser()
        harness_dir = backend_dir / "packages" / "harness"
        python_bin = Path(os.getenv("DEERFLOW_PYTHON_BIN", str(backend_dir / ".venv" / "bin" / "python"))).expanduser()
        enabled = backend_dir.exists() and harness_dir.exists()
        return DeerflowBridgeConfig(
            backend_dir=backend_dir.absolute(),
            harness_dir=harness_dir.absolute(),
            python_bin=python_bin.absolute(),
            enabled=enabled,
        )

    def _ensure_import_path(self) -> None:
        harness_path = str(self.config.harness_dir)
        if harness_path not in sys.path:
            sys.path.insert(0, harness_path)

    def _load_client_class(self):
        if not self.config.enabled:
            raise FileNotFoundError(f"DeerFlow backend not found: {self.config.backend_dir}")
        self._ensure_import_path()
        from deerflow.client import DeerFlowClient  # type: ignore

        return DeerFlowClient

    def _run_embedded_python(self, code: str) -> dict:
        if not self.config.python_bin.exists():
            raise FileNotFoundError(f"DeerFlow python not found: {self.config.python_bin}")
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{self.config.harness_dir}:{env.get('PYTHONPATH', '')}".rstrip(":")
        proc = subprocess.run(
            [str(self.config.python_bin), "-c", code],
            cwd=str(self.config.backend_dir),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout).strip() or "embedded deerflow call failed")
        import json

        return json.loads(proc.stdout.strip() or "{}")

    def _build_client(self):
        DeerFlowClient = self._load_client_class()
        return DeerFlowClient()

    def health(self) -> dict:
        payload = {
            "status": "ok",
            "service": "deerflow-bridge",
            "mode": "embedded" if self.config.enabled else "stub",
            "backend_dir": str(self.config.backend_dir),
            "harness_dir": str(self.config.harness_dir),
            "python_bin": str(self.config.python_bin),
        }
        if not self.config.enabled:
            payload["reason"] = "deer-flow backend directory not found"
            return payload
        try:
            skills = self.list_skills()
            payload["skills_count"] = len(skills.get("skills", []))
            payload["client_ready"] = skills.get("mode") in {"embedded", "subprocess"}
        except Exception as exc:
            payload["client_ready"] = False
            payload["status"] = "degraded"
            payload["error"] = str(exc)
        return payload

    def list_skills(self) -> dict:
        if not self.config.enabled:
            return {"mode": "stub", "skills": []}
        try:
            result = self._run_embedded_python(
                "\n".join(
                    [
                        "import json",
                        "from deerflow.client import DeerFlowClient",
                        "client = DeerFlowClient()",
                        "print(json.dumps(client.list_skills(enabled_only=False), ensure_ascii=False))",
                    ]
                )
            )
            result["mode"] = "subprocess"
            return result
        except Exception as exc:
            return {
                "mode": "degraded",
                "skills": [],
                "error": str(exc),
            }

    def ensure_thread(self, thread_id: str, metadata: dict | None = None) -> dict:
        if not self.config.enabled:
            return {
                "mode": "stub",
                "thread_id": thread_id,
                "workspace_ready": False,
            }
        try:
            result = self._run_embedded_python(
                "\n".join(
                    [
                        "import json",
                        "from deerflow.config.paths import get_paths",
                        f"thread_id = {thread_id!r}",
                        f"metadata = {metadata or {}!r}",
                        "paths = get_paths()",
                        "paths.ensure_thread_dirs(thread_id)",
                        "thread_dir = paths.thread_dir(thread_id)",
                        "workspace = paths.sandbox_work_dir(thread_id)",
                        "uploads = paths.sandbox_uploads_dir(thread_id)",
                        "outputs = paths.sandbox_outputs_dir(thread_id)",
                        "if metadata:",
                        "    (thread_dir / 'vibe_context.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')",
                        "print(json.dumps({",
                        "    'thread_id': thread_id,",
                        "    'thread_dir': str(thread_dir),",
                        "    'workspace_path': str(workspace),",
                        "    'uploads_path': str(uploads),",
                        "    'outputs_path': str(outputs),",
                        "    'workspace_ready': True,",
                        "}, ensure_ascii=False))",
                    ]
                )
            )
            result["mode"] = "subprocess"
            return result
        except Exception as exc:
            return {
                "mode": "degraded",
                "thread_id": thread_id,
                "workspace_ready": False,
                "error": str(exc),
            }

    def get_memory(self) -> dict:
        if not self.config.enabled:
            return {"mode": "stub", "memory": {}}
        try:
            result = self._run_embedded_python(
                "\n".join(
                    [
                        "import json",
                        "from deerflow.client import DeerFlowClient",
                        "client = DeerFlowClient()",
                        "print(json.dumps({'memory': client.get_memory()}, ensure_ascii=False))",
                    ]
                )
            )
            result["mode"] = "subprocess"
            return result
        except Exception as exc:
            return {
                "mode": "degraded",
                "memory": {},
                "error": str(exc),
            }

    def invoke_analysis_skill(self, skill_name: str, payload: dict) -> dict:
        thread_id = payload.get("thread_id") or f"vibe_{uuid4().hex[:10]}"
        message = payload.get("message") or payload.get("goal") or ""

        if not self.config.enabled:
            return {
                "skill_name": skill_name,
                "payload": payload,
                "mode": "stub",
                "thread_id": thread_id,
            }

        try:
            if skill_name == "ensure_thread":
                return self.ensure_thread(thread_id=thread_id, metadata=payload.get("metadata"))

            if skill_name == "list_skills":
                result = self.list_skills()
                result["thread_id"] = thread_id
                return result

            if skill_name == "get_memory":
                result = self.get_memory()
                result["thread_id"] = thread_id
                return result

            if skill_name == "chat":
                result = self._run_embedded_python(
                    "\n".join(
                        [
                            "import json",
                            "from deerflow.client import DeerFlowClient",
                            f"thread_id = {thread_id!r}",
                            f"message = {message!r}",
                            "client = DeerFlowClient()",
                            "response = client.chat(message, thread_id=thread_id)",
                            "print(json.dumps({'response': response}, ensure_ascii=False))",
                        ]
                    )
                )
                result.update(
                    {
                        "mode": "subprocess",
                        "thread_id": thread_id,
                        "skill_name": skill_name,
                        "message": message,
                    }
                )
                return result
        except Exception as exc:
            return {
                "mode": "degraded",
                "thread_id": thread_id,
                "skill_name": skill_name,
                "payload": payload,
                "error": str(exc),
            }

        return {
            "mode": "embedded-fallback",
            "thread_id": thread_id,
            "skill_name": skill_name,
            "message": message,
            "note": "当前桥接层已接通 DeerFlow client，但业务 skill 映射尚未细化",
        }
