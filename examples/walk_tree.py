import os
import sys

# Ensure the local src directory is in the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from pykusto_language import parse


def walk_node(node, depth=0):
    # 1. BASE CASE: Safety check for NoneType nodes from .NET
    if node is None:
        return

    indent = "  " * depth
    try:
        kind = str(node.Kind)
    except AttributeError:
        # Catch cases where node might not be a SyntaxNode
        return

    # 2. NOISE REDUCTION: Skip structural wrappers and terminal tokens
    # We recursively walk their children but stay at the same visual depth
    skip_types = ["Token", "List", "SeparatedElement", "TokenName", "SyntaxList"]
    if any(t in kind for t in skip_types):
        for i in range(node.ChildCount):
            child = node.GetChild(i)
            if child is not None:
                walk_node(child, depth)
        return

    # 3. LOGICAL MAPPING: Focus on high-level operator semantics
    match kind:
        case "QueryBlock":
            print(f"{indent}📦 Root: QueryBlock")

        case "ExpressionStatement":
            print(f"{indent}📜 Statement: Expression")

        case "PipeExpression":
            print(f"{indent}└── 🔗 Pipe (|)")

        case "NameReference":
            print(f"{indent}    └── 📂 Source/Ref: {node}")

        case "FilterOperator":
            # Explicitly call .ToString() to bypass pythonnet's implicit str() limits
            # .Condition is the semantic property; .GetChild(1) is the raw syntax slot
            node_to_print = node.Condition if hasattr(node, "Condition") else node.GetChild(1)
            condition_text = node_to_print.ToString().strip() if node_to_print else ""
            print(f"{indent}    └── 🛠️  Filter: {condition_text}")

        case "ProjectOperator":
            # .Columns is the specific property for project
            node_to_print = node.Columns if hasattr(node, "Columns") else node.GetChild(1)
            columns_text = node_to_print.ToString().strip() if node_to_print else ""
            print(f"{indent}    └── 📋 Project: {columns_text}")

        case _:
            # If you want to see other nodes, uncomment for debugging
            # print(f"{indent}. ({kind})")
            pass

    # 4. RECURSE: Visit children of logical nodes
    for i in range(node.ChildCount):
        child = node.GetChild(i)
        if child is not None:
            walk_node(child, depth + 1)


if __name__ == "__main__":
    query = "SecurityEvent | where EventID == 4624 | project TimeGenerated, Account"
    print(f"🔍 Analyzing: {query}\n")

    result = parse(query)
    walk_node(result.syntax)
