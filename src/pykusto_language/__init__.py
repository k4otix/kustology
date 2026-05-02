import os
import platform
import sys

import pythonnet


def _initialize_bridge():
    """Initializes the CLR bridge to the bundled Kusto.Language DLLs."""
    # Attempt to load the CoreCLR
    try:
        if not pythonnet.get_runtime_info():
            # Defaults to looking for the runtime in standard system paths
            pythonnet.load("coreclr")
    except Exception as e:
        raise RuntimeError(f"Failed to initialize .NET runtime. Ensure .NET 8.0+ is installed. Error: {e}")

    import clr

    # Locate the bin directory relative to this file
    base_dir = os.path.dirname(os.path.abspath(__file__))
    bin_dir = os.path.join(base_dir, "bin")

    # Add to path and reference the primary assembly
    sys.path.append(bin_dir)
    try:
        clr.AddReference("Kusto.Language")
    except Exception as e:
        raise ImportError(
            f"Could not find Kusto.Language.dll in {bin_dir}. Ensure the DLL is placed in the package bin directory."
        )


# Initialize on import
_initialize_bridge()

# Expose main entry points
from Kusto.Language import KustoCode
from Kusto.Language.Syntax import SyntaxNode

__all__ = ["KustoCode", "SyntaxNode"]
