"""Phase 12: Regression Test Runner."""
import os
import subprocess
import sys

def main():
    print("Starting Placement Mail Tracker Regression Suite...")
    
    # Run pytest with coverage
    result = subprocess.run(
        ["pytest", "tests/", "--cov=src", "--cov-report=term-missing"],
        capture_output=True,
        text=True
    )
    
    print(result.stdout)
    
    if result.returncode == 0:
        print("All tests passed successfully!")
    else:
        print("Some tests failed. Check the output above.")
        
    with open("regression_report.txt", "w", encoding="utf-8") as f:
        f.write(result.stdout)
        
if __name__ == "__main__":
    main()
