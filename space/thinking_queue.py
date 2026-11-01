"""Bounded native-frame buffer; pending includes in-flight database writes."""
import collections
import datetime
import threading
import time


class NativeFrameLimitError(ValueError):
    pass


class PendingFrames:
    def __init__(self, *, max_frames=512, max_bytes=64 * 1024**2,
                 batch_count=128, batch_bytes=1024**2, batch_wait=.1, clock=time.monotonic):
        if not 1 <= batch_count <= 128 or min(max_frames, max_bytes, batch_bytes) < 1 or not 0 <= batch_wait <= .2:
            raise ValueError('Invalid native buffer limits')
        self.max_frames, self.max_bytes = max_frames, max_bytes
        self.batch_count, self.batch_bytes, self.batch_wait = batch_count, batch_bytes, batch_wait
        self.clock = clock
        self.condition = threading.Condition()
        self.waiting = collections.deque()
        self.pending = {}
        self.pending_bytes = 0
        self.counter = 0
        self.closed = False
        self.last_native_time = None
        self.last_received_at = None
        self.last_ingest_time = None

    def put(self, message, *, size=0, received_at=None, received_mono=None, native_time=None, stop=None):
        if size > self.max_bytes or size < 0:
            raise NativeFrameLimitError('NATIVE_FRAME_TOO_LARGE')
        native = message.get('type') == 'native'
        mono = self.clock() if received_mono is None else received_mono
        with self.condition:
            while not self.closed and (len(self.waiting) >= self.max_frames or
                    native and (len(self.pending) >= self.max_frames or self.pending_bytes + size > self.max_bytes)):
                if stop is not None and stop.is_set():
                    return False
                self.condition.wait(.05)
            if self.closed or stop is not None and stop.is_set():
                return False
            item = dict(message)
            if native:
                self.counter += 1
                item.update(ticket=self.counter, receivedAt=received_at, size=size)
                self.pending[self.counter] = {'mono': mono, 'size': size, 'receivedAt': received_at}
                self.pending_bytes += size
                self.last_received_at = received_at
                if isinstance(native_time, (int, float)) and not isinstance(native_time, bool):
                    self.last_native_time = max(self.last_native_time or native_time, native_time)
            self.waiting.append(item)
            self.condition.notify_all()
            return True

    def get_batch(self):
        with self.condition:
            while not self.waiting and not self.closed:
                self.condition.wait(.05)
            if not self.waiting:
                return []
            first = self.waiting.popleft()
            self.condition.notify_all()
            if first.get('type') != 'native':
                return [first]
            batch, total = [first], first['size']
            deadline = self.clock() + self.batch_wait
            while len(batch) < self.batch_count:
                if self.waiting:
                    item = self.waiting[0]
                    if item.get('type') != 'native' or total + item['size'] > self.batch_bytes:
                        break
                    batch.append(self.waiting.popleft())
                    total += item['size']
                    self.condition.notify_all()
                elif self.closed or self.clock() >= deadline:
                    break
                else:
                    self.condition.wait(min(.05, max(0, deadline - self.clock())))
            return batch

    def committed(self, batch, *, ingested_at=None):
        """Acknowledge only after the complete raw/state/event transaction commits."""
        with self.condition:
            for item in batch:
                metadata = self.pending.pop(item.get('ticket'), None)
                if metadata:
                    self.pending_bytes -= metadata['size']
            self.last_ingest_time = ingested_at or datetime.datetime.now(datetime.timezone.utc).isoformat()
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            oldest = min(self.pending.values(), key=lambda row: row['mono']) if self.pending else None
            queued = sum(item.get('type') == 'native' for item in self.waiting)
            return {'pendingFrames': len(self.pending), 'queuedFrames': queued,
                    'inFlightFrames': len(self.pending) - queued, 'pendingBytes': self.pending_bytes,
                    'backlogAgeMs': int(max(0, self.clock() - oldest['mono']) * 1000) if oldest else 0,
                    'oldestPendingReceivedAt': oldest['receivedAt'] if oldest else None,
                    'lastNativeTime': self.last_native_time, 'lastNativeReceivedAt': self.last_received_at,
                    'lastIngestTime': self.last_ingest_time,
                    'batchMaxFrames': self.batch_count, 'batchMaxBytes': self.batch_bytes,
                    'batchWaitMs': round(self.batch_wait * 1000), 'bufferMaxFrames': self.max_frames,
                    'bufferMaxBytes': self.max_bytes}

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
