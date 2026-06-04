#!/usr/bin/env bash
# Production Cleanup Commands for Placement Mail Tracker
# IMPORTANT: Review carefully before running.

echo "Starting safe cleanup of obsolete modules and temporary artifacts..."

# 1. Remove obsolete root scripts and reports
echo "Removing obsolete root scripts..."
rm -f test_generator.py
rm -f regression_report.txt
rm -f src/placement_mail_tracker/app.py

# 2. Remove obsolete directories (scripts, redundant wrapper modules)
echo "Removing obsolete directories..."
rm -rf scripts/
rm -rf src/placement_mail_tracker/filters/
rm -rf src/placement_mail_tracker/gemini/

# 3. Clear Caches
echo "Clearing Python and Pytest caches..."
find . -type d -name "__pycache__" -exec rm -rf {} +
rm -rf .pytest_cache/
rm -rf .ruff_cache/

# 4. Clear large log files (Optional: uncomment to enable)
# echo "Clearing runtime logs..."
# > logs/app.log

echo "Cleanup complete! The project is now lean and production-ready."
echo "Note: Remember to update imports in runner.py to point directly to 'ai.gemini_extractor'."
