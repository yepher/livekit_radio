"""Icecast audio stream handler with metadata processing and LiveKit integration.

Handles streaming from Icecast/SHOUTcast servers, processes ICY metadata,
and manages audio playback through LiveKit's audio pipeline."""

import asyncio
import logging
import queue
import threading
import time
from typing import Optional, Callable, Awaitable
import numpy as np
import ffmpeg
import aiohttp
from livekit import rtc
from livekit.agents import JobContext

logger = logging.getLogger("livekit-radio")

SAMPLE_RATE = 48000
NUM_CHANNELS = 2  # Stereo
SAMPLE_WIDTH = 2  # 16-bit
SAMPLES_PER_FRAME = 960  # 20ms frames at 48kHz

class IcecastStreamer:
    """Handles Icecast audio streaming, metadata processing, and LiveKit integration.
    
    Manages network connections, audio decoding, volume control, and ensures smooth
    playback with proper error handling and recovery mechanisms."""

    def __init__(self, source: rtc.AudioSource, ctx: JobContext):
        """Initialize an Icecast stream handler with audio source and job context."""
        self.source = source
        self.ctx = ctx
        self._running = False
        self._task = None
        self._current_stream = None
        self._metadata_callback: Optional[Callable[[dict], Awaitable[None]]] = None
        self._codec = None  # Track detected codec
        self._volume = 1.0  # Full volume by default
        self._volume_lock = threading.Lock()
        self.meta_int = 0  # Initialize in __init__

    async def play(self, url: str):
        """Start streaming audio from the specified Icecast URL.
        
        Args:
            url: Full HTTP URL of the radio stream to play
        """
        if self._running:
            self.stop()

        self._running = True
        self._task = asyncio.create_task(self._stream_audio(url))

    def stop(self):
        """Stop current stream playback and clean up resources.
        
        Sends silence frames to ensure smooth audio transition on stop.
        """
        if not self._running:
            return
            
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

        # Send a few frames of silence to clear the audio pipeline
        try:
            silence = np.zeros(SAMPLES_PER_FRAME * NUM_CHANNELS, dtype=np.int16)
            for _ in range(5):  # Send multiple frames to ensure clean stop
                frame = rtc.AudioFrame(
                    data=silence.tobytes(),
                    sample_rate=SAMPLE_RATE,
                    num_channels=NUM_CHANNELS,
                    samples_per_channel=SAMPLES_PER_FRAME
                )
                asyncio.create_task(self.source.capture_frame(frame))
        except (RuntimeError, ValueError) as e:
            logger.error("Error sending silence frames: %s", e)

    async def _stream_audio(self, url: str):
        process_task = None
        stderr_reader = None
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"Icy-MetaData": "1"}) as resp:
                    # Log all response headers
                    logger.info("Icecast response headers: %s", dict(resp.headers))
                    
                    # Get metadata interval
                    metaint = int(resp.headers.get('icy-metaint', 0))
                    
                    # Detect codec from headers
                    self._detect_codec(resp.headers)
                    
                    # Build FFmpeg command with proper escaping
                    ffmpeg_process = (
                        ffmpeg
                        .input('pipe:', **self._get_input_args())
                        .output('pipe:', **self._get_output_args())
                        .global_args('-hide_banner')
                        .global_args('-loglevel','warning')
                        .run_async(
                            pipe_stdin=True,
                            pipe_stdout=True,
                            pipe_stderr=True,
                            overwrite_output=True
                        )
                    )
                    
                    # Start stderr reader
                    stderr_reader = asyncio.create_task(self._read_ffmpeg_errors(ffmpeg_process))

                    # Create audio source and track properly
                    source = rtc.AudioSource(SAMPLE_RATE, NUM_CHANNELS)
                    track = rtc.LocalAudioTrack.create_audio_track(
                        name="radio_stream",
                        source=source
                    )
                    self.source = source
                    await self.ctx.room.local_participant.publish_track(track)
                    
                    # Start audio processing
                    process_task = asyncio.create_task(self._process_audio(ffmpeg_process))
                    
                    # Stream data to FFmpeg
                    bytes_until_metadata = metaint if metaint > 0 else None
                    # audio_buffer = bytearray()
                    
                    while self._running:
                        if bytes_until_metadata is not None and bytes_until_metadata <= 0:
                            # Read metadata length byte
                            meta_length_bytes = await resp.content.read(1)
                            if not meta_length_bytes:
                                logger.debug("End of stream reached")
                                break
                            meta_length = ord(meta_length_bytes) * 16
                            
                            # Read metadata if present
                            if meta_length > 0:
                                metadata = await resp.content.read(meta_length)
                                logger.debug("Received metadata: %s", metadata.decode('utf-8', errors='replace'))
                                if self._metadata_callback is not None:
                                    meta_dict = self._parse_icy_metadata(metadata.decode('utf-8', errors='ignore'))
                                    await self._metadata_callback(meta_dict)
                            
                            bytes_until_metadata = metaint
                            continue

                        # Read chunk, respecting metadata boundary
                        chunk_size = min(4096, bytes_until_metadata) if bytes_until_metadata is not None else 4096
                        chunk = await resp.content.read(chunk_size)
                        if not chunk:
                            logger.debug("End of stream reached")
                            break
                        # logger.debug("Read %d bytes from stream", len(chunk))
                        
                        if bytes_until_metadata is not None:
                            bytes_until_metadata -= len(chunk)
                        
                        # Write audio data to FFmpeg
                        try:
                            ffmpeg_process.stdin.write(chunk)
                            ffmpeg_process.stdin.flush()
                        except BrokenPipeError:
                            logger.error("FFmpeg input broken - restarting")
                            break

        except (aiohttp.ClientError, asyncio.CancelledError, ConnectionError) as e:
            logger.error("Stream error [%s]: %s. URL: %s", type(e).__name__, e, url)
            if resp is not None:
                logger.debug("HTTP status: %d, headers: %s", resp.status, dict(resp.headers))
                
        except (RuntimeError) as e:
            logger.error("FFmpeg processing failed [%s]: %s", type(e).__name__, e)
            logger.debug("FFmpeg command: %s", " ".join(ffmpeg_process.args))
        finally:
            self._running = False
            if process_task is not None:
                process_task.cancel()
            if ffmpeg_process:
                ffmpeg_process.kill()
            if stderr_reader is not None:
                await stderr_reader

    async def _write_to_ffmpeg(self, ffmpeg_process, audio_queue: asyncio.Queue):
        """Separate task to write data to FFmpeg"""
        try:
            while self._running:
                chunk = await audio_queue.get()
                try:
                    ffmpeg_process.stdin.write(chunk)
                    ffmpeg_process.stdin.flush()  # Ensure data is written
                except BrokenPipeError:
                    logger.error("FFmpeg input broken - restarting")
                    return
                await asyncio.sleep(0.001)  # Small delay to prevent blocking
        except (aiohttp.ClientError, OSError, asyncio.CancelledError) as e:
            logger.error("FFmpeg write error: %s", e)

    def _detect_codec(self, headers):
        # Map common Icecast content types to codecs
        codec_map = {
            'audio/mpeg': 'mp3',
            'audio/aac': 'aac',
            'audio/aacp': 'aac',
            'audio/ogg': 'ogg',
            'audio/webm': 'webm',
            'audio/wav': 'wav',
        }
        self._codec = 'mp3'  # default
        for ct, codec in codec_map.items():
            if ct in headers.get('Content-Type', ''):
                self._codec = codec
                break
        logger.info("Detected stream codec: %s", {self._codec})

    def _get_input_args(self):
        return {
            'f': 'mp3',
            'ac': '2',     # Force stereo input
            'fflags': '+nobuffer+igndts',  # Reduce buffering, ignore timestamps
            'threads': '1',  # Single threaded decoding
            'probesize': '32768',  # Larger probe size
            'analyzeduration': '0'  # Don't analyze duration
        }

    def _get_output_args(self):
        return {
            'f': 's16le',
            'ar': str(SAMPLE_RATE),
            'ac': str(NUM_CHANNELS),
            'acodec': 'pcm_s16le',
            'af': 'aresample=48000'  # Simple resampling
        }

    def _parse_metadata(self, stream):
        length = ord(stream.read(1)) * 16
        if length > 0:
            metadata = stream.read(length).decode('utf-8', errors='ignore')
            return self._parse_icy_metadata(metadata)
        return None

    def _parse_icy_metadata(self, data):
        # Example: "StreamTitle='LATINO ';StreamUrl='';"
        metadata = {}
        parts = data.split(';')
        for part in parts:
            if '=' in part:
                key, val = part.split('=', 1)
                metadata[key.strip()] = val.strip("' ")
        return metadata

    # Add metadata header parsing
    def _parse_icy_headers(self, resp):
        self.meta_int = int(resp.headers.get('icy-metaint', '0'))
        if self.meta_int > 0:
            logger.info("Stream contains ICY metadata every %d bytes", self.meta_int)

    async def _read_ffmpeg_errors(self, process):
        try:
            while True:
                line = await asyncio.get_event_loop().run_in_executor(
                    None, process.stderr.readline
                )
                if not line:
                    break
                logger.error("FFmpeg: %s", line.decode().strip())
        except (BrokenPipeError, RuntimeError) as e:
            logger.error("FFmpeg stderr read failed: %s", e)

    async def _process_audio(self, ffmpeg_process):
        try:
            # Create a smaller queue to prevent large backlogs
            frame_queue = queue.Queue(maxsize=100)
            
            # Start audio processing in a separate thread
            process_thread = threading.Thread(
                target=self._process_audio_thread,
                args=(ffmpeg_process, frame_queue),
                daemon=True
            )
            process_thread.start()
            
            # Process frames in the async context
            frame_count = 0
            buffer_frames = []
            buffer_size = 50  # Increased from 25 (1 second buffer at 20ms frames)
            last_frame_time = time.monotonic()
            frame_interval = SAMPLES_PER_FRAME / SAMPLE_RATE  # ~20ms per frame
            
            while self._running:
                try:
                    # Get frame with shorter timeout
                    frame = await asyncio.get_event_loop().run_in_executor(
                        None, frame_queue.get, True, 0.1
                    )
                    
                    current_time = time.monotonic()
                    time_since_last = current_time - last_frame_time
                    
                    # If we're falling behind, skip frames to catch up
                    if time_since_last > frame_interval * 3:
                        # logger.warning("Audio falling behind...")
                        if frame_queue.qsize() > 20:  # Increased backlog threshold
                            # Clear backlog
                            while not frame_queue.empty():
                                try:
                                    frame_queue.get_nowait()
                                except queue.Empty:
                                    break
                            continue
                    
                    # Buffer initial frames
                    if frame_count < buffer_size:
                        buffer_frames.append(frame)
                        frame_count += 1
                        if frame_count == buffer_size:
                            logger.info("Initial buffer filled with %d frames...", buffer_size)
                            # Send all buffered frames
                            for buffered_frame in buffer_frames:
                                await self.source.capture_frame(buffered_frame)
                                await asyncio.sleep(frame_interval)
                            buffer_frames = []
                        continue
                    
                    # Regular playback
                    await self.source.capture_frame(frame)
                    last_frame_time = current_time
                    
                    # Small delay to maintain timing
                    next_frame_time = last_frame_time + frame_interval
                    sleep_time = next_frame_time - time.monotonic()
                    if sleep_time > 0:
                        await asyncio.sleep(sleep_time)
                    
                    # Log stats periodically
                    frame_count += 1
                    if frame_count % 500 == 0:
                        logger.debug("Processed %d frames, queue size: %d", frame_count, frame_queue.qsize())
                        
                except (queue.Empty, asyncio.TimeoutError):
                    continue
                except (BlockingIOError, RuntimeError) as e:
                    logger.error("Audio processing error: %s", e)
                    break
                    
        except (RuntimeError) as e:
            logger.error("Audio processing error: %s", e)
            logger.exception("Audio processing stack trace:")
        finally:
            self._running = False
            process_thread.join(timeout=1.0)  # Wait for thread to finish

    def _process_audio_thread(self, ffmpeg_process, frame_queue: queue.Queue):
        """Process audio in a separate thread"""
        try:
            while self._running:
                try:
                    # Read raw audio data
                    raw_data = ffmpeg_process.stdout.read(SAMPLES_PER_FRAME * NUM_CHANNELS * SAMPLE_WIDTH)
                    if not raw_data or len(raw_data) < SAMPLES_PER_FRAME * NUM_CHANNELS * SAMPLE_WIDTH:
                        if self._running:
                            logger.error("FFmpeg output ended unexpectedly")
                        break

                    # If queue is full, skip this frame
                    if frame_queue.full():
                        continue  # Skip frame if queue is full

                    # Convert to numpy array and apply volume
                    audio_data = np.frombuffer(raw_data, dtype=np.int16)
                    with self._volume_lock:
                        audio_data = (audio_data * self._volume).astype(np.int16)
                    
                    # Create audio frame
                    frame = rtc.AudioFrame(
                        data=audio_data.tobytes(),
                        sample_rate=SAMPLE_RATE,
                        num_channels=NUM_CHANNELS,
                        samples_per_channel=SAMPLES_PER_FRAME
                    )

                    # Put frame in queue without blocking
                    if self._running:  # Check again before putting frame
                        try:
                            frame_queue.put_nowait(frame)
                        except queue.Full:
                            continue  # Skip frame if queue is full
                except (BlockingIOError, RuntimeError) as e:  # I/O and thread safety errors
                    if self._running:
                        logger.error("Error processing audio frame: %s", e)
                    break

        except (RuntimeError) as e:
            if self._running:
                logger.error("Audio thread error: %s", e)
                logger.exception("Audio thread stack trace:")

    def set_volume(self, volume: float):
        """Set volume level (0.0 to 1.0)"""
        with self._volume_lock:
            self._volume = max(0.0, min(1.0, volume))
            logger.debug("Stream volume set to %.2f", self._volume)

    def set_metadata_callback(self, callback: Callable[[dict], Awaitable[None]]):
        """Set async callback for receiving metadata updates."""
        if not callable(callback):
            raise ValueError("Metadata callback must be callable")
        self._metadata_callback = callback
