import re

def parse_price_to_float(raw_price: str) -> dict:
    """
    Parses a raw price string like "$46.97", "$40 - $50", "From $42.99"
    into a structured dictionary with price_min and price_max.
    """
    if not raw_price or not isinstance(raw_price, str):
        return {
            "price_min": None,
            "price_max": None,
            "raw": str(raw_price) if raw_price is not None else "",
            "confidence": "missing"
        }
    
    clean_str = raw_price.strip()
    
    # Strip common prefixes/suffixes
    clean_str = re.sub(r'From\s*', '', clean_str, flags=re.IGNORECASE)
    clean_str = re.sub(r'Starting at\s*', '', clean_str, flags=re.IGNORECASE)
    
    # Find all floats/ints that look like currency amounts
    # e.g., $40, 40.00, 1,000.50
    matches = re.findall(r'\$?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)', clean_str)
    
    if not matches:
        return {
            "price_min": None,
            "price_max": None,
            "raw": raw_price,
            "confidence": "missing"
        }
    
    try:
        prices = [float(m.replace(',', '')) for m in matches]
    except ValueError:
        return {
            "price_min": None,
            "price_max": None,
            "raw": raw_price,
            "confidence": "missing"
        }
        
    prices = sorted(prices)
    
    if " - " in clean_str or " to " in clean_str:
        if len(prices) >= 2:
            return {
                "price_min": prices[0],
                "price_max": prices[-1],
                "raw": raw_price,
                "confidence": "range"
            }
            
    if "& above" in clean_str.lower() or "and above" in clean_str.lower():
        if len(prices) >= 1:
            return {
                "price_min": prices[0],
                "price_max": None,
                "raw": raw_price,
                "confidence": "range_open_ended"
            }
            
    # Default: single price (or we take the lowest if multiple found confusingly)
    return {
        "price_min": prices[0],
        "price_max": prices[0],
        "raw": raw_price,
        "confidence": "high"
    }

def product_card_matches_price_filter(parsed_price: dict, filter_min: float | None, filter_max: float | None, mode: str) -> dict:
    """
    Evaluates whether a product card's parsed price satisfies the requested filter criteria.
    Modes:
    - card_price_prefilter: Strict. If price is known and outside range, reject. If missing, keep (needs_pdp_price_check).
    - hybrid: Soft prefilter. Reject only if obviously out of range.
    - pdp_variant_filter: Never reject here. Keep everything.
    
    Returns: {"status": "filtered_in" | "filtered_out", "reason": str}
    """
    if mode == "pdp_variant_filter":
        return {"status": "filtered_in", "reason": "kept by pdp_variant_filter mode"}
        
    p_min = parsed_price.get("price_min")
    p_max = parsed_price.get("price_max")
    
    if p_min is None:
        return {"status": "filtered_in", "reason": "ambiguous or missing price"}
        
    if filter_min is not None:
        if p_max is not None and p_max < filter_min:
            return {"status": "filtered_out", "reason": f"card max price ({p_max}) < filter min ({filter_min})"}
        if p_max is None and p_min < filter_min:
             if mode == "card_price_prefilter":
                 return {"status": "filtered_out", "reason": f"card min price ({p_min}) < filter min ({filter_min})"}
             elif mode == "hybrid":
                 # In hybrid, if p_min is below filter but it's an open range, we might still keep it.
                 if parsed_price.get("confidence") == "range_open_ended":
                     return {"status": "filtered_in", "reason": "open ended range might contain valid price"}
                 # If it's a "high" confidence exact price below threshold
                 if parsed_price.get("confidence") == "high":
                     return {"status": "filtered_out", "reason": f"exact card price ({p_min}) < filter min ({filter_min})"}
                 
    if filter_max is not None:
        if p_min > filter_max:
             return {"status": "filtered_out", "reason": f"card min price ({p_min}) > filter max ({filter_max})"}
             
    return {"status": "filtered_in", "reason": "passed price filter"}
