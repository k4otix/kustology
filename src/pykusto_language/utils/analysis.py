import hashlib
import json
from ..bridge import (
    GlobalState, TableSymbol, ColumnSymbol, DatabaseSymbol, ScalarTypes,
    KustoCode
)

class KustoWalker:
    """Base class for manual AST traversal to ensure DRY across analysis tools."""
    def visit(self, node):
        if node is None:
            return
        
        self.pre_visit(node)
        
        for i in range(node.ChildCount):
            child = node.GetChild(i)
            if child is not None:
                self.visit(child)
        
        self.post_visit(node)

    def pre_visit(self, node):
        pass

    def post_visit(self, node):
        pass

class TableExtractor(KustoWalker):
    def __init__(self, semantic=False):
        self.tables = set()
        self.let_variables = set()
        self.semantic = semantic

    def pre_visit(self, node):
        kind = str(node.Kind)
        
        if kind == "LetStatement":
            name_node = node.GetChild(1)
            if name_node:
                self.let_variables.add(name_node.ToString().strip())

        if self.semantic and hasattr(node, "ReferencedSymbol") and node.ReferencedSymbol:
            sym = node.ReferencedSymbol
            if "TableSymbol" in str(sym.GetType().FullName):
                self.tables.add(sym.Name)
        
        elif kind == "NameReference":
            parent = node.Parent
            if parent is not None:
                parent_kind = str(parent.Kind)
                is_source = False
                if parent_kind in ["ExpressionStatement", "PipeExpression"]:
                    if parent.GetChild(0) == node:
                        is_source = True
                elif "Operator" in parent_kind and parent.GetChild(0) == node:
                    is_source = True
                
                if is_source:
                    name = node.ToString().strip()
                    if name and name not in self.let_variables:
                        self.tables.add(name)

def get_tables_syntactic(kusto_code) -> set[str]:
    extractor = TableExtractor(semantic=False)
    extractor.visit(kusto_code.Syntax)
    return extractor.tables

def create_global_state(schema_dict: dict):
    tables = []
    for table_name, columns in schema_dict.items():
        col_symbols = [ColumnSymbol(c, ScalarTypes.String) for c in columns]
        tables.append(TableSymbol(table_name, col_symbols))
    return GlobalState.Default.WithDatabase(DatabaseSymbol("NetDB", tables))

def get_tables_semantic(kusto_code, schema: dict) -> set[str]:
    state = create_global_state(schema)
    analyzed = KustoCode.ParseAndAnalyze(kusto_code.Text, state)
    extractor = TableExtractor(semantic=True)
    extractor.visit(analyzed.Syntax)
    return extractor.tables

def get_operator_stats(kusto_code) -> dict:
    class OperatorCounter(KustoWalker):
        def __init__(self):
            self.counts = {}
        def pre_visit(self, node):
            kind = str(node.Kind)
            if "Operator" in kind:
                self.counts[kind] = self.counts.get(kind, 0) + 1
    counter = OperatorCounter()
    counter.visit(kusto_code.Syntax)
    return counter.counts

def get_operator_chain(kusto_code) -> list:
    chain = []
    def walk(node):
        if node is None: return
        kind = str(node.Kind)
        if kind == "QueryBlock" or "List" in kind or kind == "SeparatedElement":
            for i in range(node.ChildCount): walk(node.GetChild(i))
        elif kind == "ExpressionStatement":
            walk(node.GetChild(0))
        elif kind == "PipeExpression":
            walk(node.GetChild(0))
            chain.append(node.GetChild(2))
        elif "Operator" in kind or kind == "NameReference":
            chain.append(node)
    walk(kusto_code.Syntax)
    return chain

def node_to_dict(node):
    if node is None: return None
    result = {"kind": str(node.Kind), "text": node.ToString().strip(), "children": []}
    for i in range(node.ChildCount):
        child = node.GetChild(i)
        if child is not None: result["children"].append(node_to_dict(child))
    return result

def get_referenced_columns(kusto_code) -> set[str]:
    extractor = TableExtractor(semantic=False)
    extractor.visit(kusto_code.Syntax)
    class ColumnExtractor(KustoWalker):
        def __init__(self, tables, variables):
            self.columns = set()
            self.tables = tables
            self.variables = variables
        def pre_visit(self, node):
            if str(node.Kind) == "NameReference":
                name = node.ToString().strip()
                if name and name not in self.tables and name not in self.variables:
                    self.columns.add(name)
    col_extractor = ColumnExtractor(extractor.tables, extractor.let_variables)
    col_extractor.visit(kusto_code.Syntax)
    return col_extractor.columns

def get_structural_hash(kusto_code) -> str:
    struct_str = []
    class HashWalker(KustoWalker):
        def pre_visit(self, node):
            kind = str(node.Kind)
            struct_str.append(kind)
            # Skip children for literals to treat them as atomic structural units
            if "LiteralExpression" in kind: return
    HashWalker().visit(kusto_code.Syntax)
    return hashlib.sha256("".join(struct_str).encode()).hexdigest()

def get_time_range(kusto_code) -> list[str]:
    time_filters = []
    class TimeWalker(KustoWalker):
        def pre_visit(self, node):
            text = node.ToString()
            if any(k in text for k in ["ago(", "datetime(", "now()"]):
                if node.ChildCount == 0 or str(node.Kind) == "FunctionCallExpression":
                    time_filters.append(text.strip())
    TimeWalker().visit(kusto_code.Syntax)
    return list(set(time_filters))

def replace_table(kusto_code, old_name: str, new_name: str) -> str:
    replacements = []
    class ReplaceWalker(KustoWalker):
        def pre_visit(self, node):
            if str(node.Kind) == "NameReference" and node.ToString().strip() == old_name:
                parent = node.Parent
                if parent and (str(parent.Kind) in ["ExpressionStatement", "PipeExpression"] or "Operator" in str(parent.Kind)):
                    replacements.append((node.TextStart, node.Width))
    ReplaceWalker().visit(kusto_code.Syntax)
    text = kusto_code.Text
    for start, length in sorted(replacements, reverse=True):
        text = text[:start] + new_name + text[start+length:]
    return text

def mask_literals(kusto_code, mask="'<REDACTED>'") -> str:
    literals = []
    class LiteralMasker(KustoWalker):
        def pre_visit(self, node):
            kind = str(node.Kind)
            if "LiteralExpression" in kind and "String" in kind:
                literals.append((node.TextStart, node.Width))
    LiteralMasker().visit(kusto_code.Syntax)
    text = kusto_code.Text
    for start, length in sorted(literals, reverse=True):
        text = text[:start] + mask + text[start+length:]
    return text
