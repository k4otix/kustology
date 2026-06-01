"""Regenerate the Sentinel KQL corpus under ``tests/fixtures/complex_queries/``.

The corpus is a set of real-world detection queries extracted from
published Azure-Sentinel analytic rules. ``tests/ir/test_complex_harness.py``
parametrizes over each ``.kql`` file and asserts the IR builder produces
no ``UnknownExpr`` or unspecialized ``Operator`` nodes — making the corpus
the practical coverage signal for the builder.

The ``.kql`` files are checked in, so the harness runs offline. This script
regenerates them when needed (new queries, Sentinel-side edits, etc.):

    python scripts/extract_complex_corpus.py \
        --sentinel-repo /path/to/Azure-Sentinel

Each entry in ``RELATIVE_PATHS`` is a path inside the Azure-Sentinel
checkout to an analytic-rule YAML. The script reads each YAML's ``query:``
field and writes it to ``tests/fixtures/complex_queries/<slug>.kql``. Add or
remove entries to evolve the corpus.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "tests" / "fixtures" / "complex_queries"

# Repo-relative paths inside an Azure-Sentinel checkout. Keep this list
# alphabetically-loose-but-stable; reorderings show up as diff noise.
RELATIVE_PATHS: list[str] = [
    "Solutions/Azure Firewall/Analytic Rules/Azure Firewall - Port Scan.yaml",
    "Solutions/Azure SQL Database solution for sentinel/Analytic Rules/Detection-ErrorsSyntaxStatefulAnomalyOnDatabase.yaml",
    "Solutions/Azure Web Application Firewall (WAF)/Analytic Rules/AFD-Premium-WAF-XSSDetection.yaml",
    "Solutions/FalconFriday/Analytic Rules/CertifiedPreOwned-TGTs-requested.yaml",
    "Solutions/FalconFriday/Analytic Rules/ExpiredAccessCredentials.yaml",
    "Solutions/Google Cloud Platform Audit Logs/Analytic Rules/GCPOpenFirewallRuleCreated.yaml",
    "Solutions/Lumen Defender Threat Feed/Analytic Rules/Lumen_IPEntity_DeviceEvents.yaml",
    "Solutions/Microsoft Business Applications/Analytic Rules/Dataverse - Terminated employee exfiltration over email.yaml",
    "Solutions/Microsoft Defender XDR/Analytic Rules/AVTarrask.yaml",
    "Solutions/Microsoft Entra ID/Analytic Rules/Brute Force Attack against GitHub Account.yaml",
    "Solutions/Microsoft Entra ID/Analytic Rules/Cross-tenantAccessSettingsOrganizationOutboundCollaborationSettingsChanged.yaml",
    "Solutions/Microsoft Entra ID/Analytic Rules/MailPermissionsAddedToApplication.yaml",
    "Solutions/Microsoft Entra ID/Analytic Rules/NRT_AuthenticationMethodsChangedforVIPUsers.yaml",
    "Solutions/Microsoft Entra ID/Analytic Rules/PrivlegedRoleAssignedOutsidePIM.yaml",
    "Solutions/Microsoft Entra ID/Analytic Rules/SuspiciousOAuthApp_OfflineAccess.yaml",
    "Solutions/Network Session Essentials/Analytic Rules/AnomalyFoundInNetworkSessionTraffic.yaml",
    "Solutions/SecurityThreatEssentialSolution/Analytic Rules/Threat_Essentials_MultipleAdmin_membership_removals_from_NewAdmin.yaml",
    "Solutions/Threat Intelligence (NEW)/Analytic Rules/EmailEntity_AzureActivity.yaml",
    "Solutions/Threat Intelligence (NEW)/Analytic Rules/FileHashEntity_SecurityEvent.yaml",
    "Solutions/Threat Intelligence (NEW)/Analytic Rules/IPEntity_AzureFirewall.yaml",
    "Solutions/Threat Intelligence (NEW)/Analytic Rules/IPEntity_SigninLogs_Updated.yaml",
    "Solutions/Threat Intelligence (NEW)/Analytic Rules/URLEntity_SecurityAlerts.yaml",
    "Solutions/Windows Security Events/Analytic Rules/ADFSRemoteHTTPNetworkConnection.yaml",
    "Solutions/Windows Security Events/Analytic Rules/NRT_execute_base64_decodedpayload.yaml",
    "Solutions/Zinc Open Source/Analytic Rules/ZincOctober2022_AVHits_IOC.yaml",
]


def _slugify(basename: str) -> str:
    """Filename-safe slug. Sentinel YAML names often contain spaces and
    parentheses; keep alphanumerics, collapse the rest to underscores."""
    stem = basename[: -len(".yaml")] if basename.endswith(".yaml") else basename
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", stem).strip("_")
    return slug or "query"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sentinel-repo",
        required=True,
        help="Path to a local Azure-Sentinel checkout (the directory containing 'Solutions/').",
    )
    args = parser.parse_args()

    sentinel_root = Path(args.sentinel_repo).resolve()
    if not (sentinel_root / "Solutions").is_dir():
        print(
            f"error: {sentinel_root} doesn't look like an Azure-Sentinel checkout (no Solutions/)",
            file=sys.stderr,
        )
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    written = 0
    missing: list[str] = []
    skipped_no_query: list[str] = []
    seen_slugs: dict[str, int] = {}

    for rel in RELATIVE_PATHS:
        target = sentinel_root / rel
        if not target.is_file():
            missing.append(rel)
            continue

        try:
            with open(target, "r") as f:
                doc = yaml.safe_load(f)
        except Exception as e:
            print(f"warn: could not parse {target}: {e}", file=sys.stderr)
            continue

        query = (doc or {}).get("query")
        if not isinstance(query, str) or not query.strip():
            skipped_no_query.append(rel)
            continue

        slug = _slugify(target.name)
        if slug in seen_slugs:
            seen_slugs[slug] += 1
            slug = f"{slug}__{seen_slugs[slug]}"
        else:
            seen_slugs[slug] = 1

        out_path = OUT_DIR / f"{slug}.kql"
        out_path.write_text(query.rstrip() + "\n")
        written += 1

    print(f"wrote {written} queries to {OUT_DIR.relative_to(REPO_ROOT)}")
    if missing:
        print(f"missing source files ({len(missing)}):", file=sys.stderr)
        for m in missing:
            print(f"  {m}", file=sys.stderr)
    if skipped_no_query:
        print(f"skipped (no `query:` field): {len(skipped_no_query)}", file=sys.stderr)
        for s in skipped_no_query:
            print(f"  {s}", file=sys.stderr)

    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
