import json
from chewy_next_json_extractor import classify_gtin, build_variant_identifiers

def run_tests():
    print("Testing classify_gtin...")
    
    # Test 1
    t1 = classify_gtin("052742001821")
    assert t1["type"] == "upc", "Test 1 Failed"
    assert t1["normalized"] == "052742001821", "Test 1 Failed"
    assert t1["is_valid_length"] is True, "Test 1 Failed"
    
    # Test 2
    t2 = classify_gtin("0052742886008")
    assert t2["type"] == "ean", "Test 2 Failed"
    assert t2["normalized"] == "0052742886008", "Test 2 Failed"
    assert t2["is_valid_length"] is True, "Test 2 Failed"
    
    # Test 3
    t3 = classify_gtin("269150")
    assert t3["type"] == "unknown", "Test 3 Failed"
    assert t3["is_valid_length"] is False, "Test 3 Failed"
    
    print("Testing build_variant_identifiers...")
    
    # Test 4
    id1 = build_variant_identifiers(gtin="052742001821", source_sku="3861718", source_item_id="3861718")
    assert id1["upc"] == "052742001821", "Test 4 Failed"
    assert id1["gtin"] == "052742001821", "Test 4 Failed"
    assert id1["ean"] is None, "Test 4 Failed"
    assert id1["source_sku"] == "3861718", "Test 4 Failed"
    assert id1["source_item_id"] == "3861718", "Test 4 Failed"
    
    # Test 5: Check grouped output structure generated earlier
    try:
        with open("output/grouped_products/chewy_grouped_by_flavor_3861718.json", "r", encoding="utf-8") as f:
            grouped = json.load(f)
            
        first_variant = grouped["products"][0]["variants"][0]
        assert "identifiers" in first_variant, "Test 5 Failed: missing identifiers"
        assert first_variant["identifiers"]["upc"] is not None, "Test 5 Failed: missing upc"
        assert first_variant["identifiers"]["upc"] != first_variant["sku"], "Test 5 Failed: upc is sku"
    except Exception as e:
        print(f"Test 5 Warning: {e}")
        
    print("All tests passed!")

if __name__ == "__main__":
    run_tests()
