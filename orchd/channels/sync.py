"""Window calculations shared by adapter scheduling and local sync jobs."""
from datetime import timedelta

def fixed_window(checkpoint, activation, now, overlap_seconds):
    if overlap_seconds < 0 or now < activation:
        raise ValueError('invalid sync window')
    return max(activation, checkpoint - timedelta(seconds=overlap_seconds)) if checkpoint else activation, now
