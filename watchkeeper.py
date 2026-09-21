#!/usr/bin/env python3
"""
Watchkeeper
===========

Receives several VHF/UHF channels simultaneously from a single RTL-SDR capture,
gates each one on carrier presence, segments transmissions into discrete
messages, transcribes them with faster-whisper, and stores each message as a
row in SQLite alongside a WAV file of exactly that transmission.

Design notes
------------
* No scanning. One wide capture (default 2.048 MSPS) covers every channel in
  the preset, so a Ch 16 hail is never missed because we were parked on 22A.
* Audio is the source of truth. The WAV and the database row are written the
  moment a transmission ends. Transcription happens afterwards and updates the
  row. If the transcriber falls behind, crashes, or produces garbage, you still
  have the recording and an entry in the list.
* Demodulation is a property of the preset, not of the program. Aviation
  channels are AM, marine and weather are narrowband FM, and each mode brings
  its own squelch discriminator. Moving from one band to another is a config
  change.
* Squelch is never done on audio level. In AM it is carrier power; in FM it is
  high-frequency noise in the discriminator, because an unsquelched FM
  discriminator outputs *loud* hiss when no carrier is present.
* Timestamps come from a sample counter, not wall clock at processing time, so
  they reflect when the transmission actually happened.

Dependencies: numpy, scipy, soundfile, faster-whisper
External:     rtl_sdr (rtl-sdr package)
"""

import argparse
import json
import collections
import logging
import os
import queue
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import tomllib
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import numpy as np
import soundfile as sf
from scipy.ndimage import minimum_filter1d, uniform_filter1d
from scipy.signal import firwin, lfilter, lfilter_zi, upfirdn

# ============================== Configuration ==============================

BASE_DIR = os.path.expanduser("~/watchkeeper")
AUDIO_DIR = os.path.join(BASE_DIR, "audio")
DB_PATH = os.path.join(BASE_DIR, "watchkeeper.db")

# Shown as the title of the web interface and at the head of exported
# transcripts. Set station_name in [radio] to your vessel or station name.
STATION_NAME = "Radio Log"

SDR_SAMPLE_RATE = 2_048_000      # Hz, complex
SDR_GAIN = "36.4"                # dB; "0" selects the tuner's own AGC
SDR_PPM = "0"                    # crystal error correction
SDR_DEVICE_INDEX = "0"

CHUNK_SECONDS = 0.25             # capture granularity

# Decimation chain: 2.048 MHz -> /16 -> 128 kHz -> /4 -> 32 kHz -> /2 -> 16 kHz
DECIM_STAGE1 = 16
DECIM_STAGE2 = 4
IF_RATE = SDR_SAMPLE_RATE // (DECIM_STAGE1 * DECIM_STAGE2)   # 32000
AUDIO_RATE = IF_RATE // 2                                     # 16000

FM_MAX_DEVIATION = 5000.0        # Hz, narrowband FM
DEEMPHASIS_TAU = 75e-6           # seconds; see note in README section below

# --- AM squelch ---
# AM channels are keyed only while someone is talking, so carrier power is the
# right discriminator and the continuous-carrier problem that forced the FM
# noise squelch does not arise here.
#
# AM_SQUELCH_DBFS is an absolute floor in this program's own dBFS scale, which
# is NOT the same scale gqrx reports: different filter bandwidth and gain
# staging. Treat it as a guard rail. The threshold that normally decides is
# AM_OPEN_MARGIN_DB above the noise floor measured on the reference channels.
AM_SQUELCH_DBFS = -50.0
AM_OPEN_MARGIN_DB = 8.0
AM_CLOSE_MARGIN_DB = 5.0
AM_HANG_S = 0.6                  # ride through a brief unkey or dropout
# The AM detector divides the envelope by a tracked carrier level. That tracker
# must rise almost instantly when a transmitter keys up and fall slowly
# afterwards: a symmetric time constant leaves the divisor at the noise floor
# for its whole duration after the carrier appears, producing a loud click at
# the start of every message.
# Measured against a carrier keying up 20 dB above the floor: a symmetric 30 ms
# tracker leaves a +18.6 dB click, 2 ms attack leaves +8.5 dB, 0.5 ms leaves
# +3.3 dB. Faster than that buys almost nothing and starts to cost tracker
# resolution.
AM_ATTACK_S = 0.0005             # rise to meet a new carrier
AM_RELEASE_S = 0.060             # fall back afterwards
AM_TRACK_MS = 0.5                # tracker resolution
# Carrier detection is near instantaneous, so AM needs far less lead-in than a
# speech gate does. Every millisecond of preroll or tail is un-modulated
# carrier-free noise that the AGC amplifies to full scale.
AM_PREROLL_S = 0.12
AM_TAIL_KEEP_S = 0.12
# Never let the AGC divide by less than this multiple of the measured noise
# amplitude. Without a floor, a segment containing only noise is normalised to
# full scale, which is exactly what a squelch tail sounds like.
AM_AGC_FLOOR_MULT = 2.5
FADE_MS = 40                     # raised-cosine fade at both ends of a clip
# Top of the AM audio passband. Measured S/N on real airband captures is flat
# with frequency, so narrowing this removes noise and speech in equal measure:
# it sounds much cleaner without recovering any words. Purely a taste and
# hiss-tolerance setting, and the quieter settings are not the better ones:
# intelligibility keeps improving above 2000 Hz because that is where the
# consonants live. gqrx AM "narrow" is roughly equivalent to 1500.
AM_AUDIO_HI = 2700
# Same setting for the FM path. FM noise before de-emphasis rises with
# frequency, so narrowing here can help more than it does in AM -- though on
# this receiver the measured noise came out flat, meaning de-emphasis is
# already matched and the gain from narrowing is only proportional to
# bandwidth.
FM_AUDIO_HI = 3600

# --- Squelch / segmentation ---
# Carrier detection uses the classic FM noise squelch: an unmodulated
# discriminator fed no carrier outputs random phase, which is broadband hiss.
# Measuring energy ABOVE the voice band gives a metric that is near 1.2 with no
# carrier and collapses toward 0 when one appears -- and, crucially, is an
# absolute figure independent of signal strength or tuner gain. Comparing power
# to a tracked noise floor cannot work for a continuously-keyed transmitter
# such as NOAA, because the floor primes itself at carrier level.
GATE_FRAME_MS = 10
NOISE_BAND_HZ = 5000             # discriminator energy above this = no carrier
CARRIER_OPEN_NOISE = 0.55        # below this: carrier present
CARRIER_CLOSE_NOISE = 0.75       # above this: carrier gone (hysteresis)
CARRIER_HANG_S = 0.5             # ride through brief dropouts
# Audio kept after the carrier is lost. The hang above is for DECIDING when a
# transmission has ended; this is what ends up in the recording. They are not
# the same thing: an FM discriminator with no carrier outputs full-scale noise,
# so every millisecond of hang that reaches the file is a burst of static --
# which Whisper duly transcribes as "BANG!".
CARRIER_TAIL_KEEP_S = 0.05

# Second stage. Within a carrier, split on silence so a continuously-keyed
# station is broken at natural pauses instead of running to MAX_MESSAGE_S.
# The threshold is anchored to an absolute level rather than being primed from
# the signal, because on marine VHF the carrier and the speech arrive in the
# same frame -- a self-priming floor would calibrate itself to the voice it is
# supposed to detect. Demodulated audio is scaled so full deviation is 1.0, so
# an absolute floor is meaningful independent of RF signal strength.
# Measured against a live NOAA broadcast: speech sits near -13 dB and pauses
# fall to -28..-38 dB, so the boundary belongs around -23 dB. That puts the
# open threshold at -22.9 dB and close at -27.0 dB.
SPEECH_ABS_THRESHOLD = 0.025
SPEECH_OPEN_MARGIN_DB = 9.0
SPEECH_CLOSE_MARGIN_DB = 5.0
# Long enough to ride through pauses inside a sentence, short enough to split a
# continuously-keyed station at section boundaries. Run --monitor and read the
# reported gap percentiles to check this against your own stations.
SPLIT_SILENCE_S = 1.2            # quiet time that ends a message
# Whisper pads every clip to a 30 s encoder window, so a 2 s message costs as
# much CPU as a 12 s one and transcribes far worse for lack of context. Never
# split a continuous station below this length: wait for the first pause after
# it instead. A transmission that ends on its own (carrier drops) is unaffected.
TARGET_MESSAGE_S = 10.0

# --- Automatic squelch calibration ---
# Reference channels are parked on unallocated interstitial frequencies, so
# they see the same front end and the same band conditions as the real
# channels but should never contain a signal. Their idle noise reading gives a
# measured baseline for the carrier thresholds, replacing a hardcoded constant.
CALIBRATION_SECONDS = 20.0
NOMINAL_IDLE_NOISE = 0.86        # what the metric reads on pure noise

# Spectral flatness endpoints for the readability score. Measured, not
# guessed: clean aviation voice reads 0.108, marine voice under heavy noise
# 0.29-0.32, and white noise 0.565.
FLATNESS_SPEECH = 0.05
FLATNESS_NOISE = 0.55

# Readability above which a transcript is never discarded outright, only
# trimmed. Measured across clips with known outcomes: everything containing
# real speech scored 72-95, everything the decoder invented scored 47-52.
# Repetition heuristics work on text and cannot tell a Coast Guard station
# identifying itself three times -- which is correct radio procedure -- from a
# decoder stuck on a phrase. The audio can.
KEEP_ABOVE_QUALITY = 65.0
# Thresholds are stored as fractions of the measured idle value so they scale
# with whatever the reference actually reports.
OPEN_FRACTION = CARRIER_OPEN_NOISE / NOMINAL_IDLE_NOISE
CLOSE_FRACTION = CARRIER_CLOSE_NOISE / NOMINAL_IDLE_NOISE
# A reference reading outside this range means the reference is not seeing
# clean noise -- something is transmitting on it, or the front end is broken.
# Calibrating from that would deafen the receiver, so we refuse and keep the
# defaults.
REF_NOISE_SANE = (0.45, 1.60)
# Change in reference noise power that suggests the antenna path changed.
ANTENNA_ALARM_DB = 6.0

PREROLL_S = 0.4                  # audio kept before the gate opened
TAIL_KEEP_S = 0.3                # audio kept after the signal drops
MIN_MESSAGE_S = 0.7              # shorter bursts are discarded as clicks
# Discard a completed message whose mean carrier-to-noise over its whole
# duration falls below this, in dB. 0 disables the check.
#
# This is a better filter than raising open_margin_db, which judges the entire
# transmission on the instant of key-up: a strong onset followed by a weak,
# garbled body passes that test and fails this one. It also leaves the squelch
# free to catch weak signals, which matters if the same code is ever pointed at
# a distress channel.
MIN_SNR_DB = 0.0
# Matches Whisper's 30 s encoder window. A longer clip needs a second full
# encoder pass for no gain in accuracy: measured 22-32 s to decode a 60 s clip
# against 7-13 s for a 20-25 s one.
MAX_MESSAGE_S = 30.0             # force-close a stuck or continuous transmitter

# Output level. RMS targeting rather than peak, because a single static crack
# at the start of a transmission would otherwise set the scale for the whole
# message and leave the speech too quiet. The gain cap stops a near-silent clip
# from being amplified into noise.
TARGET_RMS = 0.1                 # about -20 dBFS
MAX_NORM_GAIN_DB = 20.0
# Output ceiling, expressed as a crest factor over TARGET_RMS rather than as a
# fraction of full scale. Measured speech crest after normalisation is about
# 3.6, so 4.0 sits just above real speech peaks: transients are pulled down to
# roughly +5 dB over the body while nothing spoken is touched. A ceiling set
# against full scale instead leaves the transient 12 dB up, which still bangs.
LIMITER_CREST = 4.0
LIMITER_LOOKAHEAD_S = 0.005      # gain starts easing down before the peak lands

# --- Whisper ---
MODEL_SIZE = "small.en"
CPU_THREADS = 2
# Decoder cost, not encoder cost, is what varies between clips: the encoder
# always processes one padded 30 s window, so a 2 s message costs the same
# there as a 25 s one. These two settings are what make a bad clip expensive.
#
# beam_size 1 is greedy decoding, roughly half the decoder work of 2.
#
# Each extra temperature is a COMPLETE re-decode, triggered when the
# compression-ratio or log-prob check fails. On noisy audio that happens often,
# so a three-entry ladder means a bad clip can cost three full passes. Keep the
# ladder short when messages arrive faster than they can be transcribed.
BEAM_SIZE = 2
WHISPER_TEMPERATURES = [0.0, 0.2, 0.4]
COMPRESSION_RATIO_THRESHOLD = 2.4
# Prompts are per preset. A prompt primes the decoder's vocabulary, so the
# weather-radio terms below would actively mislead it on Ch 16, where nobody
# says "coastal flood advisory" and half the traffic is procedure words.
WX_PROMPT = (
    "National Weather Service coastal waters forecast, NOAA weather radio "
    "station KWO37, small craft advisory, coastal flood advisory, high surf "
    "advisory, rip current risk, surf heights, south swell, west swell, "
    "high tide, low tide, feet, Pacific Daylight Time, sea temperature, "
    "air temperature, watches warnings and advisories, knots, wave height."
)

MARINE_PROMPT = (
    "Securite securite securite, pan pan, mayday, this is the United States "
    "Coast Guard Sector Los Angeles Long Beach, all stations all stations, "
    "vessel traffic service, motor vessel, sailing vessel, tug and tow, "
    "switch and answer channel one six, channel two two alpha, channel "
    "one three, bridge to bridge, inbound, outbound, Angels Gate, Queens "
    "Gate, Long Beach Pilot, over, out, roger, wilco, say again, standing by."
)

# Default used by the benchmark harness when no preset is in play.
INITIAL_PROMPT = WX_PROMPT

# Confidence thresholds for flagging a transcript as unreliable. These are the
# defence against confidently-wrong output: Whisper reports how certain it was,
# and anything past these is shown as "(unclear)" rather than presented as
# fact. Tighten them (a less negative logprob, a lower no-speech figure) to
# have more marginal transcripts flagged.
LOW_CONF_AVG_LOGPROB = -1.0
LOW_CONF_NO_SPEECH = 0.60

