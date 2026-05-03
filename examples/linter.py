import os
import sys

# Ensure the local src directory is in the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from pykusto_language import format, validate

def run_linter_demo():
    print("=== KQL Linter Demo ===\n")

    # 1. Formatting Demo
    messy_query = "SecurityEvent|where TimeGenerated > ago(1h)|   summarize count() by Account,EventID | project Account, Count=count_"
    print(f"Original messy query:\n{messy_query}\n")
    
    formatted = format(messy_query)
    print(f"Formatted query:\n{formatted}\n")

    # 2. Validation Demo
    broken_query = "SecurityEvent | where EventID == | project TimeGenerated"
    print(f"Checking for errors in: {broken_query}")
    
    diagnostics = validate(broken_query)
    if diagnostics:
        print("❌ Found issues:")
        for d in diagnostics:
            # Diagnostics include 'start', 'length', 'message', 'severity', etc.
            print(f"  - [{d['severity']}] at char {d['start']}: {d['message']}")
    else:
        print("✅ No syntactic errors found.")

if __name__ == "__main__":
    run_linter_demo()
