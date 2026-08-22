#!/usr/bin/env python3
"""
Expand instrument bounds in all map files.

Currently most instruments are placed at a single point (minX=maxX, minY=maxY).
This script expands each instrument's bounds by EXPAND_STEPS in X and Y,
so players can interact with them from a wider area.

Bounds format in XML: bounds="minX maxX minY maxY minZ maxZ"
"""

import os
import re
import sys

MAPS_DIR = os.path.join(os.path.dirname(__file__), "..", "maps")
EXPAND_STEPS = 2  # Expand by 2 steps in each direction

def detect_line_ending(content: str) -> str:
    """Detect line ending style from content."""
    if "\r\n" in content:
        return "\r\n"
    elif "\n" in content:
        return "\n"
    return "\r\n"  # Default to CRLF for Windows

def expand_bounds(bounds_str: str, expand: int = EXPAND_STEPS) -> str:
    """Expand bounds by 'expand' steps in X and Y directions."""
    parts = bounds_str.split()
    if len(parts) != 6:
        return bounds_str  # Invalid format, skip
    
    try:
        min_x, max_x, min_y, max_y, min_z, max_z = [int(p) for p in parts]
    except ValueError:
        return bounds_str  # Not integers, skip
    
    # Expand X and Y
    min_x -= expand
    max_x += expand
    min_y -= expand
    max_y += expand
    
    return f"{min_x} {max_x} {min_y} {max_y} {min_z} {max_z}"

def process_map_file(filepath: str) -> int:
    """Process a single map file. Returns number of instruments expanded."""
    with open(filepath, "rb") as f:
        raw = f.read()
    
    # Detect line ending
    if b"\r\n" in raw:
        line_ending = "\r\n"
    else:
        line_ending = "\n"
    
    content = raw.decode("utf-8")
    
    # Pattern to match instrument elements with bounds
    # <instrument bounds="61 61 10 10 0 0" instrument="drumset" id="xxx"/>
    pattern = r'(<instrument\s+bounds=")([^"]+)(")'
    
    count = 0
    def replace_match(match):
        nonlocal count
        prefix = match.group(1)
        bounds = match.group(2)
        suffix = match.group(3)
        
        # Check if it's a point (minX == maxX or minY == maxY)
        parts = bounds.split()
        if len(parts) == 6:
            try:
                min_x, max_x, min_y, max_y = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
                if min_x == max_x or min_y == max_y:
                    # It's a point or thin rectangle - expand it
                    new_bounds = expand_bounds(bounds)
                    count += 1
                    return f"{prefix}{new_bounds}{suffix}"
            except ValueError:
                pass
        return match.group(0)  # No change
    
    new_content = re.sub(pattern, replace_match, content)
    
    if new_content != content:
        # Preserve original line endings
        new_raw = new_content.encode("utf-8").replace(b"\n", line_ending.encode())
        with open(filepath, "wb") as f:
            f.write(new_raw)
    
    return count

def main():
    if not os.path.exists(MAPS_DIR):
        print(f"Error: Maps directory not found: {MAPS_DIR}")
        sys.exit(1)
    
    total_expanded = 0
    files_processed = 0
    
    for filename in sorted(os.listdir(MAPS_DIR)):
        if not filename.endswith(".map"):
            continue
        
        filepath = os.path.join(MAPS_DIR, filename)
        count = process_map_file(filepath)
        
        if count > 0:
            print(f"  {filename}: expanded {count} instrument(s)")
            total_expanded += count
        
        files_processed += 1
    
    print(f"\nDone! Processed {files_processed} map files, expanded {total_expanded} instruments total.")
    print(f"Instruments now have {EXPAND_STEPS}-step wider bounds in X and Y for easier interaction.")

if __name__ == "__main__":
    main()
