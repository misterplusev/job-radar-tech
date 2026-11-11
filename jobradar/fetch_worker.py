"""Bounded provider subprocess. It fetches public careers data; it never publishes."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def fetch_provider(provider, run_id: str, timeout: int = 180):
    from .models import Job
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="jobradar-fetch-") as folder:
        request_file, output_file = Path(folder)/"request.json", Path(folder)/"jobs.json"
        opts = {k: v for k, v in vars(provider).items() if k != "slug" and not k.startswith("_")}
        request_file.write_text(json.dumps({"source": provider.source, "slug": provider.slug,
                                           "options": opts, "run_id": run_id}), encoding="utf-8")
        environment = {k: v for k, v in os.environ.items()
                       if not k.startswith(("SUPABASE_", "DISCORD_", "ANTHROPIC_"))
                       and k not in ("GITHUB_TOKEN", "GH_TOKEN")}
        result = subprocess.run([sys.executable, "-m", "jobradar.fetch_worker",
                                 str(request_file), str(output_file)], cwd=root, env=environment,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                timeout=timeout, text=True, encoding="utf-8")
        if result.returncode:
            raise RuntimeError(f"Provider subprocess failed: {result.stderr[-1200:]}")
        if output_file.stat().st_size > 64 * 1024 * 1024:
            raise RuntimeError("Provider output exceeds 64 MiB safety limit")
        return [Job(**row) for row in json.loads(output_file.read_text(encoding="utf-8"))]


def main():
    if sys.platform == "linux":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    from .providers import build_providers
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    providers = build_providers({request["source"]: [dict(slug=request["slug"], **request["options"])]})
    if len(providers) != 1:
        raise ValueError("Expected one configured provider")
    jobs = providers[0].fetch(request["run_id"])
    Path(sys.argv[2]).write_text(json.dumps([job.to_row() for job in jobs]), encoding="utf-8")


if __name__ == "__main__":
    main()
