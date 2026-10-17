import argparse
import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path


def run_command(command, cwd, env):
    sam_path = shutil.which("sam", path=env.get("PATH"))

    command[0] = sam_path

    safe_command = ["<redacted>" if arg.startswith("AdminToken=") else arg for arg in command]

    print(f"\nRunning: {' '.join(safe_command)}")

    try:
        subprocess.run(command, cwd=cwd, env=env, check=True)
    except subprocess.CalledProcessError as exc:
        print(f"Command failed with exit code {exc.returncode}.", file=sys.stderr)
        sys.exit(exc.returncode)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("admin_token", nargs="?")
    parser.add_argument("--prompt", action="store_true")
    args = parser.parse_args()

    if args.prompt and args.admin_token:
        parser.error("Use either --prompt or the positional token.")

    if args.prompt:
        admin_token = getpass.getpass("Admin secret token: ")
    elif args.admin_token:
        admin_token = args.admin_token
    else:
        parser.error("Provide a token or use --prompt.")

    if len(admin_token) < 32:
        parser.error("Admin token must contain at least 32 characters.")

    project_dir = Path(__file__).resolve().parent

    template = project_dir / "template.yaml"
    if not template.exists():
        template = project_dir / "template.yml"

    if not template.exists():
        print("Error: template.yaml or template.yml not found.", file=sys.stderr)
        sys.exit(2)

    if not (project_dir / "samconfig.toml").exists():
        print("Error: samconfig.toml not found.", file=sys.stderr)
        sys.exit(2)

    env = os.environ.copy()
    env.pop("SAM_DEBUG", None)

    run_command(
        ["sam", "validate", "--lint", "--template-file", str(template)],
        project_dir,
        env,
    )

    run_command(
        ["sam", "build", "--template-file", str(template)],
        project_dir,
        env,
    )

    run_command(
        [
            "sam",
            "deploy",
            "--config-file",
            "samconfig.toml",
            "--config-env",
            "default",
            "--parameter-overrides",
            f"AdminToken={admin_token}",
        ],
        project_dir,
        env,
    )

    admin_token = None
    print("\nDeployment completed successfully.")


if __name__ == "__main__":
    main()
