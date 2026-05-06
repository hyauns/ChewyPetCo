import json
from chewy_next_json_extractor import normalize_chewy_product, empty_feeding_instructions

def test_cases():
    print("Running feeding_instructions none-safe tests...\n")
    
    # 1. None input
    p1 = {"feeding_instructions": None, "title": "Test Product"}
    r1 = normalize_chewy_product(p1)
    fi1 = r1["content_sections"]["feeding_instructions"]
    assert fi1 == empty_feeding_instructions()
    assert r1["storefront_display"]["accordion_sections"][4]["enabled"] is False
    assert "Feeding instructions missing or empty." in r1["warnings"]
    print("PASS: None input")
    
    # 2. Empty string input
    p2 = {"feeding_instructions": "", "title": "Test Product"}
    r2 = normalize_chewy_product(p2)
    fi2 = r2["content_sections"]["feeding_instructions"]
    assert fi2 == empty_feeding_instructions()
    assert r2["storefront_display"]["accordion_sections"][4]["enabled"] is False
    assert "Feeding instructions missing or empty." in r2["warnings"]
    print("PASS: Empty string input")
    
    # 3. Markdown table input
    table_str = "|Weight|Amount|\n|---|---|\n|10 lbs|1 cup|"
    p3 = {"feeding_instructions": table_str, "title": "Test Product"}
    r3 = normalize_chewy_product(p3)
    fi3 = r3["content_sections"]["feeding_instructions"]
    assert len(fi3["tables"]) == 1
    assert fi3["tables"][0]["rows"][0]["Weight"] == "10 lbs"
    assert r3["storefront_display"]["accordion_sections"][4]["enabled"] is True
    print("PASS: Markdown table input")
    
    # 4. Already-normalized dict input
    dict_in = {"summary": "Feed well", "tables": [{"title": "t", "columns": [], "rows": []}]}
    p4 = {"feeding_instructions": dict_in, "title": "Test Product"}
    r4 = normalize_chewy_product(p4)
    fi4 = r4["content_sections"]["feeding_instructions"]
    assert len(fi4["tables"]) == 1
    assert fi4["summary"] == "Feed well"
    assert "transition_instructions" in fi4
    assert r4["storefront_display"]["accordion_sections"][4]["enabled"] is True
    print("PASS: Already-normalized dict input")
    
    # 5. Malformed input (integer)
    p5 = {"feeding_instructions": 12345, "title": "Test Product"}
    r5 = normalize_chewy_product(p5)
    fi5 = r5["content_sections"]["feeding_instructions"]
    assert fi5 == empty_feeding_instructions()
    assert r5["storefront_display"]["accordion_sections"][4]["enabled"] is False
    assert "Feeding instructions could not be parsed." in r5["warnings"]
    print("PASS: Malformed input (integer)")

    # 6. Metafields plan check
    assert r1["metafields_plan"]["custom.feeding_instructions_json"] == empty_feeding_instructions()
    print("PASS: Metafields plan protected")

if __name__ == "__main__":
    test_cases()
    print("\nAll regression tests passed successfully!")