# Whisper's own gatekeepers. VAD_FILTER runs Silero ahead of the decoder and
# drops anything it does not consider speech; on weak or noisy audio it rejects
# real transmissions outright, which surfaces as "no speech found" on a clip a
# person can hear voice in. The carrier squelch has already established that
# something was transmitting, so on a gated channel the VAD is largely
# redundant and mostly costs recall.
# Cap on decoder output, in tokens per second of audio. Speech runs about
# three words a second, so four or five tokens; eight leaves generous headroom.
# Without a cap a decoder that starts looping on noise runs to Whisper's
# 448-token ceiling, which on a four-second clip means a real-time factor near
# 50 -- and with a temperature ladder it does that once per temperature.
MAX_TOKENS_PER_SECOND = 8

# Optional phrase list generated by fetch_hallucinations.py. Absent by default;
# the built-in filters cover the common cases without it.
HALLUCINATION_LIST_PATH = ""

# Thermal governor. Transcription is the hottest thing this program does, and
# on a Pi with a HAT fitted it can hold the SoC at the soft limit indefinitely.
# Waiting between clips when hot costs latency, which is nearly free here --
# traffic is sparse and the log is read after the fact -- and avoids
# throttling, which slows everything including the DSP. Set
# thermal_pause_above to 0 to disable.
THERMAL_PAUSE_ABOVE = 0.0        # degrees C; 0 disables
THERMAL_RESUME_BELOW = 0.0       # resume once back under this
THERMAL_MAX_WAIT_S = 45.0        # never stall longer than this on one clip
VAD_FILTER = True
NO_SPEECH_THRESHOLD = 0.6        # segments above this are discarded
LOG_PROB_THRESHOLD = -1.0        # decodes below this are discarded

# Standard maritime and aeronautical triple calls. A repeated word is the
# signature of a decoder loop, but these are exactly how the most urgent
# transmissions are formatted -- suppressing "mayday mayday mayday" as a
# hallucination would be the single worst failure this program could have.
# How many times a standard call may repeat before it stops being
# phraseology and starts being a decoder loop.
MAX_CALL_REPEATS = 3

TRIPLE_CALLS = {"mayday", "pan", "panpan", "securite", "seelonce",
                "mayonnaise",          # what Whisper often makes of "mayday"
                "secure", "security",  # and of "securite"
                "all", "stations",     # "all stations, all stations, all
                                       # stations" opens every broadcast
                "say", "again", "over", "out"}


def collapse_single_token(text: str):
    """One NUMBER repeated over and over: keep a single instance.

    Short numeric transmissions do this constantly -- "75" comes back as
    "75 75 75 75 75" -- and the number was really said, so discarding it loses
    real content. Restricted to numbers on purpose: a repeated word on noisy
    audio is the ordinary hallucination, and "Gun, gun, gun" collapsing to
    "Gun" would promote junk into something that reads like a report.

    How many times it was said is not recoverable, so the caller marks the
    result unclear rather than presenting it as fact.
    """
    words = re.findall(r"[A-Za-z0-9']+", text)
    if len(words) < 3 or len({w.lower() for w in words}) != 1:
        return None
    return words[0] if words[0].isdigit() else None


def numeric_with_repeats(text: str) -> bool:
    """All numbers, at least one of them repeated.

    Which number was actually said is not recoverable from "200. 100. 200.
    200. 200", so the text stands as decoded but is flagged unreliable rather
    than guessed at.
    """
    words = re.findall(r"[A-Za-z0-9']+", text)
    if len(words) < 3 or not all(w.isdigit() for w in words):
        return False
    return len(set(words)) < len(words)


ELLIPSIS_TAIL = re.compile(r"(?:\s*(?:\.\s*\.\s*\.|\u2026)\s*){2,}\s*$")


def trim_ellipsis_tail(text: str):
    """Drop a run of trailing ellipses.

    Whisper pads with these when it runs out of audio but not out of decoding.
    A single trailing "..." is ordinary punctuation and is left alone; two or
    more in a row are filler.
    """
    cut = ELLIPSIS_TAIL.sub("", text).rstrip(" ,;:-\u2014\u2013")
    return (cut, True) if cut != text else (text, False)


def trim_trailing_loop(text: str):
    """Cut a repetition that runs to the end, keeping what came before it.

    Discarding the whole transcript throws away real content: a transmission
    that decoded as "Islander traffic, K-104 K-104 K-104 K-104" contained two
    real words and a loop on the noise burst that followed. Truncating keeps
    the two words.
    """
    toks = list(re.finditer(r"[A-Za-z0-9'\-]+", text))
    words = [m.group(0).lower() for m in toks]
    if len(words) < 3:
        return text, False
    cut_at = None
    for n in (1, 2, 3, 4):
        if len(words) < 3 * n:
            continue
        gram = words[-n:]
        k = 1
        while len(words) >= (k + 1) * n and words[-(k + 1) * n:-k * n] == gram:
            k += 1
        if k < 3:
            continue
        # A standard call is said three times -- "mayday mayday mayday",
        # "securite securite securite" -- so a short run of call words is
        # phraseology, not a fault. A long one is the decoder stuck: a USCG
        # broadcast ended "...megahertz out Out Out Out Out Out Out Out Out
        # Out Out", and blanket-exempting "out" threw away the whole
        # transmission rather than the eleven stray words at the end.
        if set(gram).issubset(TRIPLE_CALLS) and k <= MAX_CALL_REPEATS:
            continue
        start = len(words) - k * n
        cut_at = start if cut_at is None else min(cut_at, start)
    if cut_at is None:
        return text, False
    if cut_at == 0:
        return "", True          # the whole transcript was the loop
    return text[:toks[cut_at].start()].rstrip(" ,.;:-\u2014\u2013"), True


