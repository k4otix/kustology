from . import KustoCode

def parse_query(query_text: str):
    """
    Parses a KQL query string and returns the KustoCode object model.
    """
    return KustoCode.Parse(query_text)
