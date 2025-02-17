"""LiveKit Radio Agent - Voice-controlled internet radio streaming integration.

Handles voice command processing, audio streaming management, and real-time
metadata synchronization through LiveKit's real-time communication platform."""

from typing import Annotated
import asyncio
import logging 
from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    AutoSubscribe,
    JobContext,
    JobProcess,
    WorkerOptions,
    cli,
    llm,
    metrics,
)
from livekit.agents.pipeline import VoicePipelineAgent
from livekit.plugins import deepgram, openai, silero

from icecast_stream import IcecastStreamer
from functions import format_metadata, SongHistory, AssistantFnc

load_dotenv()
logger = logging.getLogger("livekit-radio")

ENABLE_COMFORT_NOISE = True
SAMPLE_RATE = 48000
NUM_CHANNELS = 2  # Stereo
SAMPLE_WIDTH = 2  # 16-bit
DEFAULT_ICE_URL = "http://latino.amgradio.ru/latino"
DEFAULT_STREAMS = {
    "default": DEFAULT_ICE_URL,
}
SAMPLES_PER_FRAME = 960  # 20ms frames at 48kHz

def prewarm(proc: JobProcess):
    """"The stuff we want to do before the job starts"""
    proc.userdata["vad"] = silero.VAD.load()

async def entrypoint(ctx: JobContext):
    """The main entrypoint for the job"""
    
    # Connect to the room FIRST
    logger.info("connecting to room %s", ctx.room.name)
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    # Create chat manager early
    chat = rtc.ChatManager(ctx.room)

    # THEN create audio source and publish track
    source = rtc.AudioSource(SAMPLE_RATE, NUM_CHANNELS)
    track = rtc.LocalAudioTrack.create_audio_track("radio-stream", source)
    await ctx.room.local_participant.publish_track(track)
    
    # Create song history tracker
    song_history = SongHistory()
    
    # Create streamer AFTER publishing track
    streamer = IcecastStreamer(source, ctx)
    
    # Create function context with both dependencies
    fnc_ctx = AssistantFnc(streamer, song_history)
    
    # Metadata callback setup
    async def send_metadata(meta):
        if 'StreamTitle' in meta:
            logger.info("Now playing: %s", meta)
            metadata = {'title': meta['StreamTitle']}
            song_history.update_current_song(metadata)
            formatted_msg = format_metadata(metadata)
            await chat.send_message(formatted_msg)
    streamer.set_metadata_callback(send_metadata)

    # Add participant disconnect handler
    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        # Get count of remaining participants (excluding the agent)
        remaining_participants = ctx.room.remote_participants
        remaining_count = len(remaining_participants)

        logger.info("participant disconnected: %s", participant.identity)
        logger.info("There are %d users left", remaining_count)

        if remaining_count <= 0:
            logger.info("All users left, stopping radio stream")
            streamer.stop()

    # Rest of initialization
    initial_ctx = llm.ChatContext().append(
        role="system",
        text=(
            "You are a voice assistant created by LiveKit. Your interface with users will be voice. "
            "You should use short and concise responses, and avoiding usage of unpronouncable punctuation. "
            "You can play radio streams when requested using the 'play_stream' function. "
            "Users can request streams by name (like 'Play Cydonia') or by genre (like 'Play some techno'). "
            "You can stop playing music when asked using the 'stop_stream' function. "
            "You can tell users what song is currently playing with 'get_current_song' function. "
            "You can tell users what song played previously with 'get_previous_song' function. "
            "Respond to requests like 'stop the music', 'turn it off', 'stop playing' by calling stop_stream. "
            "Respond to requests like 'what's playing', 'what song is this' by calling get_current_song. "
            "Respond to requests like 'what was the last song', 'previous song' by calling get_previous_song."
        ),
    )

    # wait for the first participant to connect
    participant = await ctx.wait_for_participant()
    logger.info("starting voice assistant for participant %s", participant.identity)

    dg_model = "nova-2-general"
    if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
        # use a model optimized for telephony
        dg_model = "nova-2-phonecall"

    agent = VoicePipelineAgent(
        vad=ctx.proc.userdata["vad"],
        stt=deepgram.STT(model=dg_model),
        llm=openai.LLM(),
        tts=openai.TTS(),
        fnc_ctx=fnc_ctx,
        chat_ctx=initial_ctx,
        max_nested_fnc_calls=3
    )

    # Event handlers
    @agent.on("agent_started_speaking")
    def _on_agent_started_speaking():
        logger.info("Agent started speaking")
        streamer.set_volume(0.2)  # Reduce volume to 20%
        

    @agent.on("agent_stopped_speaking")
    def _on_agent_stopped_speaking():
        logger.info("Agent stopped speaking")
        streamer.set_volume(1.0)  # Restore full volume

    @agent.on("user_started_speaking")
    def _on_user_started_speaking():
        logger.info("User started speaking")
        streamer.set_volume(0.2)  # Reduce volume to 20%

    @agent.on("user_stopped_speaking")
    def _on_user_stopped_speaking():
        logger.info("User stopped speaking")
        streamer.set_volume(1.0)  # Restore full volume

    agent.start(ctx.room, participant)

    usage_collector = metrics.UsageCollector()

    @agent.on("metrics_collected")
    def _on_metrics_collected(mtrcs: metrics.AgentMetrics):
        metrics.log_metrics(mtrcs)
        usage_collector.collect(mtrcs)

    async def log_usage():
        summary = usage_collector.get_summary()
        logger.info("Usage: %s", summary)

    ctx.add_shutdown_callback(log_usage)

    async def answer_from_text(txt: str):
        chat_ctx = agent.chat_ctx.copy()
        chat_ctx.append(role="user", text=txt)
        stream = agent.llm.chat(chat_ctx=chat_ctx)
        await agent.say(stream)

    @chat.on("message_received")
    def on_chat_received(msg: rtc.ChatMessage):
        if msg.message:
            asyncio.create_task(answer_from_text(msg.message))

    await agent.say("Hey, I can play radio streams. Just ask!", allow_interruptions=True)

if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
        ),
    )