def trim_repetitive_tail(text: str):
    """Cut a run of sentences at the end that all start the same way.

    A decoder that drifts does not always repeat exactly. It latches onto an
    opening and keeps reaching for new endings: "We'll be right back. We'll be
    back. We'll be waiting for you. We'll be ready." Exact n-gram matching sees
    nothing repeated there, so the earlier trimmer leaves it alone and the
    whole transcript gets discarded -- including the real broadcast in front
    of it.

    Only the TAIL is trimmed. A repeated opening at the start is how urgent
    traffic is formatted: "Securite securite securite", "Mayday mayday mayday".
    """
    parts = [p for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    if len(parts) < 4:
        return text, False

    def opening(s, n=2):
        w = re.findall(r"[a-z']+", s.lower())
        return tuple(w[:n]) if len(w) >= n else None

    tail = opening(parts[-1])
    if not tail:
        return text, False
    run = 0
    for p in reversed(parts):
        if opening(p) == tail:
            run += 1
        else:
            break
    # Three sentences opening identically at the end of a transmission is not
    # how people talk; it is how a decoder drifts.
    if run < 3 or run == len(parts):
        return text, False
    kept = " ".join(parts[:len(parts) - run]).strip()
    return kept, True


def looks_like_loop(text: str) -> bool:
    """Detect a decoder repetition loop.

    Two shapes, because they fail differently. A single word repeated over and
    over shows up as a low unique-word ratio. A repeated PHRASE does not --
    "thanks guys, thanks guys, nice to see you, thanks guys" has a perfectly
    ordinary word ratio while being obviously a loop -- so short n-grams are
    counted as well.

    Both checks exempt repetition made entirely of standard calls, because
    "mayday mayday mayday" and "pan pan, pan pan, pan pan" are exactly how the
    most urgent transmissions are formatted.
    """
    words = re.findall(r"[a-z0-9']+", text.lower())
    if len(words) < 3:
        return False

    uniq = set(words)
    if len(uniq) / len(words) < 0.5 and not uniq.issubset(TRIPLE_CALLS):
        return True

    for n in (2, 3, 4):
        if len(words) < n * 3:
            continue
        counts = {}
        for i in range(len(words) - n + 1):
            gram = tuple(words[i:i + n])
            counts[gram] = counts.get(gram, 0) + 1
        for gram, c in counts.items():
            if c >= 3 and not set(gram).issubset(TRIPLE_CALLS):
                return True
    return False


# Whisper learned subtitle conventions from its training data, so on
# non-speech audio it narrates: "[sound of a plane taking off]", "(gunshots)",
# "*static*", "♪♪". These are always structurally marked, which makes them
# removable with certainty rather than by guesswork.
ANNOTATION_RE = re.compile(
    r"[\[\(\{<][^\]\)\}>]*[\]\)\}>]"     # bracketed spans
    r"|[A-Za-z]*\*{2,}[A-Za-z]*"              # censored words: F***, s**t
    r"|\*[^*]*\*"                            # *asterisk* spans
    r"|[\[\(\{<][^\]\)\}>]*$"               # unclosed, running to the end
    r"|[\u266a\u266b\u2669\u266c]+"          # musical notes
)


# Whisper also narrates in plain prose, with no bracket or marker at all:
# "Sound of a fire crackling.", "Indistinct chatter." Nothing structural
# identifies these, but the opening does -- no operator says "Sound of a".
# Anchored to a sentence start so a place name like "the Sound of Mull" read
# mid-sentence is left alone.
NARRATION_RE = re.compile(
    r"(?:(?<=^)|(?<=[.!?])|(?<=\n))\s*"
    r"(?:the\s+)?"
    r"(?:sounds?\s+of\b"
    r"|noises?\s+of\b"
    r"|indistinct\b|inaudible\b|unintelligible\b"
    r"|(?:music|static|silence|chatter)\s+(?:playing|continues|in the background)\b"
    r")[^.!?]*[.!?]?",
    re.I)


# Whisper also writes sound effects bare, without brackets: "BANG! BANG!",
# "CRASH!", "Beep beep". Caps plus an exclamation mark is the reliable tell --
# no radio operator is transcribed that way, while genuine acronyms like USCG,
# LAX, VTS and DSC never carry one.
ONOMATOPOEIA = {
    "bang", "boom", "crash", "thud", "beep", "honk", "click", "clack", "pop",
    "whoosh", "splash", "screech", "rumble", "buzz", "ding", "dong", "knock",
    "tap", "hiss", "roar", "siren", "horn", "whistle", "clang", "sizzle",
    "crackle", "applause", "laughter", "gunshot", "gunshots", "explosion",
    "squeak", "rustle", "thump", "slam", "ping", "chime", "ring",
    "bam", "blam", "wham", "whack", "smack", "clunk", "rattle", "crack",
    "zap", "bleep", "boop", "brr", "swoosh", "plop", "clap", "tick", "tock",
}
# Never stripped, whatever the punctuation.
SHOUTED_EXEMPT = {"sos", "mayday", "pan", "securite", "help", "fire"}

ONOMATOPOEIA_RE = re.compile(
    r"\b([A-Za-z]{3,})\b[!]+"          # any word with an exclamation mark
    r"|\b([A-Z]{3,})\b",               # or a bare all-caps word
)


def _drop_sound_word(m):
    low = (m.group(1) or m.group(2) or "").lower()
    if low in SHOUTED_EXEMPT:
        return m.group(0)
    # Bare all-caps words are kept -- those are acronyms like USCG or VTS.
    return " " if low in ONOMATOPOEIA else m.group(0)


def strip_annotations(text: str) -> str:
    cleaned = ANNOTATION_RE.sub(" ", text)
    cleaned = NARRATION_RE.sub(" ", cleaned)
    cleaned = ELLIPSIS_TAIL.sub("", cleaned)
    cleaned = ONOMATOPOEIA_RE.sub(_drop_sound_word, cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.;:-\u2014\u2013")
    # "Beep beep!" loses only the marked word above, leaving a bare "Beep".
    # If nothing but sound words remains, there was no speech in it.
    words = re.findall(r"[A-Za-z']+", cleaned.lower())
    if words and all(w in ONOMATOPOEIA for w in words):
        return ""
    return cleaned


# Phrases from Whisper's subtitle training data. Unlike the blocklist below,
# which only matches a whole transcript, the presence of any of these ANYWHERE
# condemns the entire output: no aviation or marine transmission contains them,
# so one appearing means the decoder stopped transcribing and started reciting
# YouTube outros.
SUBTITLE_CORPUS = re.compile(
    r"thank(?:s| you) for watching"
    r"|please\s+subscribe|subscribe to (?:my|the) channel"
    r"|(?:like|hit) (?:and|the) (?:subscribe|like button|bell)"
    r"|subscribe and like|don'?t forget to subscribe"
    r"|see you (?:next time|in the next)"
    r"|subtitles? (?:by|and corrections)|captions? by|transcription by"
    r"|amara\.org|www\.|\.com\b",
    re.I)

# Words that make a transcript safety-relevant. Their presence is never
# trusted as fact: a real distress call and a hallucinated one look identical
# in text, and both need a human to listen. Logged loudly and flagged in the
# web interface.
DISTRESS_RE = re.compile(
    r"\bmay ?day\b|\bpan[ -]?pan\b|\bsecurit[ae]\b|\bseelonce\b"
    r"|\bdistress\b|\bsinking\b|\babandon(?:ing)? ship\b"
    r"|\bman overboard\b|\btaking on water\b|\bemergency\b", re.I)

def load_hallucination_list(path):
    """Merge a generated phrase list into the built-in filters.

    Written by fetch_hallucinations.py from the public whisper-hallucinations
    dataset, with anything resembling real radio traffic already removed. The
    built-in lists work unchanged if the file is absent.
    """
    global SUBTITLE_CORPUS
    try:
        with open(os.path.expanduser(path)) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        log.debug("no hallucination list at %s: %s", path, e)
        return 0, 0

    exact = [p.strip().lower() for p in data.get("exact", []) if p.strip()]
    HALLUCINATION_BLOCKLIST.update(exact)

    contains = [p.strip() for p in data.get("contains", []) if p.strip()]
    if contains:
        # Whitespace in the stored phrases is normalised, so allow any run of
        # spaces or punctuation between words when matching a real transcript.
        parts = [r"\s*[^a-z0-9]*\s*".join(re.escape(w) for w in p.split())
                 for p in sorted(contains, key=len, reverse=True)]
        SUBTITLE_CORPUS = re.compile(
            SUBTITLE_CORPUS.pattern + "|" + "|".join(parts), re.I)
    log.info("hallucination list: %d exact-match phrase(s), %d blocked "
             "anywhere (from %s)", len(exact), len(contains),
             os.path.basename(path))
    return len(exact), len(contains)


def strip_subtitle_corpus(text: str):
    """Remove subtitle boilerplate, sentence by sentence, keeping the rest.

    The outro is appended AFTER real speech, so condemning the whole transcript
    throws away the transmission. A 21-second VTS report about a
    non-participating vessel was discarded whole because "Thanks for watching"
    followed it.
    """
    parts = re.split(r"(?<=[.!?])\s+", text)
    kept = [p for p in parts if not SUBTITLE_CORPUS.search(p)]
    if len(kept) == len(parts):
        return text, False
    return " ".join(kept).strip(), True


# Text that Whisper reliably invents when fed noise
HALLUCINATION_BLOCKLIST = {
    "", ".", "you", "thank you", "thank you.", "thanks for watching",
    "thanks for watching!", "bye", "bye.", "bye bye", "please subscribe",
    "subtitles by the amara.org community", "www.mooji.org", "[blank_audio]",
    "silence", "music", "[music]",
    # Seen in practice on short clips from this receiver:
    "we'll see you tomorrow", "we'll see you next time", "see you next time",
    "i'll see you next time", "okay", "so", "the end",
}

# --- Retention ---
MAX_MESSAGES = 100               # rows kept; WAVs deleted with their row

# Raw I/Q is 4.1 MB/s at 2.048 MSPS, so about 15 GB per hour. The cap stops a
# forgotten recording from filling the card.
MAX_RECORD_GB = 8.0

# Capture chunks buffered ahead of the DSP. Each is CHUNK_SECONDS of raw I/Q,
# about 1 MB at 2.048 MSPS. A deep queue costs almost nothing in memory and
# absorbs a burst of slow transcriptions that would otherwise cost real audio.
QUEUE_DEPTH = 40                 # 10 seconds of slack

# How many messages may wait for transcription. Kept short deliberately: the
# queue is served newest-first, so a deep backlog would only hold entries that
# will never be reached before newer traffic displaces them. Anything evicted
# keeps its audio and is marked so the log says so honestly.
TRANSCRIBE_BACKLOG = 12

# ============================== Channel presets ==============================
# Every channel in a preset must sit inside center +/- (SDR_SAMPLE_RATE * 0.4),
# and every offset from center must be a whole multiple of 12.5 kHz so the
# mixer oscillator repeats exactly once per chunk (see Channelizer.__init__).


@dataclass
class ChannelSpec:
    name: str
    freq_hz: int
    voice: bool = True          # False = recorded but never transcribed
    role: str = "voice"         # "voice" | "diagnostic" | "reference"


AIRBAND_PROMPT = (
    "Southern California Approach, Los Angeles Center, LAX Tower, Hawthorne "
    "Ground, Burbank Terminal Radar, Orange County Approach, Palm Springs "
    "Approach, cleared for takeoff, cleared to land, runway two five left, "
    "runway seven right, line up and wait, contact departure, squawk, ident, "
    "heavy, descend and maintain, climb and maintain, flight level, turn left "
    "heading, traffic in sight, altimeter, wind two seven zero at one zero, "
    "niner, tree, fife, roger, wilco, say again, radar contact, good day."
)


PRESETS = {
    # --- Aviation AM. Development band: constant discrete transmissions.
    "socal_low": {
        "center_hz": 125_200_000,
        "mode": "am",
        "prompt": AIRBAND_PROMPT,
        "channels": [
            ChannelSpec("Coastal Intermediate", 124_300_000),
            ChannelSpec("LAX West Arrivals", 124_500_000),
            ChannelSpec("Burbank Terminal Radar", 124_600_000),
            ChannelSpec("LAX South Arrivals", 124_900_000),
            ChannelSpec("HHR Ground", 125_100_000),
            ChannelSpec("Palm Springs Approach", 125_250_000),
            ChannelSpec("Orange County Approach", 125_350_000),
            ChannelSpec("Joshua Control Approach", 126_100_000),
            # 12.5 kHz off the 25 kHz airband grid, and clear of the known
            # 125.001 MHz oscillator spur.
            ChannelSpec("ref-a", 124_712_500, voice=False, role="reference"),
            ChannelSpec("ref-b", 125_787_500, voice=False, role="reference"),
        ],
    },
    "socal_high": {
        "center_hz": 133_600_000,
        "mode": "am",
        "prompt": AIRBAND_PROMPT,
        "channels": [
            ChannelSpec("High Altitude Center", 132_850_000),
            ChannelSpec("LAX Tower North", 133_900_000),
            ChannelSpec("Inland Transition Sector", 134_350_000),
            ChannelSpec("ref-a", 133_312_500, voice=False, role="reference"),
            ChannelSpec("ref-b", 134_112_500, voice=False, role="reference"),
        ],
    },
    # --- Narrowband FM. The eventual target.
    # Development preset. NOAA gives a strong continuous signal to exercise the
    # pipeline; the two AIS channels are a propagation meter -- if AIS bursts
    # start appearing, ship-borne VHF is reaching the antenna.
    "wx": {
        "center_hz": 162_262_500,
        "mode": "nfm",
        "prompt": WX_PROMPT,
        "channels": [
            ChannelSpec("AIS-A", 161_975_000, voice=False, role="diagnostic"),
            ChannelSpec("AIS-B", 162_025_000, voice=False, role="diagnostic"),
            ChannelSpec("WX2", 162_400_000),
            ChannelSpec("WX1", 162_550_000),
            # Interstitial, 12.5 kHz off the NOAA grid: nothing transmits here.
            ChannelSpec("ref-a", 162_337_500, voice=False, role="reference"),
            ChannelSpec("ref-b", 162_487_500, voice=False, role="reference"),
        ],
    },
    # Operational preset. Everything from Ch 68 to Ch 22A fits in one capture.
    "marine": {
        "center_hz": 156_762_500,
        "mode": "nfm",
        "prompt": MARINE_PROMPT,
        "channels": [
            ChannelSpec("Ch68", 156_425_000),
            ChannelSpec("Ch09", 156_450_000),
            ChannelSpec("Ch69", 156_475_000),
            ChannelSpec("Ch12", 156_600_000),
            ChannelSpec("Ch13", 156_650_000),
            ChannelSpec("Ch14", 156_700_000),
            ChannelSpec("Ch16", 156_800_000),
            ChannelSpec("Ch22A", 157_100_000),
            # Interstitial, 12.5 kHz off the marine 25 kHz grid.
            ChannelSpec("ref-a", 156_537_500, voice=False, role="reference"),
            ChannelSpec("ref-b", 156_737_500, voice=False, role="reference"),
        ],
    },
}

log = logging.getLogger("watchkeeper")


# ============================== Configuration file ==============================
# Channels, names, modulation and filter settings live in watchkeeper.toml so
# the program itself never has to be edited to add a frequency or move to a
# different band.

CONFIG_SEARCH = [
    "watchkeeper.toml",
    os.path.expanduser("~/watchkeeper.toml"),
    os.path.join(BASE_DIR, "watchkeeper.toml"),
]

# Per-preset settings that override module defaults when that preset is
# selected. Only one preset runs per process, so applying them to the module
# globals at startup is safe and keeps the signal path free of settings
# plumbing. Key in the file -> global name here.
PRESET_OVERRIDES = {
    "audio_hi_hz": "AM_AUDIO_HI",
    "fm_audio_hi_hz": "FM_AUDIO_HI",
    "vad_filter": "VAD_FILTER",
    "no_speech_threshold": "NO_SPEECH_THRESHOLD",
    "log_prob_threshold": "LOG_PROB_THRESHOLD",
    "max_tokens_per_second": "MAX_TOKENS_PER_SECOND",
    "thermal_pause_above": "THERMAL_PAUSE_ABOVE",
    "thermal_resume_below": "THERMAL_RESUME_BELOW",
    "thermal_max_wait_s": "THERMAL_MAX_WAIT_S",
    "hallucination_list": "HALLUCINATION_LIST_PATH",
    # Decode cost settings are per-preset as well as global: a band with sparse
    # traffic can afford a temperature ladder that a busy one cannot.
    "beam_size": "BEAM_SIZE",
    "temperatures": "WHISPER_TEMPERATURES",
    "compression_ratio_threshold": "COMPRESSION_RATIO_THRESHOLD",
    "squelch_dbfs": "AM_SQUELCH_DBFS",
    "open_margin_db": "AM_OPEN_MARGIN_DB",
    "close_margin_db": "AM_CLOSE_MARGIN_DB",
    "hang_s": "AM_HANG_S",
    "preroll_s": "AM_PREROLL_S",
    "carrier_tail_keep_s": "CARRIER_TAIL_KEEP_S",
    "tail_keep_s": "AM_TAIL_KEEP_S",
    "min_message_s": "MIN_MESSAGE_S",
    "min_snr_db": "MIN_SNR_DB",
    "limiter_crest": "LIMITER_CREST",
    "transcribe_backlog": "TRANSCRIBE_BACKLOG",
    "max_message_s": "MAX_MESSAGE_S",
    "fm_open_noise": "CARRIER_OPEN_NOISE",
    "fm_close_noise": "CARRIER_CLOSE_NOISE",
    "fm_split_silence_s": "SPLIT_SILENCE_S",
    "fm_target_message_s": "TARGET_MESSAGE_S",
}

STORAGE_OVERRIDES = {
    "base_dir": "BASE_DIR",
    "audio_dir": "AUDIO_DIR",
    "db_path": "DB_PATH",
}

RADIO_OVERRIDES = {
    "station_name": "STATION_NAME",
    "sample_rate": "SDR_SAMPLE_RATE",
    "gain": "SDR_GAIN",
    "ppm": "SDR_PPM",
    "device_index": "SDR_DEVICE_INDEX",
    "max_messages": "MAX_MESSAGES",
    "max_record_gb": "MAX_RECORD_GB",
    "model": "MODEL_SIZE",
    "cpu_threads": "CPU_THREADS",
    "beam_size": "BEAM_SIZE",
    "temperatures": "WHISPER_TEMPERATURES",
    "compression_ratio_threshold": "COMPRESSION_RATIO_THRESHOLD",
    "low_conf_avg_logprob": "LOW_CONF_AVG_LOGPROB",
    "low_conf_no_speech": "LOW_CONF_NO_SPEECH",
}


def find_config(explicit=None):
    if explicit:
        if not os.path.exists(explicit):
            sys.exit(f"config file not found: {explicit}")
        return explicit
    for path in CONFIG_SEARCH:
        if os.path.exists(path):
            return path
    return None


def load_config(path):
    """Replace the built-in presets with those defined in the file.

    Validation happens here rather than at first use, so a typo in a frequency
    is reported before the dongle is ever opened.
    """
    with open(path, "rb") as f:
        cfg = tomllib.load(f)

    for key, name in RADIO_OVERRIDES.items():
        if key in cfg.get("radio", {}):
            globals()[name] = cfg["radio"][key]

    # Paths are resolved after base_dir so that setting only base_dir moves
    # everything, while audio_dir or db_path can still be pointed elsewhere --
    # which is how the recordings end up on tmpfs while the database stays on
    # durable storage.
    store = cfg.get("storage", {})
    if "base_dir" in store:
        base = os.path.expanduser(store["base_dir"])
        globals()["BASE_DIR"] = base
        globals()["AUDIO_DIR"] = os.path.join(base, "audio")
        globals()["DB_PATH"] = os.path.join(base, "watchkeeper.db")
    for key in ("audio_dir", "db_path"):
        if key in store:
            globals()[STORAGE_OVERRIDES[key]] = os.path.expanduser(store[key])

    _refresh_preset_defaults()

    presets = {}
    for name, body in cfg.get("preset", {}).items():
        if "center_hz" not in body:
            sys.exit(f"[preset.{name}] is missing center_hz")
        mode = body.get("mode", "nfm")
        if mode not in ("am", "nfm"):
            sys.exit(f"[preset.{name}] mode must be 'am' or 'nfm', got {mode!r}")
        chans = []
        for c in body.get("channels", []):
            if "name" not in c or "freq" not in c:
                sys.exit(f"[preset.{name}] every channel needs name and freq")
            chans.append(ChannelSpec(
                name=str(c["name"]),
                freq_hz=int(c["freq"]),
                voice=bool(c.get("voice", c.get("role", "voice") == "voice")),
                role=str(c.get("role", "voice")),
            ))
        if not chans:
            sys.exit(f"[preset.{name}] has no channels")
        presets[name] = {
            "center_hz": int(body["center_hz"]),
            "mode": mode,
            # An absent or empty prompt means no priming at all. Whisper is
            # given nothing to reach for, so what it invents on noise is
            # generic rather than domain-plausible -- and generic inventions
            # are the ones the filters can catch.
            "prompt": (body.get("prompt") or "").strip() or None,
            "channels": chans,
            "settings": {k: v for k, v in body.items() if k in PRESET_OVERRIDES},
        }

    if not presets:
        sys.exit(f"{path} defines no presets")
    globals()["PRESETS"] = presets
    return presets


# Baseline for anything a preset does not set, so one preset's settings cannot
# leak into the next. Refreshed after the [radio] section is read, which makes
# [radio] the default and the preset the override for keys that appear in both.
_PRESET_DEFAULTS = {name: globals()[name] for name in PRESET_OVERRIDES.values()}


def _refresh_preset_defaults():
    _PRESET_DEFAULTS.clear()
    _PRESET_DEFAULTS.update({n: globals()[n] for n in PRESET_OVERRIDES.values()})


def apply_preset_settings(preset):
    for name, default in _PRESET_DEFAULTS.items():
        globals()[name] = default
    for key, value in preset.get("settings", {}).items():
        globals()[PRESET_OVERRIDES[key]] = value


def validate_preset(name, preset):
    """Catch a bad frequency before the radio is touched."""
    center = preset["center_hz"]
    edge = 0.5 * globals()["SDR_SAMPLE_RATE"]
    problems = []
    for c in preset["channels"]:
        off = c.freq_hz - center
        if abs(off) >= edge:
            problems.append(
                f"{c.name} at {c.freq_hz/1e6:.4f} MHz is {abs(off)/1e6:.3f} MHz "
                f"from center, outside the {globals()['SDR_SAMPLE_RATE']/1e6:.3f} "
                f"MHz capture")
        cycles = off * CHUNK_SECONDS
        if abs(cycles - round(cycles)) > 1e-9:
            problems.append(
                f"{c.name}: offset {off} Hz is not a multiple of "
                f"{1/CHUNK_SECONDS:g} Hz")
    if problems:
        sys.exit(f"[preset.{name}] is not usable:\n  " + "\n  ".join(problems))

    limit = 0.4 * globals()["SDR_SAMPLE_RATE"]
    edgy = [c for c in preset["channels"] if abs(c.freq_hz - center) > limit]
    for c in edgy:
        log.warning("preset %s: %s is %.0f kHz from center, past the %.0f kHz "
                    "point where the tuner starts rolling off; expect a few dB "
                    "of loss", name, c.name, abs(c.freq_hz - center) / 1e3,
                    limit / 1e3)


# ============================== SDR capture ==============================

class SdrSource:
    """Runs rtl_sdr and hands raw uint8 I/Q chunks to a bounded queue.

    The reader runs in its own thread so that a slow consumer can never stall
    the USB pipe. When the queue fills, the oldest chunk is dropped and the
    loss is counted, because silently losing audio is worse than knowing you
    lost it.
    """

    def __init__(self, center_hz: int, queue_depth: int = QUEUE_DEPTH,
                 record_path: str = None, max_gb: float = MAX_RECORD_GB):
        self.center_hz = center_hz
        self.chunk_bytes = int(SDR_SAMPLE_RATE * CHUNK_SECONDS) * 2
        self.q: "queue.Queue[bytes]" = queue.Queue(maxsize=queue_depth)
        self.dropped_chunks = 0
        self.proc = None
        self._stop = threading.Event()
        self._threads = []
        self.record_path = record_path
        self.max_bytes = int(max_gb * 1e9)
        self._rec = None
        self._rec_bytes = 0

    def start(self):
        cmd = [
            "rtl_sdr",
            "-d", SDR_DEVICE_INDEX,
            "-f", str(self.center_hz),
            "-s", str(SDR_SAMPLE_RATE),
            "-g", SDR_GAIN,
            "-p", SDR_PPM,
            "-",
        ]
        log.info("starting: %s", " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
        )
        if self.record_path:
            self._rec = open(self.record_path, "wb", buffering=1024 * 1024)
            write_iq_sidecar(self.record_path, self.center_hz)
            log.info("recording raw I/Q to %s (%.1f MB/s, cap %.1f GB)",
                     self.record_path, SDR_SAMPLE_RATE * 2 / 1e6,
                     self.max_bytes / 1e9)
        self._threads = [
            threading.Thread(target=self._read_loop, daemon=True),
            threading.Thread(target=self._stderr_loop, daemon=True),
        ]
        for t in self._threads:
            t.start()

    def _read_exact(self, stdout, n: int) -> bytes:
        """Read exactly n bytes.

        Popen was opened with bufsize=0, so stdout is a raw FileIO and read()
        returns whatever a single syscall yielded -- typically the 256 KiB pipe
        buffer, not the full chunk. Treating that as EOF is wrong; loop instead
        and only stop on a genuinely empty read.
        """
        buf = bytearray(n)
        view = memoryview(buf)
        got = 0
        while got < n:
            if self._stop.is_set():
                return b""
            chunk = stdout.read(n - got)
            if not chunk:
                return b""  # real EOF
            view[got:got + len(chunk)] = chunk
            got += len(chunk)
        return bytes(buf)

    def _read_loop(self):
        stdout = self.proc.stdout
        while not self._stop.is_set():
            data = self._read_exact(stdout, self.chunk_bytes)
            if not data:
                rc = self.proc.poll()
                log.warning("rtl_sdr stream ended (exit code %s)", rc)
                break
            if self._rec is not None:
                if self._rec_bytes + len(data) > self.max_bytes:
                    log.warning("I/Q recording hit the %.1f GB cap; "
                                "closing the file and continuing live",
                                self.max_bytes / 1e9)
                    self._rec.close()
                    self._rec = None
                else:
                    self._rec.write(data)
                    self._rec_bytes += len(data)
            try:
                self.q.put_nowait(data)
            except queue.Full:
                try:
                    self.q.get_nowait()
                    self.dropped_chunks += 1
                    if self.dropped_chunks % 20 == 1:
                        log.warning(
                            "DSP behind real time: dropped %d chunks (%.1f s of audio)",
                            self.dropped_chunks, self.dropped_chunks * CHUNK_SECONDS,
                        )
                except queue.Empty:
                    pass
                try:
                    self.q.put_nowait(data)
                except queue.Full:
                    pass
        self.q.put(b"")  # sentinel

    def _stderr_loop(self):
        """rtl_sdr reports PLL lock failures and sample loss here. Do not
        discard it -- 'cb transfer status' messages are how you find out the
        dongle is overheating or the USB link is marginal."""
        for raw in iter(self.proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                log.info("rtl_sdr: %s", line)

    def stop(self):
        self._stop.set()
        if self._rec is not None:
            self._rec.close()
            log.info("wrote %.2f GB of I/Q (%.0f s)", self._rec_bytes / 1e9,
                     self._rec_bytes / (SDR_SAMPLE_RATE * 2))
            self._rec = None
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)


def spectral_flatness(audio, sr=None):
    """Geometric over arithmetic mean of the voice-band spectrum.

    Speech has formants, so its spectrum is lumpy and the ratio sits low
    (0.01-0.15). Noise and most digital modes are flat and sit near 0.55.
    Unlike any level-based measure this is unaffected by gain, limiting or
    the FM capture effect, and it reflects what ARRIVED rather than how good
    the carrier was at our own antenna -- which is what decides whether the
    decoder can make sense of it.
    """
    sr = sr or AUDIO_RATE
    if audio is None or audio.size < 1024:
        return None
    N = 512
    step = N // 2
    frames = [audio[i:i + N] for i in range(0, audio.size - N, step)]
    if not frames:
        return None
    rms = np.array([float(np.sqrt(np.mean(f ** 2))) for f in frames])
    if not rms.size:
        return None
    cut = float(np.percentile(rms, 60))
    win = np.hanning(N)
    freqs = np.fft.rfftfreq(N, 1.0 / sr)
    band = (freqs > 300) & (freqs < 3400)
    vals = []
    for f, r in zip(frames, rms):
        if r <= cut or r < 1e-5:
            continue
        P = np.maximum(np.abs(np.fft.rfft(f * win)) ** 2, 1e-20)[band]
        vals.append(float(np.exp(np.mean(np.log(P))) / np.mean(P)))
    return float(np.median(vals)) if vals else None


def quality_score(flat):
    """Flatness mapped to 0-100, higher meaning more readable.

    Calibrated against clips whose transcription outcome is known: the ones
    that decoded correctly sit at 84-95, the ones the decoder invented text
    for sit at 47-52. A cut around 60 separates them.
    """
    if flat is None:
        return None
    return float(np.clip((FLATNESS_NOISE - flat) /
                         (FLATNESS_NOISE - FLATNESS_SPEECH) * 100.0, 0.0, 100.0))


def soc_temperature():
    """SoC temperature in degrees C, or None where it cannot be read."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def filesystem_of(path: str) -> str:
    """Filesystem type backing a path, from /proc/mounts.

    Used to say out loud whether the recordings are on tmpfs. A tmpfs mount
    that silently failed to come up leaves a perfectly usable directory on the
    SD card underneath, so without checking there is nothing to notice.
    """
    try:
        target = os.path.realpath(path)
        best, best_type = "", "unknown"
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount = parts[1]
                if (target == mount or target.startswith(mount.rstrip("/") + "/")) \
                        and len(mount) > len(best):
                    best, best_type = mount, parts[2]
        return best_type
    except OSError:
        return "unknown"


def write_iq_sidecar(path: str, center_hz: int):
    meta = {
        "center_hz": center_hz,
        "sample_rate": SDR_SAMPLE_RATE,
        "format": "u8 interleaved I/Q, as produced by rtl_sdr",
        "gain_db": SDR_GAIN,
        "ppm": SDR_PPM,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(path + ".json", "w") as f:
        json.dump(meta, f, indent=2)


def read_iq_sidecar(path: str):
    try:
        with open(path + ".json") as f:
            return json.load(f)
    except OSError:
        return {}


class FileSource:
    """Replays a recorded I/Q capture in place of the dongle.

    Runs as fast as the DSP allows rather than in real time, which is the whole
    point: a few minutes of real Ch 16 traffic can be pushed through the
    channelizer repeatedly at a desk while tuning squelch thresholds and
    segmentation, with no antenna and no waiting.
    """

    def __init__(self, path: str, queue_depth: int = 8):
        self.path = path
        self.meta = read_iq_sidecar(path)
        self.center_hz = self.meta.get("center_hz")
        rate = self.meta.get("sample_rate", SDR_SAMPLE_RATE)
        if rate != SDR_SAMPLE_RATE:
            raise ValueError(
                f"{path} was recorded at {rate} S/s but this build runs at "
                f"{SDR_SAMPLE_RATE}; the channel offsets would be wrong")
        self.chunk_bytes = int(SDR_SAMPLE_RATE * CHUNK_SECONDS) * 2
        self.q: "queue.Queue[bytes]" = queue.Queue(maxsize=queue_depth)
        self.dropped_chunks = 0
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        size = os.path.getsize(self.path)
        log.info("replaying %s: %.2f GB, %.0f s of capture at %.3f MHz",
                 self.path, size / 1e9, size / (SDR_SAMPLE_RATE * 2),
                 (self.center_hz or 0) / 1e6)
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self):
        with open(self.path, "rb") as f:
            while not self._stop.is_set():
                data = f.read(self.chunk_bytes)
                if len(data) < self.chunk_bytes:
                    break
                self.q.put(data)          # block rather than drop; not live
        self.q.put(b"")

    def stop(self):
        self._stop.set()


# ============================== DSP ==============================

_U8_LUT = ((np.arange(256, dtype=np.float32) - 127.5) / 127.5).astype(np.float32)


def u8_to_complex(raw: bytes) -> np.ndarray:
    buf = np.frombuffer(raw, dtype=np.uint8)
    out = np.empty(buf.size // 2, dtype=np.complex64)
    out.real = _U8_LUT[buf[0::2]]
    out.imag = _U8_LUT[buf[1::2]]
    return out


class PolyphaseDecimator:
    """FIR decimator with state carried across chunks.

    scipy's upfirdn has no state, so we prepend the tail of the previous chunk
    and discard the outputs that depend on the zero-padded warm-up. Requires
    len(taps) - 1 to be a multiple of the decimation factor, and the input
    length to be a multiple of it too, which keeps the output length constant.
    """

    def __init__(self, taps: np.ndarray, factor: int, dtype=np.complex64):
        self.taps = taps.astype(np.float32)
        self.factor = factor
        self.history = (len(self.taps) // factor) * factor
        assert self.history >= len(self.taps) - 1
        self.skip = self.history // factor
        self.state = np.zeros(self.history, dtype=dtype)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        assert len(x) % self.factor == 0, "chunk length must divide evenly"
        ext = np.concatenate((self.state, x))
        y = upfirdn(self.taps, ext, up=1, down=self.factor)
        self.state = ext[-self.history:]
        return y[self.skip: self.skip + len(x) // self.factor]


class OnePoleDeemphasis:
    def __init__(self, tau: float, fs: float):
        a = 1.0 - np.exp(-1.0 / (fs * tau))
        self.b = np.array([a], dtype=np.float64)
        self.a = np.array([1.0, -(1.0 - a)], dtype=np.float64)
        self.zi = lfilter_zi(self.b, self.a) * 0.0

    def __call__(self, x: np.ndarray) -> np.ndarray:
        y, self.zi = lfilter(self.b, self.a, x, zi=self.zi)
        return y.astype(np.float32)


def limit(audio: np.ndarray) -> np.ndarray:
    """Look-ahead limiter applied to a finished message.

    A waveshaper cannot fix a key-up transient: something six times over the
    ceiling still comes out at the ceiling, just without the clipping
    harmonics, and it is still a bang. What works is reducing gain *before*
    the peak arrives and restoring it gradually afterwards, which is audible
    as a brief dip rather than a click.

    Running this on the finished message rather than per chunk means the
    look-ahead needs no state and cannot glitch at a chunk boundary.
    """
    if audio.size == 0:
        return audio
    ceiling = min(0.9, LIMITER_CREST * TARGET_RMS)
    peak = float(np.max(np.abs(audio)))
    if peak <= ceiling:
        return audio

    n_la = max(1, int(LIMITER_LOOKAHEAD_S * AUDIO_RATE))
    need = np.minimum(1.0, ceiling / np.maximum(np.abs(audio), 1e-9))
    # Symmetric running minimum: the gain is already down when the peak
    # arrives and stays down until it has passed.
    gain = minimum_filter1d(need, size=2 * n_la + 1, mode="nearest")
    # Smooth so the gain change itself is not a step, which would click too.
    gain = uniform_filter1d(gain, size=n_la, mode="nearest")
    return np.clip(audio * gain, -1.0, 1.0).astype(np.float32)


@dataclass
class Message:
    channel: str
    freq_hz: int
    start_sample: int
    audio: np.ndarray
    peak_db: float
    snr_db: float
    norm_gain: float = 1.0
    quality: float = None


class ChannelReceiver:
    """One channel: mix to baseband, filter, demodulate, gate, segment."""

    def __init__(self, spec: ChannelSpec, center_hz: int, chunk_len: int,
                 taps1: np.ndarray, taps2: np.ndarray, taps_audio: np.ndarray,
                 mode: str = "nfm"):
        self.spec = spec
        self.mode = mode
        offset = spec.freq_hz - center_hz

        # The oscillator is precomputed once. With the chunk length and channel
        # offsets both chosen so that offset * CHUNK_SECONDS is a whole number
        # of cycles, the same buffer can be reused every chunk with no phase
        # discontinuity at the seam -- and no per-sample trig at 2 MSPS.
        cycles = offset * CHUNK_SECONDS
        if abs(cycles - round(cycles)) > 1e-9:
            raise ValueError(
                f"{spec.name}: offset {offset} Hz gives {cycles} cycles per "
                f"{CHUNK_SECONDS}s chunk, which is not a whole number. The offset "
                f"must be a multiple of {1/CHUNK_SECONDS:g} Hz. Any center on a "
                f"12.5 kHz grid satisfies this for standard channel plans."
            )
        n = np.arange(chunk_len, dtype=np.float64)
        phase = -2.0 * np.pi * offset / SDR_SAMPLE_RATE * n
        self.osc = np.exp(1j * phase).astype(np.complex64)

        self.dec1 = PolyphaseDecimator(taps1, DECIM_STAGE1)
        self.dec2 = PolyphaseDecimator(taps2, DECIM_STAGE2)
        self.dec_audio = PolyphaseDecimator(taps_audio, 2, dtype=np.float32)
        self.deemph = OnePoleDeemphasis(DEEMPHASIS_TAU, IF_RATE)
        self.demod_state = np.complex64(0)

        # FM only: highpass used as a measurement tap for the noise squelch.
        self.noise_taps = firwin(65, NOISE_BAND_HZ, fs=IF_RATE, pass_zero=False)
        self.noise_zi = np.zeros(len(self.noise_taps) - 1, dtype=np.float64)

        # AM only: asymmetric tracker following the carrier amplitude. Dividing
        # the envelope by it gives modulation depth, which is independent of
        # signal strength -- the AM detector's own AGC.
        self.am_track_len = max(1, int(IF_RATE * AM_TRACK_MS / 1000))
        step = self.am_track_len / IF_RATE
        self.am_att = 1.0 - np.exp(-step / AM_ATTACK_S)
        self.am_rel = 1.0 - np.exp(-step / AM_RELEASE_S)
        self.am_carrier = 1e-3
        self.floor_dbfs = None       # set by the calibrator from references
        self.am_hang_limit = int(AM_HANG_S * 1000 / GATE_FRAME_MS)

        self.frame_len = int(IF_RATE * GATE_FRAME_MS / 1000)      # complex samples
        self.audio_per_frame = self.frame_len // 2

        # Thresholds live on the instance, not as module constants, so the
        # calibrator can retune them from the reference channels at runtime.
        self.open_noise = CARRIER_OPEN_NOISE
        self.close_noise = CARRIER_CLOSE_NOISE
        self.idle_noise = NOMINAL_IDLE_NOISE

        # State machine: "idle" -> "carrier" -> "speech"
        self.state = "idle"
        self.carrier_hang = 0
        self.carrier_hang_limit = int(CARRIER_HANG_S * 1000 / GATE_FRAME_MS)
        self.quiet_frames = 0
        self.quiet_limit = int(SPLIT_SILENCE_S * 1000 / GATE_FRAME_MS)
        # Squelch gain applied to the audio itself. A real squelch mutes
        # whenever the carrier is absent, not merely at the end of a
        # transmission: on a fluttering signal the carrier drops repeatedly and
        # every dropout is a burst of full-scale discriminator noise sitting in
        # the middle of the recording.
        self.mute_gain = 0.0
        self.speech_floor = SPEECH_ABS_THRESHOLD
        # Squelch gain applied to the audio itself. A real squelch mutes
        # whenever the carrier is absent, not merely at the end of a
        # transmission: on a fluttering signal the carrier drops repeatedly and
        # every dropout is a burst of full-scale discriminator noise sitting in
        # the middle of the recording.
        self.mute_gain = 0.0

        pre_s = AM_PREROLL_S if mode == "am" else PREROLL_S
        preroll_frames = int(pre_s * 1000 / GATE_FRAME_MS)
        self.preroll = collections.deque(maxlen=max(1, preroll_frames))
        self.msg_audio = []
        self.msg_start_sample = 0
        self.msg_peak = 0.0
        self.msg_snr_sum = 0.0
        self.msg_frames = 0

        # Populated by demodulate() so --monitor and the calibrator can read
        # live metrics without reaching into the signal path.
        self.last_noise = 1.0
        self.last_speech_db = -99.0
        self.last_power_db = -99.0
        # Diagnostics: the strongest carrier ever seen, how often the gate
        # opened, and how many of those were thrown away for being too short.
        # Together these answer "is anything actually transmitting here?"
        self.peak_carrier = -99.0
        self.opens = 0
        self.short_rejects = 0
        self.weak_rejects = 0
        # Durations of quiet runs seen inside speech, used by --monitor to
        # report what SPLIT_SILENCE_S should actually be for this station.
        self.gaps = collections.deque(maxlen=300)

    # -- signal path ------------------------------------------------------
    def _demod_am(self, base):
        """Envelope detection with an asymmetric carrier-tracking AGC.

        audio = (envelope - carrier) / carrier, so the output is modulation
        depth rather than absolute amplitude. A distant aircraft and a local
        tower come out at comparable level, which is what Whisper wants.

        The tracker runs at 1 ms resolution rather than per sample, so the
        asymmetry costs a short Python loop of a few hundred iterations per
        chunk instead of tens of thousands. The result is interpolated back to
        the sample rate; an AGC has no business changing faster than that
        anyway.
        """
        env = np.abs(base).astype(np.float32)
        n = env.size
        fl = self.am_track_len
        nf = n // fl
        if nf < 1:
            carrier = np.full(n, max(self.am_carrier, 1e-5), dtype=np.float32)
        else:
            # tolist() matters: numpy scalars in a Python loop are roughly
            # twenty times slower than plain floats, and this loop runs a few
            # hundred times per chunk per channel.
            frames = env[:nf * fl].reshape(nf, fl).mean(axis=1).tolist()
            c = float(self.am_carrier)
            att = float(self.am_att)
            rel = float(self.am_rel)
            track = [c]
            append = track.append
            for x in frames:
                c += (att if x > c else rel) * (x - c)
                append(c)
            self.am_carrier = c
            xp = np.concatenate(([-0.5 * fl], (np.arange(nf) + 0.5) * fl))
            carrier = np.interp(np.arange(n), xp,
                                np.asarray(track, dtype=np.float32)).astype(np.float32)

        carrier = np.maximum(carrier, 1e-5)
        if self.floor_dbfs is not None:
            floor_amp = 10.0 ** (self.floor_dbfs / 20.0)
            carrier = np.maximum(carrier, AM_AGC_FLOOR_MULT * floor_amp)
        return ((env - carrier) / carrier).astype(np.float32)

    def _demod_fm(self, base):
        prev = np.concatenate(([self.demod_state], base[:-1]))
        self.demod_state = base[-1]
        disc = np.angle(base * np.conj(prev)).astype(np.float32)
        disc *= IF_RATE / (2.0 * np.pi * FM_MAX_DEVIATION)
        return disc

    def demodulate(self, iq: np.ndarray):
        """Mix, filter, demodulate.

        Returns (noise, speech, audio) where `noise` is the per-frame FM noise
        metric used for carrier detection and `speech` is the per-frame audio
        RMS used for segmentation.
        """
        base = self.dec2(self.dec1(iq * self.osc))
        n_frames = len(base) // self.frame_len

        if self.mode == "am":
            # Reference channels are measurement taps: their audio is never
            # recorded, so there is no reason to run the detector on them.
            disc = (np.zeros(len(base), dtype=np.float32)
                    if self.spec.role == "reference" else self._demod_am(base))
            # Carrier power per frame, in dBFS. This is the AM discriminator.
            b_f = base[: n_frames * self.frame_len].reshape(n_frames, self.frame_len)
            power = np.mean(np.abs(b_f) ** 2, axis=1)
            noise = 10 * np.log10(np.maximum(power, 1e-12))
            audio = self.dec_audio(disc)
        else:
            disc = self._demod_fm(base)
            # FM noise squelch tap: energy above the voice band, taken before
            # de-emphasis, which would tilt the very band being measured.
            hf, self.noise_zi = lfilter(self.noise_taps, [1.0], disc, zi=self.noise_zi)
            hf_f = hf[: n_frames * self.frame_len].reshape(n_frames, self.frame_len)
            noise = np.sqrt(np.mean(hf_f ** 2, axis=1))
            audio = self.dec_audio(self.deemph(disc))
        a_f = audio[: n_frames * self.audio_per_frame].reshape(n_frames, self.audio_per_frame)
        speech = np.sqrt(np.mean(a_f ** 2, axis=1))

        if n_frames:
            self.last_noise = float(np.median(noise))
            if self.mode == "am":
                self.peak_carrier = max(self.peak_carrier, float(np.max(noise)))
            self.last_speech_db = 20 * np.log10(max(float(np.median(speech)), 1e-9))
            # Absolute channel power. The noise metric deliberately ignores
            # amplitude, so it cannot tell a live antenna from a disconnected
            # one -- both produce random phase. Power can.
            self.last_power_db = 10 * np.log10(
                max(float(np.mean(np.abs(base) ** 2)), 1e-12))
        return noise, speech, audio

    def process(self, iq: np.ndarray, sample_index: int):
        """Returns a list of completed Messages (usually empty)."""
        noise, speech, audio = self.demodulate(iq)
        if self.mode == "am":
            return self._gate_am(noise, audio, sample_index)
        return self._gate(noise, speech, audio, sample_index)

    # -- squelch and segmentation ----------------------------------------
    def _am_thresholds(self):
        """Open above the noise floor, but never below the absolute guard."""
        floor = self.floor_dbfs
        if floor is None:
            return AM_SQUELCH_DBFS, AM_SQUELCH_DBFS - 3.0
        return (max(AM_SQUELCH_DBFS, floor + AM_OPEN_MARGIN_DB),
                max(AM_SQUELCH_DBFS - 3.0, floor + AM_CLOSE_MARGIN_DB))

    def _am_ready(self):
        """The gate stays shut until the noise floor has been measured.

        The absolute guard is a backstop against a bad calibration, not a
        usable standalone threshold: it can sit only a couple of dB above the
        real floor, in which case ordinary noise peaks trip it. Better to miss
        the first twenty seconds than to fill the database with noise.
        """
        return self.floor_dbfs is not None

    def _gate_am(self, power_db, audio, sample_index):
        """An AM transmission is exactly one keying of the carrier.

        No speech sub-gate: unlike a continuously-keyed weather station, an
        aircraft or controller keys up, talks, and unkeys, so the carrier
        itself marks the message boundaries.
        """
        done = []
        if not self._am_ready():
            for i in range(len(power_db)):
                self.preroll.append(
                    audio[i * self.audio_per_frame:(i + 1) * self.audio_per_frame])
            return done

        base_sample = sample_index // (DECIM_STAGE1 * DECIM_STAGE2 * 2)
        open_thr, close_thr = self._am_thresholds()

        for i, p in enumerate(power_db):
            a = audio[i * self.audio_per_frame:(i + 1) * self.audio_per_frame]

            if self.state != "speech":
                self.preroll.append(a)
                if p > open_thr:
                    self.state = "speech"
                    self.opens += 1
                    self.quiet_frames = 0
                    self.msg_audio = list(self.preroll)
                    self.preroll.clear()
                    frames_back = len(self.msg_audio)
                    self.msg_start_sample = max(
                        0, base_sample + (i - frames_back) * self.audio_per_frame)
                    self.msg_peak = 0.0
                    self.msg_snr_sum = 0.0
                    self.msg_frames = 0
                continue

            self.msg_audio.append(a)
            self.msg_frames += 1
            self.msg_snr_sum += p - (self.floor_dbfs if self.floor_dbfs is not None
                                     else AM_SQUELCH_DBFS)
            if a.size:
                self.msg_peak = max(self.msg_peak, float(np.max(np.abs(a))))

            if p < close_thr:
                self.quiet_frames += 1
            else:
                self.quiet_frames = 0

            total = sum(x.size for x in self.msg_audio)
            if (self.quiet_frames >= self.am_hang_limit
                    or total >= MAX_MESSAGE_S * AUDIO_RATE):
                msg = self._close(snr_is_db=True)
                if msg:
                    done.append(msg)
                self.state = "idle"
        return done

    def _gate(self, noise, speech, audio, sample_index):
        done = []
        base_sample = sample_index // (DECIM_STAGE1 * DECIM_STAGE2 * 2)
        open_lin = 10 ** (SPEECH_OPEN_MARGIN_DB / 20)
        close_lin = 10 ** (SPEECH_CLOSE_MARGIN_DB / 20)

        for i in range(len(noise)):
            nz = noise[i]
            sp = speech[i]
            a = audio[i * self.audio_per_frame:(i + 1) * self.audio_per_frame]

            # --- carrier detection ---
            if self.state == "idle":
                if nz < self.open_noise:
                    self.state = "carrier"
                    self.carrier_hang = 0
                    self.quiet_frames = 0
                    self.speech_floor = SPEECH_ABS_THRESHOLD
            else:
                if nz > self.close_noise:
                    self.carrier_hang += 1
                else:
                    self.carrier_hang = 0
                if self.carrier_hang >= self.carrier_hang_limit:
                    if self.state == "speech":
                        # Trim back to where the carrier actually went, not to
                        # where the gate decided it had gone.
                        msg = self._close(trim_frames=self.carrier_hang,
                                          tail_keep_s=CARRIER_TAIL_KEEP_S)
                        if msg:
                            done.append(msg)
                    self.state = "idle"
                    self.mute_gain = 0.0
                    self.preroll.clear()
                    continue

            if self.state == "idle":
                self.preroll.append(a)
                continue

            # --- speech gate, only meaningful while a carrier is present ---
            if self.state == "carrier":
                # Track the quiet-carrier level, but never below the absolute
                # threshold and only while not already in speech, so a long
                # transmission cannot drag the reference up and mute itself.
                self.speech_floor = (0.98 * self.speech_floor + 0.02 * sp
                                     if sp < self.speech_floor
                                     else 0.9995 * self.speech_floor + 0.0005 * sp)
                self.speech_floor = max(self.speech_floor, SPEECH_ABS_THRESHOLD)

                self.preroll.append(a)
                if sp > self.speech_floor * open_lin:
                    self.state = "speech"
                    self.quiet_frames = 0
                    self.msg_audio = list(self.preroll)
                    self.preroll.clear()
                    frames_back = len(self.msg_audio)
                    self.msg_start_sample = max(
                        0, base_sample + (i - frames_back) * self.audio_per_frame
                    )
                    self.msg_peak = 0.0
                    self.msg_snr_sum = 0.0
                    self.msg_frames = 0
                continue

            # --- accumulating a message ---
            # Mute across any moment the carrier is not present, ramping over
            # the frame so the transition itself does not click.
            target = 1.0 if nz <= self.close_noise else 0.0
            if target != self.mute_gain or target == 0.0:
                a = a * np.linspace(self.mute_gain, target, a.size,
                                    dtype=np.float32)
            self.mute_gain = target
            self.msg_audio.append(a)
            self.msg_frames += 1
            self.msg_snr_sum += nz
            if a.size:
                self.msg_peak = max(self.msg_peak, float(np.max(np.abs(a))))

            if sp < self.speech_floor * close_lin:
                self.quiet_frames += 1
            else:
                if self.quiet_frames:
                    self.gaps.append(self.quiet_frames * GATE_FRAME_MS / 1000.0)
                self.quiet_frames = 0

            total = sum(x.size for x in self.msg_audio)
            long_enough = total >= TARGET_MESSAGE_S * AUDIO_RATE
            if ((self.quiet_frames >= self.quiet_limit and long_enough)
                    or total >= MAX_MESSAGE_S * AUDIO_RATE):
                msg = self._close()
                if msg:
                    done.append(msg)
                self.state = "carrier"
        return done

    def _close(self, snr_is_db: bool = False, trim_frames: int = None,
               tail_keep_s: float = None):
        # Record the gap that ended this message. Without this the histogram
        # only ever shows gaps that were followed by more speech, so every gap
        # long enough to actually cause a split would be missing from it.
        if trim_frames is None and self.quiet_frames:
            self.gaps.append(self.quiet_frames * GATE_FRAME_MS / 1000.0)

        # The split timer keeps the message open through pauses, so on close
        # the buffer ends with SPLIT_SILENCE_S of quiet. Trim it back to a short
        # tail so the WAV matches the transmission.
        tail_s = AM_TAIL_KEEP_S if self.mode == "am" else TAIL_KEEP_S
        if tail_keep_s is not None:
            tail_s = tail_keep_s
        keep = int(tail_s * 1000 / GATE_FRAME_MS)
        # Which counter to trim by depends on what ended the message. Closing
        # on carrier loss means trimming the carrier hang; closing on a pause
        # in speech means trimming the silence.
        held = self.quiet_frames if trim_frames is None else trim_frames
        trim = max(0, held - keep)
        frames = self.msg_audio[:len(self.msg_audio) - trim] if trim else self.msg_audio
        audio = np.concatenate(frames) if frames else np.array([], np.float32)

        self.quiet_frames = 0
        self.msg_audio = []
        self.preroll.clear()

        if audio.size < MIN_MESSAGE_S * AUDIO_RATE:
            self.short_rejects += 1
            return None

        # Raised-cosine fade so the clip does not begin and end on a step,
        # which reads as a click and which Whisper sometimes hallucinates on.
        n_fade = min(int(FADE_MS * AUDIO_RATE / 1000), audio.size // 2)
        if n_fade > 1:
            w = 0.5 * (1.0 - np.cos(np.linspace(0.0, np.pi, n_fade,
                                                dtype=np.float32)))
            audio[:n_fade] *= w
            audio[-n_fade:] *= w[::-1]

        peak = max(self.msg_peak, 1e-6)
        rms = max(float(np.sqrt(np.mean(audio ** 2))), 1e-6)
        gain = min(TARGET_RMS / rms, 10 ** (MAX_NORM_GAIN_DB / 20))
        pre_limit_peak = float(np.max(np.abs(audio * gain))) if audio.size else 0.0
        audio = (audio * gain).astype(np.float32)

        audio = limit(audio)
        if not self.msg_frames:
            snr = 0.0
        elif snr_is_db:
            # AM: carrier power above the calibrated noise floor. A real
            # carrier-to-noise ratio, left alone.
            snr = self.msg_snr_sum / self.msg_frames
        else:
            # FM: mean discriminator noise, expressed as decibels below the
            # no-carrier reading. 0 dB means the gate opened on something no
            # better than noise; 25 dB is a solid carrier. Not a power ratio,
            # but monotonic with link quality, which the old figure was not.
            mean_nz = max(self.msg_snr_sum / self.msg_frames, 1e-4)
            idle = max(self.idle_noise, 1e-3)
            snr = float(np.clip(20 * np.log10(idle / mean_nz), 0.0, 40.0))
        if MIN_SNR_DB > 0 and snr < MIN_SNR_DB:
            self.weak_rejects += 1
            return None

        return Message(
            channel=self.spec.name,
            freq_hz=self.spec.freq_hz,
            start_sample=self.msg_start_sample,
            audio=audio,
            peak_db=float(pre_limit_peak),
            snr_db=float(snr),
            norm_gain=float(gain),
            quality=quality_score(spectral_flatness(audio)),
        )


class Calibrator:
    """Sets the carrier thresholds from reference channels parked on empty
    frequencies, and watches reference noise power for antenna faults.

    Deliberately conservative. A bad calibration produces a receiver that is
    silently deaf, which is the worst failure this system can have, so any
    reading that does not look like clean noise is rejected and the compiled-in
    defaults are kept.
    """

    def __init__(self, receivers, mode: str = "nfm"):
        self.mode = mode
        self.refs = [rx for rx in receivers if rx.spec.role == "reference"]
        self.targets = [rx for rx in receivers if rx.spec.role != "reference"]
        self.noise_samples = []
        self.power_samples = []
        self.calibrated = False
        self.baseline_power_db = None
        self.idle_noise = NOMINAL_IDLE_NOISE
        self.antenna_warned = False
        self._needed = int(CALIBRATION_SECONDS / CHUNK_SECONDS)

    def update(self):
        if not self.refs:
            return
        self.noise_samples.append(float(np.median([r.last_noise for r in self.refs])))
        self.power_samples.append(float(np.median([r.last_power_db for r in self.refs])))
        if len(self.noise_samples) < self._needed:
            return

        idle = float(np.median(self.noise_samples))
        power = float(np.median(self.power_samples))
        self.noise_samples.clear()
        self.power_samples.clear()

        if self.mode == "am":
            # In AM the metric is already the noise floor in dBFS, so it is
            # published directly to the receivers rather than converted into a
            # ratio the way the FM noise metric is.
            for rx in self.targets:
                rx.floor_dbfs = idle
            if not self.calibrated:
                self.calibrated = True
                self.baseline_power_db = power
                self.idle_noise = idle
                open_thr, close_thr = self.targets[0]._am_thresholds() \
                    if self.targets else (AM_SQUELCH_DBFS, AM_SQUELCH_DBFS)
                log.info("AM squelch calibrated: noise floor %.1f dBFS -> open "
                         "above %.1f dBFS (absolute guard %.1f)",
                         idle, open_thr, AM_SQUELCH_DBFS)
            else:
                self.idle_noise = idle
                self._check_antenna(power)
            return

        lo, hi = REF_NOISE_SANE
        if not (lo <= idle <= hi):
            log.warning(
                "reference channels read %.2f, outside the sane range %.2f-%.2f; "
                "keeping default thresholds. Something may be transmitting on a "
                "reference frequency, or the front end is misbehaving.",
                idle, lo, hi)
            return

        self.idle_noise = idle
        open_thr = idle * OPEN_FRACTION
        close_thr = idle * CLOSE_FRACTION
        for rx in self.targets:
            rx.open_noise = open_thr
            rx.close_noise = close_thr
            rx.idle_noise = idle

        if not self.calibrated:
            self.calibrated = True
            self.baseline_power_db = power
            log.info("squelch calibrated from reference: idle noise %.2f -> "
                     "open < %.2f, close > %.2f (reference power %.1f dB)",
                     idle, open_thr, close_thr, power)
        else:
            self._check_antenna(power)

    def _check_antenna(self, power: float):
        delta = power - self.baseline_power_db
        if abs(delta) >= ANTENNA_ALARM_DB:
            if not self.antenna_warned:
                direction = "dropped" if delta < 0 else "risen"
                log.warning(
                    "reference noise power has %s %.1f dB since startup "
                    "(%.1f -> %.1f dB). A drop usually means the antenna or a "
                    "connector has come loose; a rise means new interference.",
                    direction, abs(delta), self.baseline_power_db, power)
                self.antenna_warned = True
        elif self.antenna_warned:
            log.info("reference noise power back to normal (%.1f dB)", power)
            self.antenna_warned = False


class Channelizer:
    def __init__(self, center_hz: int, channels, mode: str = "nfm"):
        self.mode = mode
        chunk_len = int(SDR_SAMPLE_RATE * CHUNK_SECONDS)
        if chunk_len % (DECIM_STAGE1 * DECIM_STAGE2 * 2):
            raise ValueError("CHUNK_SECONDS does not divide evenly through the chain")

        # Stage 1 only has to keep energy from folding into the final +/-8 kHz,
        # so a wide, cheap filter is enough. Stage 2 does the real channel
        # selection and is what rejects the adjacent channel 25 kHz away.
        taps1 = firwin(DECIM_STAGE1 * 8 + 1, 50_000, fs=SDR_SAMPLE_RATE)
        # Airband AM occupies about 6 kHz of a 25 kHz channel; narrowband FM
        # needs roughly twice that. Using the FM width on AM would let the
        # adjacent channel through.
        # RF bandwidth follows the audio band: an AM channel needs one audio
        # bandwidth either side of the carrier, plus a little margin.
        chan_bw = (AM_AUDIO_HI + 600) if mode == "am" else 8_500
        taps2 = firwin(DECIM_STAGE2 * 64 + 1, chan_bw, fs=SDR_SAMPLE_RATE // DECIM_STAGE1)
        # ATC voice is fully intelligible inside 300-2700 Hz, and the top
        # octave is where most of the audible hiss lives. Narrower band, less
        # noise, no loss of words.
        audio_band = [300, AM_AUDIO_HI if mode == "am" else FM_AUDIO_HI]
        taps_audio = firwin(129, audio_band, fs=IF_RATE, pass_zero=False)

        self.receivers = [
            ChannelReceiver(c, center_hz, chunk_len, taps1, taps2, taps_audio, mode)
            for c in channels
        ]
        self.chunk_len = chunk_len
        self.calibrator = Calibrator(self.receivers, mode)
        if mode == "am" and not self.calibrator.refs:
            log.warning("preset has no reference channels; AM squelch will use "
                        "the fixed %.1f dBFS threshold with no floor tracking",
                        AM_SQUELCH_DBFS)
            for rx in self.receivers:
                rx.floor_dbfs = AM_SQUELCH_DBFS - AM_OPEN_MARGIN_DB

    def process(self, iq: np.ndarray, sample_index: int):
        out = []
        for rx in self.receivers:
            msgs = rx.process(iq, sample_index)
            # Reference channels are measurement taps only. If one ever
            # produces a message, that frequency is not empty and the whole
            # calibration is suspect -- so drop the message and say so.
            if rx.spec.role == "reference":
                if msgs:
                    log.warning("reference channel %s detected a signal; "
                                "pick a quieter frequency", rx.spec.name)
                continue
            out.extend(msgs)
        self.calibrator.update()
        return out


# ============================== Storage ==============================

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    channel      TEXT    NOT NULL,
    freq_hz      INTEGER NOT NULL,
    started_at   TEXT    NOT NULL,
    duration_s   REAL    NOT NULL,
    snr_db       REAL,
    audio_path   TEXT    NOT NULL,
    transcript   TEXT,
    status       TEXT    NOT NULL DEFAULT 'pending',
    avg_logprob  REAL,
    no_speech    REAL,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_started  ON messages(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_channel  ON messages(channel, started_at DESC);
"""


# Columns added after the first release. Applied with ALTER TABLE on every
# start so an existing database keeps working rather than needing a rebuild.
MIGRATIONS = [
    ("preset",          "TEXT"),    # which preset captured it
    ("noise_floor_db",  "REAL"),    # measured floor at the time
    ("open_thresh",     "REAL"),    # squelch threshold then in force
    ("gain_db",         "TEXT"),    # tuner gain
    ("peak_pre_limit",  "REAL"),    # before normalisation and limiting
    ("norm_gain",       "REAL"),    # what normalisation applied
    ("raw_transcript",  "TEXT"),    # what Whisper said before filtering
    ("reject_reason",   "TEXT"),    # why the text was discarded, if it was
    ("rtf",             "REAL"),    # decode time over audio duration
    ("quality",         "REAL"),    # 0-100 readability, from spectral flatness
]


class Store:
    """SQLite is opened per-thread; sharing a connection across threads is the
    classic source of 'objects created in a thread can only be used in that
    same thread' at 3am."""

    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        with self._conn() as c:
            c.executescript(SCHEMA)
            have = {r[1] for r in c.execute("PRAGMA table_info(messages)")}
            for name, kind in MIGRATIONS:
                if name not in have:
                    c.execute(f"ALTER TABLE messages ADD COLUMN {name} {kind}")

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def insert(self, msg: Message, started_at: datetime, audio_path: str,
               ctx: dict = None) -> int:
        ctx = ctx or {}
        c = self._conn()
        with c:
            cur = c.execute(
                "INSERT INTO messages (channel, freq_hz, started_at, duration_s,"
                " snr_db, audio_path, status, created_at, preset, noise_floor_db,"
                " open_thresh, gain_db, peak_pre_limit, norm_gain, quality)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (msg.channel, msg.freq_hz, started_at.isoformat(),
                 msg.audio.size / AUDIO_RATE, msg.snr_db, audio_path,
                 "pending", datetime.now(timezone.utc).isoformat(),
                 ctx.get("preset"), ctx.get("noise_floor_db"),
                 ctx.get("open_thresh"), SDR_GAIN,
                 msg.peak_db, msg.norm_gain, msg.quality),
            )
        return cur.lastrowid

    def set_status(self, row_id: int, status: str):
        c = self._conn()
        with c:
            c.execute("UPDATE messages SET status=? WHERE id=?", (status, row_id))

    def update_transcript(self, row_id: int, text: str, status: str,
                          avg_logprob, no_speech, raw=None, reason=None, rtf=None):
        c = self._conn()
        with c:
            c.execute(
                "UPDATE messages SET transcript=?, status=?, avg_logprob=?,"
                " no_speech=?, raw_transcript=?, reject_reason=?, rtf=? WHERE id=?",
                (text, status, avg_logprob, no_speech, raw, reason, rtf, row_id),
            )

    # Retention is by value, not purely by age. With a fixed number of slots,
    # deleting strictly oldest-first lets a burst of noise-triggered junk push
    # out messages worth reading. Ranking by transcription outcome first and
    # recency second means the junk is evicted before anything useful, so a
    # weak message can be kept rather than discarded at capture time.
    KEEP_RANK = ("CASE status "
                 "WHEN 'empty' THEN 0 "      # Whisper found no speech
                 "WHEN 'error' THEN 0 "
                 "WHEN 'low' THEN 1 "        # transcribed, low confidence
                 "WHEN 'skipped' THEN 1 "    # diagnostic channel
                 "WHEN 'backlog' THEN 2 "    # audio kept, never transcribed
                 "ELSE 3 END")               # 'ok' and 'pending'

    def reconcile(self):
        """Drop rows whose recording has gone.

        Required whenever the audio lives on tmpfs: after a reboot the database
        survives and the WAVs do not, so without this the log would list
        messages that cannot be played. Also catches a file deleted by hand.
        """
        c = self._conn()
        rows = c.execute("SELECT id, audio_path FROM messages").fetchall()
        missing = [(r[0],) for r in rows
                   if not r[1] or not os.path.exists(r[1])]
        if missing:
            with c:
                c.executemany("DELETE FROM messages WHERE id=?", missing)
            log.info("removed %d row(s) whose audio is gone (expected after a "
                     "reboot if the recordings are on tmpfs)", len(missing))
        return len(missing)

    def prune(self, keep: int = MAX_MESSAGES):
        c = self._conn()
        rows = c.execute(
            f"SELECT id, audio_path FROM messages "
            f"ORDER BY {self.KEEP_RANK} DESC, id DESC LIMIT -1 OFFSET ?",
            (keep,),
        ).fetchall()
        for row_id, path in rows:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except OSError as e:
                log.warning("could not remove %s: %s", path, e)
        if rows:
            with c:
                c.executemany("DELETE FROM messages WHERE id=?",
                              [(r[0],) for r in rows])


# ============================== Transcription ==============================

class NewestFirstQueue:
    """Transcription queue that serves the most recent message first.

    A plain FIFO is the wrong shape for a live watch. When transcription falls
    behind, FIFO means the newest message waits behind every stale one, so the
    top of the log shows "transcribing" while two-minute-old traffic is decoded
    first. That is exactly backwards for someone glancing at the screen.

    Serving newest-first keeps the top of the list current. Old entries are
    evicted rather than queued forever, and the caller marks them so the page
    stops claiming they are still being worked on. Their audio is untouched.
    """

    def __init__(self, maxsize):
        self.maxsize = maxsize
        self._items = collections.deque()
        self._cv = threading.Condition()
        self._closed = False

    def put(self, item):
        """Returns the items evicted to make room, oldest first."""
        with self._cv:
            self._items.append(item)
            evicted = []
            while len(self._items) > self.maxsize:
                evicted.append(self._items.popleft())
            self._cv.notify()
            return evicted

    def get(self):
        with self._cv:
            while not self._items and not self._closed:
                self._cv.wait()
            if self._items:
                return self._items.pop()      # newest
            return None

    def qsize(self):
        with self._cv:
            return len(self._items)

    def close(self):
        with self._cv:
            self._closed = True
            self._cv.notify_all()



class Transcriber(threading.Thread):
    """Consumes row ids, reads the WAV back off disk, updates the row.

    Deliberately decoupled from the audio path: the recording and the database
    row already exist by the time anything gets here, so a slow or failed
    transcription degrades to '[no transcript -- tap to listen]' rather than a
    missing message.
    """

    def __init__(self, store: Store, jobs, prompt=None):
        super().__init__(daemon=True, name="transcriber")
        self.store = store
        self.jobs = jobs
        self.prompt = prompt
        self.model = None
        self._stop = threading.Event()
        self._last_thermal_log = 0.0

    def _wait_if_hot(self):
        """Hold off starting a decode while the SoC is above the limit.

        Transcription is the hottest thing this program does, and on a Pi with
        a HAT fitted it will sit at the soft limit indefinitely. Waiting costs
        latency, which is nearly free here -- traffic is sparse and the log is
        read after the fact -- and avoids throttling, which slows the DSP too.

        The queue is newest-first, so anything that piles up during a pause is
        the oldest and least interesting.
        """
        if THERMAL_PAUSE_ABOVE <= 0:
            return 0.0
        t = soc_temperature()
        if t is None or t < THERMAL_PAUSE_ABOVE:
            return 0.0
        resume = THERMAL_RESUME_BELOW or (THERMAL_PAUSE_ABOVE - 4.0)
        start = time.time()
        while time.time() - start < THERMAL_MAX_WAIT_S and not self._stop.is_set():
            self._stop.wait(2.0)
            t = soc_temperature()
            if t is None or t <= resume:
                break
        waited = time.time() - start
        if waited > 1.0 and time.time() - self._last_thermal_log > 60:
            log.info("held transcription %.0fs at %.1f C to stay under the "
                     "thermal limit", waited, t if t is not None else -1)
            self._last_thermal_log = time.time()
        return waited

    def _load(self):
        # CTranslate2 honours cpu_threads for its own pool, but OpenMP and
        # OpenBLAS underneath will each start one thread per core unless told
        # otherwise, so the process ends up oversubscribed and fighting the
        # channelizer. These must be set before the import.
        os.environ.setdefault("OMP_NUM_THREADS", str(CPU_THREADS))
        os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
        os.environ.setdefault("MKL_NUM_THREADS", "1")
        from faster_whisper import WhisperModel
        log.info("loading faster-whisper %s (int8, %d threads)", MODEL_SIZE, CPU_THREADS)
        t0 = time.time()
        kw = dict(device="cpu", compute_type="int8", cpu_threads=CPU_THREADS)
        try:
            # Prefer the cached copy. By default faster-whisper contacts
            # Hugging Face on every start to check the model revision, which on
            # a boat with no connectivity means a stall or a failure at the one
            # moment the system needs to come up.
            self.model = WhisperModel(MODEL_SIZE, local_files_only=True, **kw)
            log.info("model ready in %.1fs (from local cache)", time.time() - t0)
        except Exception:
            log.info("model not cached; downloading once")
            self.model = WhisperModel(MODEL_SIZE, **kw)
            log.info("model ready in %.1fs (downloaded)", time.time() - t0)
        self.suppress = self._annotation_tokens()

    # Characters that only ever appear in subtitle annotations, censored
    # expletives or music marks -- never in a radio transmission.
    ANNOTATION_CHARS = set("[](){}<>*\u266a\u266b\u2669\u266c")

    def _annotation_tokens(self):
        """Every token whose text contains an annotation character.

        Whisper has no "do not transcribe sound effects" option, so suppressing
        the tokens is the only way to stop it at the decoder. Encoding a few
        sample strings is not enough: byte-level BPE merges characters, so "*",
        "**", "***" and "F***" are all different token ids. Scanning the whole
        vocabulary catches them regardless of how they happen to merge.

        Best effort -- tokenizer internals move between versions, and if this
        fails the post-filter still removes the text afterwards.
        """
        ids = {-1}
        try:
            tok = self.model.hf_tokenizer
            vocab = tok.get_vocab()
            for text, tid in vocab.items():
                if any(c in self.ANNOTATION_CHARS for c in text):
                    ids.add(tid)
            log.info("suppressing %d annotation token(s) in the decoder "
                     "(scanned %d)", len(ids) - 1, len(vocab))
        except Exception as e:
            log.debug("could not resolve annotation tokens: %s", e)
        return sorted(ids)

    def run(self):
        # Linux nice values are per-thread. Dropping this thread's priority
        # means the channelizer, which cannot fall behind without permanently
        # losing audio, always wins the CPU. Transcription simply runs late,
        # which the backlog handling already copes with.
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
            log.info("transcriber thread niced to +10; the DSP takes priority")
        except (AttributeError, OSError, PermissionError) as e:
            log.debug("could not renice transcriber thread: %s", e)

        try:
            self._load()
        except Exception:
            log.exception("failed to load Whisper; audio will still be recorded")
            return
        while not self._stop.is_set():
            item = self.jobs.get()
            if item is None:
                break
            row_id, path = item
            try:
                self._wait_if_hot()
                self._transcribe(row_id, path)
            except Exception:
                log.exception("transcription failed for row %s", row_id)
                self.store.set_status(row_id, "error")

    def _transcribe(self, row_id: int, path: str):
        try:
            return self._run(row_id, path)
        except TypeError as e:
            # Older faster-whisper builds lack max_new_tokens. Fall back rather
            # than fail, and say so once so the missing protection is visible.
            if "max_new_tokens" not in str(e):
                raise
            if not getattr(self, "_warned_tokens", False):
                log.warning("this faster-whisper has no max_new_tokens; decoder "
                            "output is unbounded and a loop on noise will be "
                            "expensive. Upgrade faster-whisper if the log shows "
                            "large rtf values.")
                self._warned_tokens = True
            self._no_cap = True
            return self._run(row_id, path)

    def _run(self, row_id: int, path: str):
        audio, sr = sf.read(path, dtype="float32")
        t0 = time.time()
        # Bound the output to what the clip could plausibly contain. This is
        # what stops a runaway loop costing minutes on a four-second recording.
        cap = int(len(audio) / sr * MAX_TOKENS_PER_SECOND) + 16
        extra = {} if getattr(self, "_no_cap", False) else {"max_new_tokens": cap}
        segments, _info = self.model.transcribe(
            audio,
            **extra,
            beam_size=BEAM_SIZE,
            temperature=WHISPER_TEMPERATURES,
            compression_ratio_threshold=COMPRESSION_RATIO_THRESHOLD,
            log_prob_threshold=LOG_PROB_THRESHOLD,
            no_speech_threshold=NO_SPEECH_THRESHOLD,
            no_repeat_ngram_size=4,
            condition_on_previous_text=False,
            initial_prompt=self.prompt,
            suppress_tokens=getattr(self, "suppress", [-1]),
            vad_filter=VAD_FILTER,
            vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=300),
        )
        segs = list(segments)
        raw = " ".join(s.text.strip() for s in segs).strip()
        text = strip_annotations(raw)
        # Test the pattern, not string inequality: strip_annotations also trims
        # trailing punctuation, which would otherwise be reported as an
        # annotation removal and could trip the short-remnant rule below.
        had_annotation = bool(ANNOTATION_RE.search(raw)) or (
            len(re.findall(r"[A-Za-z']+", text)) <
            len(re.findall(r"[A-Za-z']+", raw)))
        if had_annotation:
            log.info("row %d: removed sound-effect annotation from %r",
                     row_id, raw[:60])

        if segs:
            avg_logprob = float(np.mean([s.avg_logprob for s in segs]))
            no_speech = float(np.mean([s.no_speech_prob for s in segs]))
        else:
            avg_logprob, no_speech = None, None

        # A transcript that was nothing but annotation, or is left with a
        # single stray word after stripping, carried no speech.
        reason = "sound-effect annotation removed" if had_annotation else None
        if had_annotation and len(re.findall(r"[a-z']+", text.lower())) < 2:
            text, reason = "", "nothing but a sound-effect annotation"

        rtf = (time.time() - t0) / max(len(audio) / sr, 1e-6)

        # Independent evidence about the recording, used below to stop the
        # text-pattern filters discarding a transmission that was plainly
        # audible. Costs about 0.1% of real time.
        readable = quality_score(spectral_flatness(audio, sr))
        readable_ok = readable is not None and readable >= KEEP_ABOVE_QUALITY

        # Default from the decoder's own confidence. Branches below override it
        # as needed; any branch that does not falls back to this. The previous
        # arrangement put this at the END of the chain, so a branch that kept
        # the text without setting status left the variable unassigned and
        # crashed -- which is exactly what the subtitle-stripping branch did.
        status = "low" if (
            (avg_logprob is not None and avg_logprob < LOW_CONF_AVG_LOGPROB)
            or (no_speech is not None and no_speech > LOW_CONF_NO_SPEECH)
        ) else "ok"

        normalized = text.lower().strip(" .!?\u2026")
        if not text:
            status, reason = "empty", reason or (
                "decoder returned nothing" if not raw else reason)
        elif SUBTITLE_CORPUS.search(text):
            rest, cut = strip_subtitle_corpus(text)
            # Two words is enough: "Roger, standing by" is a complete and
            # useful transmission, and radio traffic is terse by nature.
            if cut and len(re.findall(r"[a-z0-9']+", rest.lower())) >= 2:
                log.info("row %d: removed subtitle boilerplate, keeping %r",
                         row_id, rest[:70])
                reason = "subtitle boilerplate removed from the end"
                text = rest
            else:
                log.info("row %d discarded as subtitle boilerplate: %r",
                         row_id, text[:70])
                status, text, reason = "empty", "", "subtitle-corpus hallucination"
        elif normalized in HALLUCINATION_BLOCKLIST:
            status, text, reason = "empty", "", "known hallucination phrase"
        elif numeric_with_repeats(text) and collapse_single_token(text) is None:
            log.info("row %d: numbers with repeats, flagged unclear: %r",
                     row_id, text[:50])
            status = "low"
            reason = "repeated numbers -- which was said is not recoverable"
        elif looks_like_loop(text):
            single = collapse_single_token(text)
            if single is not None:
                log.info("row %d: one token repeated, keeping %r from %r",
                         row_id, single, text[:50])
                status = "low"
                reason = f"single token repeated in {text[:40]!r}"
                text = single
                trimmed, cut = None, False
            else:
                trimmed, cut = trim_trailing_loop(text)
                if not cut:
                    # Exact repetition found nothing; try the drifting kind,
                    # where only the opening of each sentence repeats.
                    trimmed, cut = trim_repetitive_tail(text)
            kept = len(re.findall(r"[a-z0-9']+", (trimmed or "").lower()))
            if single is not None:
                pass                      # already handled above
            # No second looks_like_loop() test on the trimmed text. It was
            # rejecting salvaged transmissions for repetition that is real:
            # a USCG broadcast identifies its station several times over, so
            # "Coast Guard sector Los Angeles" three times is how the message
            # was actually sent. The trimmer returns "" when the whole
            # transcript was the loop, which is what separates that from
            # "Gun, gun, gun" -- so a surviving head of two or more words is
            # content worth keeping, marked unclear.
            elif cut and kept >= 2:
                log.info("row %d: trimmed a trailing loop, keeping %r",
                         row_id, trimmed[:60])
                status = "low"
                reason = f"trailing repetition removed from {text[:40]!r}"
                text = trimmed
            elif (readable_ok and not (cut and not trimmed)
                  and len(re.findall(r"[a-z0-9']+", text.lower())) >= 2):
                # The repetition is real: broadcasts identify their station
                # several times over, which is correct radio procedure, and
                # this recording is clean enough that the words are almost
                # certainly what was sent. Excluded when the trimmer reported
                # the WHOLE transcript was one repeated phrase -- that is a
                # loop however clean the audio.
                log.info("row %d kept despite repetition, readability %.0f%%: %r",
                         row_id, readable, text[:60])
                status = "low"
                reason = f"repetition kept, audio readable at {readable:.0f}%"
            else:
                log.info("row %d discarded as a decoder loop: %r",
                         row_id, text[:60])
                status, text, reason = "empty", "", "decoder repetition loop"

        if text and DISTRESS_RE.search(text):
            log.warning("row %d on %s contains distress vocabulary -- LISTEN to "
                        "the recording, do not rely on the transcript: %r",
                        row_id, os.path.basename(path), text[:100])

        rtf = (time.time() - t0) / max(len(audio) / sr, 1e-6)
        backlog = self.jobs.qsize()
        tag = f" backlog={backlog}" if backlog else ""
        log.info("row %d [%s] rtf=%.2f%s %s", row_id, status, rtf, tag,
                 text[:100] if text else "(no speech)")
        self.store.update_transcript(row_id, text, status, avg_logprob, no_speech,
                                     raw=raw or None, reason=reason, rtf=rtf)

    def stop(self):
        self._stop.set()
        self.jobs.close()


