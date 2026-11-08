"""Cooperative runner for one adapter; polling and jobs share its source lease."""
import random

class AdapterRunner:
    def __init__(self, adapter, *, interval=60, max_backoff=900, jitter=None):
        if interval <= 0 or max_backoff < interval:
            raise ValueError('invalid runner intervals')
        self.adapter = adapter
        self.interval = interval
        self.max_backoff = max_backoff
        self.jitter = jitter or random.random

    def run(self, stop_event):
        delay = self.interval
        while not stop_event.is_set():
            retry_after = 0
            try:
                result = self.adapter.run_pending()
                if result['status'] == 'idle':
                    self.adapter.poll()
                delay = self.interval
            except Exception as error:
                delay = min(self.max_backoff, delay * 2)
                value = getattr(error, 'retry_after', None)
                if value is None:
                    value = (getattr(error, 'resp', None) or {}).get('retry-after', 0)
                try:
                    retry_after = max(0, float(value))
                except (TypeError, ValueError):
                    retry_after = 0
            stop_event.wait(max(retry_after, delay * (0.9 + 0.2 * self.jitter())))
