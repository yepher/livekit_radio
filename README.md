# LiveKit Radio Assistant

A voice-controlled radio streaming assistant built with LiveKit that allows users to interact with internet radio streams through natural language.

## Overview

It is generally more graceful to use LiveKit [ingress](https://docs.livekit.io/home/ingress/overview/) to stream audio. But I wanted to try something different where the agent streams audio directly to the room.

This project is a proof of concept that creates an interactive voice assistant that can:

- Play internet radio streams on demand
- Respond to voice commands to control playback
- Display currently playing song information
- Track song history
- Adjust volume automatically during conversations

## Key Components

### 1. Voice Assistant (livekit_radio.py)

- Handles voice interactions using LiveKit's agent system
- Uses OpenAI for speech-to-text and text-to-speech
- Processes natural language commands through a chat context
- Manages participant connections and audio routing

### 2. Stream Handler (icecast_stream.py)

- Manages connection to [Icecast/SHOUTcast](https://icecast.org/) radio streams
- Handles audio processing and buffering using FFmpeg
- Processes ICY metadata for song information
- Controls audio volume during voice interactions
- Ensures smooth playback with proper error handling

### 3. Stream Management (functions.py)

- Loads and manages stream configurations from streams.json
- Provides stream search functionality by name or genre
- Tracks song history and metadata formatting
- Handles fallback streams when requested content isn't found

## Features

- **Voice Control**: Natural language commands to play, stop, and control radio streams
- **Stream Discovery**: Find streams by name or genre
- **Metadata Display**: Shows currently playing song information
- **Song History**: Tracks and displays previous songs
- **Smart Volume**: Automatically adjusts stream volume during conversations
- **Error Recovery**: Handles connection issues and stream interruptions gracefully

## Configuration

Streams are configured in `streams.json` with the following format:

```json
[
    {
        "name": "Stream Name",
        "url": "http://stream.url",
        "genre": "Genre"
    }
]
```

## Requirements

- Python 3.8+
- LiveKit SDK
- FFmpeg
- Additional dependencies in requirements.txt

## Environment Variables

Create a `.env` file with:

```
LIVEKIT_API_KEY=your_api_key
LIVEKIT_API_SECRET=your_api_secret
OPENAI_API_KEY=your_openai_key
```

## Usage

1. Install dependencies:

```
# On macOS
brew install ffmpeg

# On Ubuntu/Debian
sudo apt-get install ffmpeg

```


```bash
python -m venv .venv

source .venv/bin/activate
pip install --upgrade pip

pip install -r requirements.txt
```

2. Start the assistant:

```bash
python livekit_radio.py dev
```

3. Connect to the LiveKit room and interact using voice commands:

- "Play some jazz"
- "What song is playing?"
- "Stop the music"
- "What was the last song?"

## Technical Details

- Audio Sample Rate: 48kHz
- Channels: Stereo (2)
- Sample Width: 16-bit
- Frame Size: 960 samples (20ms)
- Supports multiple stream formats (MP3, AAC, OGG)
- Automatic codec detection and handling

## Error Handling

The system includes robust error handling for:

- Stream connection issues
- Audio processing errors
- Metadata parsing problems
- Network interruptions
- Resource cleanup on disconnect

## Contributing

Contributions are welcome! Please submit pull requests with any improvements or bug fixes.


## TOD:

* Handle `WARNING livekit-radio - Audio falling behind...`
* Be more graceful when stopping music `ERROR livekit-radio - Stream error:`