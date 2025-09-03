#!/usr/bin/env python3
"""Debug Redis stream trimming behavior."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mojo.helpers.redis import get_adapter

def test_redis_trimming():
    """Test Redis stream trimming directly."""
    redis = get_adapter()
    stream_key = "debug_trim_test"

    # Clean up first
    redis.delete(stream_key)

    print("Testing Redis stream trimming...")

    # Add entries one by one with maxlen=5
    for i in range(10):
        result = redis.xadd(stream_key, {
            'index': str(i),
            'data': f'entry_{i}'
        }, maxlen=5)

        # Check length after each add
        info = redis.xinfo_stream(stream_key)
        print(f"After entry {i}: stream length = {info['length']}")

    print("\nFinal stream info:")
    final_info = redis.xinfo_stream(stream_key)
    for key, value in final_info.items():
        print(f"  {key}: {value}")

    # Clean up
    redis.delete(stream_key)
    print(f"\nExpected: ~5 entries (with some variance)")
    print(f"Actual: {final_info['length']} entries")

if __name__ == "__main__":
    test_redis_trimming()
