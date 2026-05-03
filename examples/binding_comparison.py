import os
import sys
import time

# Ensure the local src directory is in the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from pykusto_language import parse

def run_comparison():
    print("=== KQL Binding Comparison: Syntactic vs. Semantic ===\n")

    query = """
    DeviceProcessEvents 
    | where ProcessCommandLine contains "powershell" 
    | where DeviceName == "COMP-01"
    | project TimeGenerated, DeviceName, FileName, ProcessCommandLine
    """
    
    # Warmup to ensure DLLs and mapping overhead are cleared
    _ = parse(query).get_referenced_tables()

    # 1. SYNTACTIC BINDING (Fast)
    iterations = 10
    start_time = time.perf_counter()
    for _ in range(iterations):
        result = parse(query)
        _ = result.get_referenced_tables()
    syntactic_duration = ((time.perf_counter() - start_time) / iterations) * 1000

    print(f"--- Fast Syntactic Path (Average of {iterations}) ---")
    print(f"Duration: {syntactic_duration:.2f} ms")
    print("Logic: Walks AST searching for NameReferences in source positions.\n")

    # 2. SEMANTIC BINDING (Rigorous)
    schema = {
        "DeviceProcessEvents": ["TimeGenerated", "DeviceName", "FileName", "ProcessCommandLine"]
    }
    
    start_time = time.perf_counter()
    for _ in range(iterations):
        # Semantic requires re-analysis with state
        _ = result.get_referenced_tables(schema=schema)
    semantic_duration = ((time.perf_counter() - start_time) / iterations) * 1000

    print(f"--- Semantic Binding Path (Average of {iterations}) ---")
    print(f"Duration: {semantic_duration:.2f} ms")
    print("Logic: Creates .NET GlobalState, performs symbol binding, and extracts TableSymbols.\n")

    # PERFORMANCE SUMMARY
    print("=== Performance Metrics ===")
    ratio = semantic_duration / syntactic_duration if syntactic_duration > 0 else 0
    print(f"Syntactic is ~{ratio:.1f}x faster than Semantic.")
    print("Use Case Tip:")
    print("  - Use Syntactic for: Quick linting, syntax highlighting, basic table lists.")
    print("  - Use Semantic for: Symbol validation, column lineage, resolving ambiguous names.")

if __name__ == "__main__":
    run_comparison()
