import os
import sys

# Ensure the local src directory is in the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from pykusto_language import parse, format, validate
from pykusto_language.utils.analysis import get_operator_stats

SENTINEL_QUERY = """
let dt_lookBack = 1h;
let ioc_lookBack = 14d;
let list_tlds = ThreatIntelligenceIndicator
  | where isnotempty(DomainName)
  | where TimeGenerated > ago(ioc_lookBack)
  | summarize LatestIndicatorTime = arg_max(TimeGenerated, *) by IndicatorId
  | where Active == true and ExpirationDateTime > now()
  | extend parts = split(DomainName, '.')
  | extend tld = parts[(array_length(parts)-1)]
  | summarize count() by tostring(tld)
  | summarize make_list(tld);
let Domain_Indicators = ThreatIntelligenceIndicator
  | where isnotempty(DomainName)
  | where TimeGenerated >= ago(ioc_lookBack)
  | summarize LatestIndicatorTime = arg_max(TimeGenerated, *) by IndicatorId
  | where Active == true and ExpirationDateTime > now()
  | extend TI_DomainEntity = DomainName;
Domain_Indicators
  | join kind=innerunique (
    Syslog
    | where TimeGenerated > ago(dt_lookBack)
    | extend domain = extract("(([a-z0-9]+(-[a-z0-9]+)*\\\\.)+[a-z]{2,})",1, tolower(SyslogMessage))
    | where isnotempty(domain)
    | extend parts = split(domain, '.')
    | extend tld = parts[(array_length(parts)-1)]
    | where tld in~ (list_tlds)
    | extend Syslog_TimeGenerated = TimeGenerated
  ) on $left.TI_DomainEntity==$right.domain
  | where Syslog_TimeGenerated < ExpirationDateTime
  | summarize Syslog_TimeGenerated = arg_max(Syslog_TimeGenerated, *) by IndicatorId, domain
  | project Syslog_TimeGenerated, Description, ActivityGroupNames, IndicatorId, ThreatType, ExpirationDateTime, ConfidenceScore, SyslogMessage, Computer, ProcessName, domain, HostIP, Url, Type, TI_DomainEntity
  | extend HostName = tostring(split(Computer, '.', 0)[0])
  | extend DnsDomain = tostring(strcat_array(array_slice(split(Computer, '.'), 1, -1), '.'))[1]
  | extend timestamp = Syslog_TimeGenerated
"""

def audit_query(query_text: str):
    print("🚀 Initializing KQL Query Auditor...")
    print("-" * 60)
    
    # 1. Parsing & Fingerprinting
    result = parse(query_text)
    diagnostics = validate(query_text)
    
    if diagnostics:
        print(f"⚠️ Warning: Found {len(diagnostics)} syntactic issues.")
    else:
        print("✅ Query is syntactically valid.")

    print(f"🆔 Structural Fingerprint: {result.get_structural_hash()[:16]}... (SHA256)")

    # 2. Dependency & Column Mapping
    print("\n📂 Data Inventory:")
    tables = result.get_referenced_tables()
    print(f"  Primary Tables ({len(tables)}): {', '.join(sorted(list(tables)))}")
    
    columns = result.get_referenced_columns()
    print(f"  Referenced Columns ({len(columns)}): {', '.join(sorted(list(columns))[:8])} ...")

    # 3. Temporal Context
    time_windows = result.get_time_range()
    print(f"\n⏰ Temporal Context: {', '.join(time_windows) if time_windows else 'None detected'}")

    # 4. Operator Chain (Pipeline Flow)
    chain = result.get_operator_chain()
    print(f"\n⛓️  Pipeline Flow ({len(chain)} steps):")
    # Identify the main operators in order
    flow = []
    for node in chain:
        kind = str(node.Kind).replace("Operator", "")
        if kind == "NameReference":
            flow.append(f"[{node.ToString().strip()}]")
        else:
            flow.append(kind)
    print(f"  {' ➔ '.join(flow)}")

    # 5. Deep Operator Analytics
    stats = get_operator_stats(result._code)
    print(f"\n📊 Detailed Operator Stats:")
    for op, count in sorted(stats.items(), key=lambda x: x[1], reverse=True):
        clean_op = op.replace("Operator", "")
        print(f"  - {clean_op:15}: {count}")

    # 6. Cognitive Complexity Scoring
    # Advanced logic using our new metrics
    let_count = query_text.lower().count("let ")
    summarize_count = stats.get("SummarizeOperator", 0)
    join_count = stats.get("JoinOperator", 0)
    
    complexity = (let_count * 2) + (summarize_count * 3) + (join_count * 5) + (len(chain) // 2)
    
    print(f"\n🧠 Cognitive Complexity Score: {complexity}")
    if complexity > 25:
        print("  Status: 🔴 HIGH (Requires significant reasoning to modify)")
    elif complexity > 10:
        print("  Status: 🟡 MEDIUM (Standard analytical query)")
    else:
        print("  Status: 🟢 LOW (Simple filtering/projection)")

    # 7. Safe-Log Output
    print("\n📝 Safe-Log Preview (Formatted & Masked):")
    print("-" * 60)
    masked_query = result.mask_literals()
    formatted_query = format(masked_query)
    # Print first 10 lines
    for line in formatted_query.splitlines()[:10]:
        print(line)
    print("  ... (truncated)")
    print("-" * 60)

if __name__ == "__main__":
    audit_query(SENTINEL_QUERY)
