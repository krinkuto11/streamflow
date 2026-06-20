import unittest
import io
import time
import collections
from PIL import Image
from apps.stream.sidecar_loop_detector import SidecarHash, SidecarLoopDetector

class MockPipe(io.BytesIO):
    def read(self, size):
        return super().read(size)

class TestSidecarLoopDetector(unittest.TestCase):
    def create_ppm_frame(self, color=(255, 0, 0), dot_pos=(0, 0)):
        width, height = 32, 32
        # Fill with base color
        pixels = list(color) * (width * height)
        # Add a unique "dot" to ensure hash uniqueness
        dot_idx = (dot_pos[1] * width + dot_pos[0]) * 3
        pixels[dot_idx:dot_idx+3] = [255 - color[0], 255 - color[1], 255 - color[2]]
        
        header = f"P6\n{width} {height}\n255\n".encode()
        return header + bytes(pixels)

    def test_ppm_header_parsing(self):
        frame = self.create_ppm_frame((100, 100, 100))
        pipe = MockPipe(frame)
        detector = SidecarLoopDetector(pipe)
        
        parsed_frame = detector._read_ppm_frame()
        self.assertEqual(parsed_frame, frame)

    def test_loop_detection(self):
        # Create a sequence of 3 unique frames with unique dots
        f1 = self.create_ppm_frame((255, 0, 0), (5, 5)) 
        f2 = self.create_ppm_frame((0, 255, 0), (10, 10))
        f3 = self.create_ppm_frame((0, 0, 255), (15, 15))
        
        # Create a looping stream: F1, F2, F3, ..., (Wait 11s) ..., F1, F2, F3
        pipe = MockPipe()
        detector = SidecarLoopDetector(pipe)
        
        # Helper to add frame with timestamp
        def add_frame(f, t_offset):
            # We bypass detector.run() for deterministic testing
            from PIL import Image
            import io
            import imagehash
            img = Image.open(io.BytesIO(f))
            h = imagehash.phash(img)
            detector.buffer.append((time.monotonic() + t_offset, h))
            detector.last_frame_time = time.monotonic() + t_offset

        # Start of sequence
        add_frame(f1, 0)
        add_frame(f2, 1)
        add_frame(f3, 2)
        
        # Some filler in between (not matching the sequence)
        for i in range(10):
            add_frame(self.create_ppm_frame((i, i, i)), 3 + i)
            
        # Re-occurrence of the sequence after 12 seconds
        add_frame(f1, 15)
        add_frame(f2, 16)
        add_frame(f3, 17)
        
        # Detection should find the loop
        duration = detector.detect_loop()
        self.assertIsNotNone(duration)
        self.assertGreaterEqual(duration, 10.0)

    def test_static_image_rejection(self):
        f1 = self.create_ppm_frame((255, 0, 0))
        pipe = MockPipe()
        detector = SidecarLoopDetector(pipe)
        
        def add_frame(f, t_offset):
            from PIL import Image
            import io
            import imagehash
            img = Image.open(io.BytesIO(f))
            h = imagehash.phash(img)
            detector.buffer.append((time.monotonic() + t_offset, h))
            detector.last_frame_time = time.monotonic() + t_offset

        # Add 10 identical frames
        for i in range(10):
            add_frame(f1, i)
            
        # Should NOT detect a loop (it's a static image)
        duration = detector.detect_loop()
        self.assertIsNone(duration)

    def test_compression_noisy_sequence_detected_by_stable_hash_consensus(self):
        pipe = MockPipe()
        detector = SidecarLoopDetector(pipe)
        start = time.monotonic()

        def make_hash(value):
            return SidecarHash(value)

        def signature(seed, noise=0):
            base_phash = [0x1111222233334444, 0x5555666677778888, 0x9999AAAABBBBCCCC]
            return {
                "phash": make_hash(base_phash[seed] ^ noise),
                "ahash": make_hash([0x3333333333333333, 0x7373733333333333, 0x3333333333333337][seed]),
                "dhash": make_hash([0xE7E7E7E7E7E7E7E7, 0xE6E7E7E7E7E7E7E7, 0xE7E7E7E7E7E7E7EF][seed]),
                "whash": make_hash([0x3333333333333333, 0x3333333333333331, 0x3333333333333333][seed]),
            }

        for offset, seed in enumerate([0, 1, 2]):
            detector.buffer.append((start + offset, signature(seed)))
        for index in range(10):
            detector.buffer.append((start + 3 + index, signature(index % 3, noise=0x00FF00FF00FF00FF)))
        for offset, seed in enumerate([0, 1, 2], start=15):
            detector.buffer.append((start + offset, signature(seed, noise=0x0F0F0F0F0F0F0F0F)))
        detector.last_frame_time = start + 17

        duration = detector.detect_loop(hamming_tolerance=3)
        self.assertIsNotNone(duration)
        self.assertGreaterEqual(duration, 10.0)

    def test_average_hash_alone_does_not_mark_noisy_sequence_as_loop(self):
        pipe = MockPipe()
        detector = SidecarLoopDetector(pipe)
        start = time.monotonic()

        def signature(seed, noise=0):
            return {
                "phash": SidecarHash([0x1111222233334444, 0x5555666677778888, 0x9999AAAABBBBCCCC][seed] ^ noise),
                "ahash": SidecarHash(0x3333333333333333),
                "dhash": SidecarHash([0x1111111111111111, 0x2222222222222222, 0x4444444444444444][seed] ^ noise),
                "whash": SidecarHash([0x0101010101010101, 0x1010101010101010, 0x8080808080808080][seed] ^ noise),
            }

        for offset, seed in enumerate([0, 1, 2]):
            detector.buffer.append((start + offset, signature(seed)))
        for index in range(10):
            detector.buffer.append((start + 3 + index, signature(index % 3, noise=0x0000FFFF0000FFFF)))
        for offset, seed in enumerate([0, 1, 2], start=15):
            detector.buffer.append((start + offset, signature(seed, noise=0xFFFF0000FFFF0000)))
        detector.last_frame_time = start + 17

        duration = detector.detect_loop(hamming_tolerance=3)
        self.assertIsNone(duration)

if __name__ == '__main__':
    unittest.main()