# ============================== Main loop ==============================

class Watchkeeper:
    def __init__(self, preset_name: str, monitor: bool = False,
                 record_path: str = None, replay_path: str = None):
        preset = PRESETS[preset_name]
        apply_preset_settings(preset)
        validate_preset(preset_name, preset)
        self.monitor = monitor
        self.record_path = record_path
        self.replay_path = replay_path
        self.monitor_interval = 5.0
        self._floor_db = None      # measured noise floor, for the record
        self._open_thresh = None   # squelch threshold in force
        self.preset_name = preset_name
        self.center_hz = preset["center_hz"]
        self.channels = preset["channels"]
        self.mode = preset.get("mode", "nfm")
        self.voice_channels = {c.name for c in self.channels if c.voice}

        os.makedirs(AUDIO_DIR, exist_ok=True)
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        self.store = Store(DB_PATH)
        self.store.reconcile()

        if HALLUCINATION_LIST_PATH:
            load_hallucination_list(HALLUCINATION_LIST_PATH)

        fs = filesystem_of(AUDIO_DIR)
        note = ("recordings are in RAM and will not survive a reboot"
                if fs == "tmpfs" else
                "recordings are on persistent storage")
        log.info("audio: %s (%s) -- %s", AUDIO_DIR, fs, note)
        if fs != "tmpfs" and "tmpfs" in open("/etc/fstab").read() \
                and AUDIO_DIR in open("/etc/fstab").read():
            log.warning("fstab lists a tmpfs mount for %s but it is not "
                        "mounted; writes are going to the card instead",
                        AUDIO_DIR)
        self.jobs = NewestFirstQueue(TRANSCRIBE_BACKLOG)
        self.transcriber = Transcriber(self.store, self.jobs, preset.get("prompt"))
        if not preset.get("prompt"):
            log.info("no decoder prompt for this preset: nothing is being "
                     "suggested to the model")
        self.running = threading.Event()
        self.running.set()

    def run(self):
        if not self.monitor:
            self.transcriber.start()
        backoff = 1.0
        while self.running.is_set():
            try:
                ok = self._session()
                backoff = 1.0 if ok else min(backoff * 2, 60.0)
            except Exception:
                log.exception("session crashed")
                backoff = min(backoff * 2, 60.0)
            if not self.running.is_set():
                break
            log.warning("restarting capture in %.0fs", backoff)
            time.sleep(backoff)
        if not self.monitor:
            # Let the in-flight decode finish rather than tearing the thread out
            # from under CTranslate2, which aborts with "terminate called
            # without an active exception" on the way down.
            self.transcriber.stop()
            self.transcriber.join(timeout=20)
            if self.transcriber.is_alive():
                log.warning("transcriber still busy at shutdown; exiting anyway")

    def _session(self) -> bool:
        if self.replay_path:
            source = FileSource(self.replay_path)
            if source.center_hz and source.center_hz != self.center_hz:
                log.warning("capture was recorded at %.4f MHz but preset '%s' "
                            "expects %.4f MHz; channel offsets will be wrong",
                            source.center_hz / 1e6, self.preset_name,
                            self.center_hz / 1e6)
        else:
            source = SdrSource(self.center_hz, record_path=self.record_path)
        chan = Channelizer(self.center_hz, self.channels, self.mode)
        source.start()

        stream_start = datetime.now(timezone.utc)
        sample_index = 0
        ran_clean = False
        last_prune = time.time()
        last_monitor = 0.0

        log.info("watching %s (%s): %s", self.preset_name, self.mode.upper(),
                 ", ".join(f"{c.name}@{c.freq_hz/1e6:.3f}" for c in self.channels))
        if self.monitor:
            if self.mode == "am":
                log.info("monitor mode: nothing is recorded. Squelch opens "
                         "%.0f dB above the measured noise floor, never below "
                         "%.1f dBFS. Read the peak column in the summary at "
                         "exit: that says whether anything transmitted.",
                         AM_OPEN_MARGIN_DB, AM_SQUELCH_DBFS)
            else:
                log.info("monitor mode: nothing is recorded. noise < %.2f opens "
                         "the carrier gate, > %.2f closes it.",
                         CARRIER_OPEN_NOISE, CARRIER_CLOSE_NOISE)
        try:
            while self.running.is_set():
                raw = source.q.get()
                if raw == b"":
                    break
                iq = u8_to_complex(raw)
                for msg in chan.process(iq, sample_index):
                    if not self.monitor:
                        self._emit(msg, stream_start, source.dropped_chunks)
                    else:
                        log.info("  would record %s %.1fs snr=%.1fdB",
                                 msg.channel, msg.audio.size / AUDIO_RATE, msg.snr_db)
                sample_index += len(iq)
                ran_clean = True

                if self.monitor and time.time() - last_monitor >= self.monitor_interval:
                    last_monitor = time.time()
                    cal = chan.calibrator
                    if not cal.calibrated:
                        tag = "calibrating"
                    elif self.mode == "am":
                        o, c = chan.receivers[0]._am_thresholds()
                        tag = f"floor={cal.idle_noise:.1f}dBFS open>{o:.1f} close<{c:.1f}"
                    else:
                        tag = (f"idle={cal.idle_noise:.2f} "
                               f"open<{chan.receivers[0].open_noise:.2f}")
                    log.info("[%s]  split threshold %.2fs", tag, SPLIT_SILENCE_S)
                    for rx in chan.receivers:
                        if rx.gaps:
                            g = np.array(rx.gaps)
                            gap_txt = (f"  gaps p50={np.median(g):.2f}s "
                                       f"p90={np.percentile(g, 90):.2f}s "
                                       f"max={g.max():.2f}s n={len(g)}")
                        else:
                            gap_txt = ""
                        if self.mode == "am":
                            metric = ("carrier=%6.1f peak=%6.1f dBFS  "
                                      "opens=%-3d short=%-3d weak=%-3d"
                                      % (rx.last_noise, rx.peak_carrier,
                                         rx.opens, rx.short_rejects,
                                         rx.weak_rejects))
                        else:
                            metric = "noise=%.2f" % rx.last_noise
                        log.info("    %-24s %s  audio=%6.1f dB  "
                                 "power=%6.1f dB  %-7s%s%s",
                                 rx.spec.name, metric, rx.last_speech_db,
                                 rx.last_power_db, rx.state,
                                 "  (reference)" if rx.spec.role == "reference" else "",
                                 gap_txt)

                cal = chan.calibrator
                if cal.calibrated and chan.receivers:
                    rx = chan.receivers[0]
                    if self.mode == "am":
                        self._floor_db = cal.idle_noise
                        self._open_thresh = rx._am_thresholds()[0]
                    else:
                        self._floor_db = cal.baseline_power_db
                        self._open_thresh = rx.open_noise

                if not self.monitor and time.time() - last_prune > 60:
                    self.store.prune()
                    last_prune = time.time()
        finally:
            source.stop()
            if self.monitor:
                self._monitor_summary(chan, sample_index)
        if self.replay_path:
            log.info("replay finished")
            self.running.clear()
        return ran_clean

    def _monitor_summary(self, chan, sample_index):
        """Compact end-of-run report. This is the thing worth reading: the peak
        column says whether anything ever transmitted, and opens vs short says
        whether the gate caught it or chopped it up."""
        secs = sample_index / SDR_SAMPLE_RATE
        cal = chan.calibrator
        log.info("")
        log.info("=== monitor summary: %s (%s), %.0f s ===",
                 self.preset_name, self.mode.upper(), secs)
        if cal.calibrated:
            log.info("noise floor %.1f dBFS from %d reference channel(s)",
                     cal.idle_noise, len(cal.refs))
        else:
            log.info("calibration never completed")
        if self.mode == "am" and chan.receivers:
            o, c = chan.receivers[0]._am_thresholds()
            log.info("squelch: open above %.1f dBFS, close below %.1f dBFS "
                     "(absolute guard %.1f)", o, c, AM_SQUELCH_DBFS)
        if MIN_SNR_DB > 0:
            log.info("discarding messages below %.0f dB mean SNR", MIN_SNR_DB)
        log.info("%-26s %8s %8s %7s %7s %7s", "channel", "median", "peak",
                 "opens", "short", "weak")
        for rx in chan.receivers:
            tag = "  (reference)" if rx.spec.role == "reference" else ""
            log.info("%-26s %8.1f %8.1f %7d %7d %7d%s", rx.spec.name,
                     rx.last_noise, rx.peak_carrier, rx.opens,
                     rx.short_rejects, rx.weak_rejects, tag)
        log.info("")

    def _emit(self, msg: Message, stream_start: datetime, dropped: int):
        offset = timedelta(seconds=msg.start_sample / AUDIO_RATE
                           + dropped * CHUNK_SECONDS)
        started_at = stream_start + offset
        stamp = started_at.astimezone().strftime("%Y%m%d_%H%M%S")
        slug = re.sub(r"[^A-Za-z0-9]+", "_", msg.channel).strip("_")
        fname = f"{stamp}_{slug}_{int(time.time()*1000) % 1000:03d}.wav"
        path = os.path.join(AUDIO_DIR, fname)

        sf.write(path, msg.audio, AUDIO_RATE, subtype="PCM_16")
        row_id = self.store.insert(msg, started_at, path, ctx={
            "preset": self.preset_name,
            "noise_floor_db": self._floor_db,
            "open_thresh": self._open_thresh,
        })

        dur = msg.audio.size / AUDIO_RATE
        log.info("%-24s %5.1fs snr=%4.1fdB -> row %d",
                 msg.channel, dur, msg.snr_db, row_id)

        if msg.channel not in self.voice_channels:
            self.store.set_status(row_id, "skipped")
            return
        for old_id, _ in self.jobs.put((row_id, path)):
            # Evicted because newer traffic arrived. The recording is intact;
            # only the transcript is skipped.
            self.store.set_status(old_id, "backlog")
            log.info("row %d dropped from the transcription queue (audio kept)",
                     old_id)

    def shutdown(self, *_):
        log.info("shutting down")
        self.running.clear()


