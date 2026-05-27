import os
import json
import sys
import time
from petsmart_enrich_variants import enrich_products

def main():
    # Path configurations
    base_dir = "PetSmart"
    if not os.path.exists(base_dir):
        print(f"Error: {base_dir} directory not found.")
        return 1

    # Find all product JSON files
    files = [f for f in os.listdir(base_dir) if f.endswith(".json")]
    target_files = []
    for f in files:
        # Skip utility, metadata, and already-enriched file patterns
        if f in ("raw_algolia_hits.json", "summary.json"):
            continue
        if f.endswith("_enriched.json") or "_test" in f:
            continue
        # If it ends with _products.json or is products.json, it's a target!
        if f.endswith("_products.json") or f == "products.json":
            target_files.append(f)

    target_files.sort()
    print("=" * 60)
    print(f"PetSmart Parallel Batch Enrichment")
    print(f"Found {len(target_files)} categories to enrich:")
    for f in target_files:
        print(f"  - {f}")
    print("=" * 60)

    workers = 5
    delay = 0.2

    overall_start = time.time()
    
    for idx, filename in enumerate(target_files, 1):
        raw_path = os.path.join(base_dir, filename)
        
        # Determine output and input paths (resuming support)
        if filename == "products.json":
            enriched_filename = "products_enriched.json"
        else:
            enriched_filename = filename.replace("_products.json", "_products_enriched.json")
        
        enriched_path = os.path.join(base_dir, enriched_filename)
        
        # If enriched file already exists, load it to resume
        if os.path.exists(enriched_path):
            input_path = enriched_path
            print(f"\n[{idx}/{len(target_files)}] Resuming enrichment of {filename} -> {enriched_filename}")
        else:
            input_path = raw_path
            print(f"\n[{idx}/{len(target_files)}] Enriching {filename} -> {enriched_filename}")

        # Load data
        try:
            with open(input_path, "r", encoding="utf-8") as f:
                products = json.load(f)
        except Exception as e:
            print(f"  Error loading {input_path}: {e}")
            continue

        print(f"  Loaded {len(products)} products from {input_path}")
        
        # Run enrichment
        start_time = time.time()
        products, enriched, skipped = enrich_products(
            products, workers=workers, delay=delay
        )
        elapsed = time.time() - start_time

        # Save to enriched_path
        try:
            with open(enriched_path, "w", encoding="utf-8") as f:
                json.dump(products, f, indent=2, ensure_ascii=False)
            print(f"  Done in {elapsed:.1f}s | Enriched: {enriched} | Skipped: {skipped} | Saved: {enriched_path}")
        except Exception as e:
            print(f"  Error saving to {enriched_path}: {e}")

    overall_elapsed = time.time() - overall_start
    print("\n" + "=" * 60)
    print(f"All category enrichments completed in {overall_elapsed/60:.1f} minutes!")
    print("=" * 60)
    return 0

if __name__ == "__main__":
    sys.exit(main())
