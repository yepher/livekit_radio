"""Radio stream configuration and metadata management.

Provides functions for loading stream data, finding streams by genre/name, 
formatting metadata displays, and tracking song playback history."""

import json
import logging
import random

logger = logging.getLogger("livekit-radio")

DEFAULT_ICE_URL = "http://latino.amgradio.ru/latino"
DEFAULT_STREAMS = {
    "default": DEFAULT_ICE_URL,
}

def get_stream_url(genre: str = "default") -> str:
    """Get default stream URL for a genre.
    
    Args:
        genre: Stream genre to look up (case-insensitive)
        
    Returns:
        str: URL for the requested genre or default if not found
    """
    return DEFAULT_STREAMS.get(genre.lower(), DEFAULT_ICE_URL)

def load_streams():
    """Load radio stream configurations from streams.json.
    
    Returns:
        list: List of stream configurations or empty list on error
    """
    try:
        with open('streams.json', 'r', encoding='utf-8') as f:
            streams = json.load(f)
            logger.info("Loaded %d streams from streams.json", len(streams))
            return streams
    except json.JSONDecodeError as e:
        logger.error("Error parsing streams.json: %s", e)
        return []
    except (FileNotFoundError, PermissionError, IsADirectoryError) as e:
        logger.error("Error loading streams.json: %s", e)
        return []

def find_stream(query: str, streams: list) -> tuple[str, str, str]:
    """
    Find a stream by name or genre with multiple matching strategies.
    
    Args:
        query: Search term (name or genre)
        streams: List of stream configurations to search
        
    Returns:
        tuple: (stream_name, stream_url, match_type) where match_type can be:
               'exact', 'genre', 'suggestion', or 'default'
    """
    query = query.lower()
    
    # First try exact name match
    for stream in streams:
        if query == stream['name'].lower():
            return stream['name'], stream['url'], 'exact'
    
    # Then try genre match
    genre_matches = [s for s in streams if s['genre'].lower() == query]
    if genre_matches:
        chosen = random.choice(genre_matches)
        return chosen['name'], chosen['url'], 'genre'
    
    # Finally try fuzzy genre matching
    for stream in streams:
        if query in stream['genre'].lower() or stream['genre'].lower() in query:
            return stream['name'], stream['url'], 'suggestion'
    
    # Default to first stream if no match
    default = streams[0]
    return default['name'], default['url'], 'default'

def format_metadata(metadata: dict) -> str:
    """
    Format raw metadata into human-readable track information.
    
    Args:
        metadata: Dictionary containing ICY metadata fields
        
    Returns:
        str: Formatted display string for current track
    """
    if not metadata:
        return "♪ Playing..."
    
    # Extract common metadata fields
    title = metadata.get('title', '')
    artist = metadata.get('artist', '')
    song = metadata.get('song', '')
    
    # Some streams combine artist/title in different fields
    if song and not title:
        title = song
    
    if title and artist:
        return f"♪ Now Playing: {artist} - {title}"
    elif title:
        return f"♪ Now Playing: {title}"
    elif artist:
        return f"♪ Now Playing: {artist}"
    else:
        return "♪ Playing..."

class SongHistory:
    """Tracks and manages song history for radio streams."""
    
    def __init__(self, max_history: int = 10):
        """Initialize song history tracker.
        
        Args:
            max_history: Maximum number of previous songs to retain
        """
        self.current_song = None
        self.history = []
        self.max_history = max_history
    
    def update_current_song(self, metadata: dict) -> None:
        """Update current song and add previous to history"""
        if not metadata or 'title' not in metadata:
            return
            
        new_song = metadata['title']
        if new_song != self.current_song and self.current_song:
            self.history.append(self.current_song)
            if len(self.history) > self.max_history:
                self.history.pop(0)
        self.current_song = new_song
        logger.debug("Updating song history with: %s", metadata)
    
    def get_current_song(self) -> str:
        """Get current song info or default message"""
        if self.current_song:
            return f"♪ Currently playing: {self.current_song}"
        return "No song is currently playing"
    
    def get_previous_song(self) -> str:
        """Get previous song info or default message"""
        if self.history:
            return f"♪ Previous song was: {self.history[-1]}"
        return "No previous song information available" 