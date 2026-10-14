"""Explore the saved three-source demo offline: uv run python examples/explore.py."""

import json

from ssdu.store import Store

store = Store("data")
demo = json.loads((store.root / "demo.json").read_text())
for product_id in demo["product_ids"]:
    manifest = store.verify(product_id)
    frame = store.read_intake(product_id)
    print(f"\n{manifest['provider']}: {manifest['title']} ({len(frame)} rows)")
    if manifest["provider"] == "nomis":
        print(
            frame[["DATE", "GEOGRAPHY_CODE", "OBS_VALUE_NUMERIC", "OBS_STATUS"]].to_string(
                index=False
            )
        )
    elif manifest["provider"] == "statswales":
        print(
            frame[["Family type__code", "Data description__code", "Data values", "Notes"]]
            .head(8)
            .to_string(index=False)
        )
    else:
        print("LSOA counts by England-wide deprivation decile (1 = most deprived):")
        print(frame.groupby("imd_decile").size().rename("lsoa_count").to_string())
