import os
import sys

# Ensure the local src directory is in the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from pykusto_language import parse

def run_analysis_demo():
    print("=== KQL Query Analysis Demo ===\n")

    # Sample query with literals and complex structure
    query = """
    SecurityEvent 
    | where Account == 'admin' and Computer == "DC01.contoso.com"
    | where EventID == 4624
    | join kind=inner (
        SigninLogs | where IPAddress == "192.168.1.100"
    ) on Account
    | project TimeGenerated, Account, IPAddress
    """

    result = parse(query)

    # 1. Table Extraction (Syntactic)
    # Identifies data sources without needing a live connection or schema
    print("--- Table Extraction (Syntactic) ---")
    tables = result.get_referenced_tables()
    print(f"Referenced tables: {tables}\n")

    # 2. Table Extraction (Semantic)
    # Using a schema allows the .NET engine to resolve ambiguous names and verify symbols
    print("--- Table Extraction (Semantic) ---")
    mock_schema = {
        "SecurityEvent": ["Account", "Computer", "EventID"],
        "SigninLogs": ["Account", "IPAddress"]
    }
    semantic_tables = result.get_referenced_tables(schema=mock_schema)
    print(f"Verified tables: {semantic_tables}\n")

    # 3. Literal Masking
    # Safe redaction of sensitive strings for logging or sharing queries
    print("--- Literal Masking ---")
    masked = result.mask_literals()
    print("Masked query (Sensitive strings redacted):")
    print(masked)
    print("\nNote: String literals like 'admin' and '192.168.1.100' are masked, but numeric values like EventID 4624 are preserved.")

if __name__ == "__main__":
    run_analysis_demo()
