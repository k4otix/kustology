import os
import sys
import pythonnet

def _initialize_bridge():
    """Initializes the CLR bridge to the bundled Kusto.Language DLLs."""
    try:
        if not pythonnet.get_runtime_info():
            pythonnet.load("coreclr")
    except Exception as e:
        raise RuntimeError(f"Failed to initialize .NET runtime. Ensure .NET 8.0+ is installed. Error: {e}")

    import clr

    # Locate the bin directory relative to this file
    base_dir = os.path.dirname(os.path.abspath(__file__))
    bin_dir = os.path.join(base_dir, "bin")

    # Add to path and reference the primary assembly
    if bin_dir not in sys.path:
        sys.path.append(bin_dir)
    
    try:
        clr.AddReference("Kusto.Language")
    except Exception as e:
        raise ImportError(
            f"Could not load Kusto.Language assembly. Ensure Kusto.Language.dll is in {bin_dir}. Error: {e}"
        )

# Initialize on import
_initialize_bridge()

import clr
import System
from Kusto.Language import KustoCode, GlobalState
from Kusto.Language.Syntax import SyntaxNode, SyntaxVisitor
from Kusto.Language.Symbols import TableSymbol, ColumnSymbol, DatabaseSymbol, ScalarTypes

# Stubborn types that fail direct import in some environments
def get_net_type(fullname):
    t = System.Type.GetType(f"{fullname}, Kusto.Language")
    if t is None:
        # Fallback search through assemblies
        for assembly in System.AppDomain.CurrentDomain.GetAssemblies():
            if assembly.GetName().Name == "Kusto.Language":
                t = assembly.GetType(fullname)
                if t: break
    return t

_KustoFormatter = get_net_type("Kusto.Language.Editor.KustoFormatter")
_FormattingOptions = get_net_type("Kusto.Language.Editor.FormattingOptions")

def call_static(type_obj, method_name, *args):
    """Helper to call static methods via reflection when pythonnet mapping fails."""
    # Convert Python types to .NET types explicitly
    net_args = []
    for arg in args:
        if isinstance(arg, bool):
            net_args.append(System.Boolean(arg))
        elif isinstance(arg, int):
            # Try to be smart about 32 vs 64 bit, but default to 32 for now as most KQL APIs use it
            net_args.append(System.Int32(arg))
        elif arg is None:
            net_args.append(None)
        else:
            net_args.append(arg)
            
    net_args_array = System.Array[System.Object](net_args)
    
    # Try to find the method that matches the argument count
    methods = type_obj.GetMethods()
    for m in methods:
        if m.Name == method_name and m.GetParameters().Length == len(args):
            return m.Invoke(None, net_args_array)
            
    raise AttributeError(f"Method {method_name} with {len(args)} parameters not found on {type_obj.FullName}")

__all__ = [
    "KustoCode", 
    "GlobalState", 
    "SyntaxNode", 
    "SyntaxVisitor", 
    "TableSymbol", 
    "ColumnSymbol", 
    "DatabaseSymbol", 
    "ScalarTypes",
    "call_static",
    "_KustoFormatter",
    "_FormattingOptions"
]
