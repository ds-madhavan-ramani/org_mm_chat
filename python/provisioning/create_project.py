"""
provisioning/create_project.py — thin CLI wrapper around the CREATE_PROJECT
stored procedure (sql/00_setup_catalog.sql). Used by the setup notebook and
by the Streamlit "New Project" page.

    python provisioning/create_project.py \\
        --code CONTRACT_ANALYSIS \\
        --name "Contract Analysis LLM Wiki" \\
        --description "TPA contract Q&A" \\
        --sharepoint-site "https://metrotrains.sharepoint.com/sites/legal-contracts" \\
        --sharepoint-folder "https://metrotrains.sharepoint.com/:f:/s/legal-contracts/..."
"""

import argparse
import sys

sys.path.insert(0, "..")
from snowflake_session import get_session  # noqa: E402


def create_project(code: str, name: str, description: str = "",
                   sharepoint_site: str = "", sharepoint_folder: str = "",
                   created_by: str = "") -> str:
    session = get_session()
    result = session.sql(
        "CALL CREATE_PROJECT(?, ?, ?, ?, ?, ?)",
        params=[code, name, description, sharepoint_site, sharepoint_folder, created_by],
    ).collect()
    return result[0][0]


def main():
    parser = argparse.ArgumentParser(description="Provision a new LLM Wiki project")
    parser.add_argument("--code", required=True, help="Short code, e.g. CONTRACT_ANALYSIS")
    parser.add_argument("--name", required=True, help="Display name")
    parser.add_argument("--description", default="")
    parser.add_argument("--sharepoint-site", default="")
    parser.add_argument("--sharepoint-folder", default="")
    parser.add_argument("--created-by", default="")
    args = parser.parse_args()

    message = create_project(
        code=args.code, name=args.name, description=args.description,
        sharepoint_site=args.sharepoint_site, sharepoint_folder=args.sharepoint_folder,
        created_by=args.created_by,
    )
    print(message)


if __name__ == "__main__":
    main()
