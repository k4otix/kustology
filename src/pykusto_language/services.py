from .bridge import KustoCode, call_static, _KustoFormatter

def parse(query_text: str):
    """
    Parses a KQL query string and returns a KustoQuery object.
    """
    from .core import KustoQuery
    code = KustoCode.Parse(query_text)
    return KustoQuery(code)

def format(query_text: str) -> str:
    """
    Formats a KQL query string.
    """
    code = KustoCode.Parse(query_text)
    # Correct signature: GetFormattedText(SyntaxNode, FormattingOptions, Int32)
    # Returns FormattedText, which has a .Text property
    formatted_obj = call_static(_KustoFormatter, "GetFormattedText", code.Syntax, None, 0)
    return str(formatted_obj.Text)

def validate(query_text: str) -> list[dict]:
    """
    Validates a KQL query and returns diagnostics.
    """
    code = KustoCode.Parse(query_text)
    diagnostics = code.GetDiagnostics()
    results = []
    for d in diagnostics:
        results.append({
            "start": d.Start,
            "length": d.Length,
            "message": str(d.Message),
            "severity": str(d.Severity),
            "category": str(d.Category),
            "code": str(d.Code)
        })
    return results
