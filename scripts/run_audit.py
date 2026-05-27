"""Validation and audit execution script.

This script runs the entire pytest suite, checks local files for security risks,
verifies environment setup, and outputs a clean validation report.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_tests() -> bool:
    """Run pytest suite and return True if all tests pass."""
    print("\n==================================================")
    # Check if pytest is in virtual environment
    pytest_bin = PROJECT_ROOT / ".venv" / "Scripts" / "pytest.exe"
    if not pytest_bin.exists():
        pytest_bin = Path("pytest")  # Fallback to system path

    print(f"[*] Running Pytest Suite using: {pytest_bin}")
    try:
        result = subprocess.run(
            [str(pytest_bin), "-v", "--tb=short"],
            cwd=str(PROJECT_ROOT),
            check=False,
        )
        return result.returncode == 0
    except FileNotFoundError:
        print("[ERROR] Pytest executable not found. Make sure virtual environment is active.")
        return False


def run_security_scan() -> list[str]:
    """Scan codebase for security warning patterns."""
    print("\n==================================================")
    print("[SEC] Running Security & Secrets Check")
    warnings: list[str] = []

    # Check gitignore includes env files
    gitignore_path = PROJECT_ROOT / ".gitignore"
    if not gitignore_path.exists():
        warnings.append("No .gitignore found")
    else:
        content = gitignore_path.read_text(encoding="utf-8")
        if ".env" not in content:
            warnings.append(".env is not ignored in .gitignore!")
        if "credentials.json" not in content and "config/*.json" not in content:
            warnings.append("credentials.json is not ignored in .gitignore!")
        if "token.json" not in content and "config/*.json" not in content:
            warnings.append("token.json is not ignored in .gitignore!")

    # Check for hardcoded API keys
    key_pattern = r"(api_key|token|password|secret|pwd)\s*=\s*['\"][a-zA-Z0-9_\-]{10,}['\"]"
    for py_file in PROJECT_ROOT.glob("src/**/*.py"):
        try:
            code = py_file.read_text(encoding="utf-8")
            if any(term in code for term in ["api_key =", "token =", "password ="]):
                # Simple warning
                warnings.append(f"Potential hardcoded credential or secret key in {py_file.name}")
        except Exception:
            pass

    if not warnings:
        print("[OK] No security issues or leaked secrets found!")
    else:
        for w in warnings:
            print(f"[WARN] {w}")

    return warnings


def verify_project_structure() -> list[str]:
    """Verify directories and configurations are correctly set up."""
    print("\n==================================================")
    print("[DIR] Checking Project Structure")
    missing: list[str] = []

    required_dirs = ["src", "tests", "config", "data", "logs"]
    for d in required_dirs:
        dir_path = PROJECT_ROOT / d
        if not dir_path.exists():
            missing.append(f"Directory '{d}' is missing")
            print(f"[FAIL] Missing: {d}/")
        else:
            print(f"[OK] Found: {d}/")

    required_files = ["README.md", "pyproject.toml", "requirements.txt", ".env.example"]
    for f in required_files:
        file_path = PROJECT_ROOT / f
        if not file_path.exists():
            missing.append(f"File '{f}' is missing")
            print(f"[FAIL] Missing: {f}")
        else:
            print(f"[OK] Found: {f}")

    return missing


def main() -> int:
    """Run all validation audit steps."""
    print("==================================================")
    print("--- PLACEMENT MAIL TRACKER - VALIDATION & AUDIT ---")
    print("==================================================")

    structure_errors = verify_project_structure()
    security_warnings = run_security_scan()
    tests_passed = run_tests()

    print("\n==================================================")
    print("--- AUDIT SUMMARY ---")
    print("==================================================")
    print(f"Project Structure:  {'[FAIL]' if structure_errors else '[PASS]'}")
    print(f"Security & Secrets: {'[WARN]' if security_warnings else '[PASS]'}")
    print(f"Pytest Suite:       {'[PASS]' if tests_passed else '[FAIL]'}")
    print("==================================================")

    if not tests_passed or structure_errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