def main():
    ap = argparse.ArgumentParser(
        description="Multi-channel SDR watchkeeper (AM airband / NFM marine)")
    ap.add_argument("--config", metavar="PATH",
                    help="channel configuration file. Default: the first of "
                         + ", ".join(CONFIG_SEARCH) + " that exists.")
    ap.add_argument("--preset", help="which preset in the config file to run")
    ap.add_argument("--list-presets", action="store_true",
                    help="show the presets in the config file and exit")
    ap.add_argument("--record", metavar="PATH",
                    help="also write the raw I/Q stream to PATH while running. "
                         "Capture real traffic once, then replay it at a desk "
                         "as many times as you like while tuning.")
    ap.add_argument("--replay", metavar="PATH",
                    help="read from a recorded I/Q file instead of the dongle. "
                         "Runs as fast as the DSP allows, not in real time.")
    ap.add_argument("--monitor", action="store_true",
                    help="print live squelch metrics instead of recording; "
                         "use this to calibrate the thresholds against real RF")
    ap.add_argument("--am-audio-hz", type=int, metavar="HZ",
                    help="top of the AM audio passband, default %d. Lower "
                         "values sound dramatically less hissy but do not "
                         "improve intelligibility, because the noise is flat "
                         "with frequency. 1500 approximates gqrx's AM narrow "
                         "filter; 2400 keeps more consonant detail."
                         % AM_AUDIO_HI)
    ap.add_argument("--gain", metavar="DB",
                    help="tuner gain in dB, overriding the built-in %s. Use "
                         "'0' for the tuner's own AGC. Lower this if a strong "
                         "in-band signal is close to full scale: an overloaded "
                         "front end raises the noise floor on every channel."
                         % SDR_GAIN)
    ap.add_argument("--log", metavar="PATH",
                    help="also append all output to PATH")
    ap.add_argument("--monitor-interval", type=float, default=5.0,
                    help="seconds between monitor reports (default 5). Peaks "
                         "are tracked continuously regardless, so a slower "
                         "cadence loses nothing.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cfg_path = find_config(args.config)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        stream=sys.stdout,
    )
    if args.log:
        fh = logging.FileHandler(args.log)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        logging.getLogger().addHandler(fh)

    # These libraries log per-clip detail at INFO, which buries our own output.
    if not args.verbose:
        for noisy in ("faster_whisper", "httpx", "httpcore", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    if cfg_path:
        load_config(cfg_path)
        log.info("configuration: %s (%d preset(s))", cfg_path, len(PRESETS))
    else:
        log.warning("no watchkeeper.toml found; using the built-in presets. "
                    "Looked in: %s", ", ".join(CONFIG_SEARCH))

    if args.list_presets:
        for name in sorted(PRESETS):
            p = PRESETS[name]
            voice = [c for c in p["channels"] if c.role == "voice"]
            print(f"{name}  ({p['mode'].upper()}, centre {p['center_hz']/1e6:.4f} MHz, "
                  f"{len(voice)} channels)")
            for c in p["channels"]:
                tag = "" if c.role == "voice" else f"   [{c.role}]"
                print(f"    {c.freq_hz/1e6:10.4f}  {c.name}{tag}")
        return

    preset = args.preset
    if preset is None:
        preset = "socal_low" if "socal_low" in PRESETS else sorted(PRESETS)[0]
    if preset not in PRESETS:
        sys.exit(f"unknown preset {preset!r}. Available: "
                 + ", ".join(sorted(PRESETS)))

    if args.am_audio_hz:
        globals()["AM_AUDIO_HI"] = args.am_audio_hz
        log.info("AM audio passband set to 300-%d Hz (channel filter %d Hz)",
                 args.am_audio_hz, args.am_audio_hz + 600)

    if args.gain is not None:
        globals()["SDR_GAIN"] = args.gain
        log.info("tuner gain overridden to %s dB", args.gain)

    wk = Watchkeeper(preset, monitor=args.monitor,
                     record_path=args.record, replay_path=args.replay)
    wk.monitor_interval = args.monitor_interval
    signal.signal(signal.SIGINT, wk.shutdown)
    signal.signal(signal.SIGTERM, wk.shutdown)
    wk.run()


if __name__ == "__main__":
    main()
