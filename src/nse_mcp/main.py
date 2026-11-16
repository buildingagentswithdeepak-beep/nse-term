import json
from nse_mcp import NSEMCPClient, BHAVCOPY_URL

client = NSEMCPClient(BHAVCOPY_URL)
rows = client.paged_history("INFY", total_months=12)
selected = [
    {
        "close": row["close"],
        "volume": row["volume"],
        "date": row["date"],
    }
    for chunk in rows
    for row in chunk["data"]
]

print(json.dumps(selected, indent=2))














