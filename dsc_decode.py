#!/usr/bin/env python3
"""VHF DSC (Digital Selective Calling) decoder — single-file edition.

Decodes ITU-R M.493 DSC AFSK calls from a WAV file and prints the result
as JSON to stdout.

Usage:
    python3 dsc_decode.py capture.wav            # decode a file
    python3 dsc_decode.py - < capture.wav        # decode audio from stdin
    python3 dsc_decode.py --pretty capture.wav   # human-readable JSON
    python3 dsc_decode.py --debug capture.wav    # include demod diagnostics

Output: one JSON object with "ok", "calls" (array of decoded call records
with format, self MMSI, address, position, UTC, telecommands, EOS/ECC and
confidence), plus signal/duration metadata. See README.md for the full
field reference and examples of adapting the output format.

Requirements: Python 3.9+, numpy, scipy. Everything else is stdlib.

Compliance: ITU-R M.493-9/-11 framing — 20-bit dot pattern, DX-first
phasing (125, 111, 125, 110 ... 125, 106, fmt, 105, fmt, 104), (10,7) block
code with MSB-first check bits (dual-convention tolerant), DX/RX
time-diversity repair, pair-coded position/time, EOS/ECC validation, and
strict call validation (valid self MMSI per ITU-R M.585, known format,
correct symbol count) to reject noise.

Notes:
- Captures shorter than 1.5 s are rejected (VHF DSC frames need the full
  dot+phasing+data preamble).
- Input is resampled to 48 kHz internally if needed; tone frequencies are
  auto-measured to absorb PPM drift / NFM de-emphasis shifts.
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path



# =====================================================================
# symbols — (10,7) block code, MMSI/position/UTC decode, ITU tables
# =====================================================================
from dataclasses import dataclass


BITS_PER_SYMBOL = 10
BIT_RATE = 1200
SPACE_HZ = 1300.0  # Y state, binary 1 (lower frequency per ITU-R M.493-16)
MARK_HZ = 2100.0   # B state, binary 0 (higher frequency per ITU-R M.493-16)


# ITU-R M.493-13 format specifiers (symbol values 100-127)
FORMAT_SPECIFIERS = {
    102: "geographical_area",
    112: "distress",
    114: "group",
    116: "all_ships",
    120: "individual",
    123: "automatic_service",
}

# ITU-R M.493-13 category symbols
CATEGORIES = {
    100: "routine",
    108: "safety",
    110: "urgency",
    112: "distress",
}

# ITU-R M.493-13 nature of distress codes (Table 3).
# These are the actual 7-bit symbol values 100-112, NOT 0-31.
DISTRESS_NATURE = {
    100: "fire, explosion",
    101: "flooding",
    102: "collision",
    103: "grounding",
    104: "listing, in danger of capsizing",
    105: "sinking",
    106: "disabled and adrift",
    107: "undesignated distress",
    108: "abandoning ship",
    109: "piracy/armed robbery attack",
    110: "man overboard",
    111: "unallocated (111)",
    112: "EPIRB emission",
}

# ITU-R M.493-13 subsequent communication type symbols (used in individual calls)
TELECOMMAND_1 = {
    100: "F3E/G3E all modes TP",
    101: "F3E/G3E duplex TP",
    103: "polling",
    104: "unable to comply",
    105: "end of call",
    106: "data",
    109: "J3E TP",
    110: "distress acknowledgement",
    111: "H3E",
    112: "distress relay",
    113: "F1B/J2B TTY-FEC",
    115: "F1B/J2B TTY-ARQ",
    116: "F1B/J2B receive",
    118: "test",
    119: "F1B/J2B",
    120: "A1A Morse, ship's morse key",
    121: "ship position or location registration updating",
    123: "A1A Morse, automatic service",
    124: "F1C/F2C/F3C facsimile",
    126: "no information",
}

TELECOMMAND_2 = {
    100: "no reason given",
    101: "congestion at maritime switching centre",
    102: "busy",
    103: "queue indication",
    104: "station barred",
    105: "no operator available",
    106: "operator temporarily unavailable",
    107: "equipment disabled",
    108: "unable to use proposed channel",
    109: "unable to use proposed mode",
    110: "ships and aircraft (Res. 18)",
    111: "medical transports",
    112: "pay-phone/public call office",
    113: "facsimile/data",
    126: "no information",
}

EOS = {
    117: "acknowledge_rq",
    122: "acknowledge_bq",
    127: "eos",
}

PHASING_RX = tuple(range(111, 103, -1))
PHASING_DX = 125


@dataclass(frozen=True)
class DecodedSymbol:
    symbol: int
    valid: bool
    bits: tuple[int, ...]


def encode_symbol(symbol: int) -> tuple[int, ...]:
    """Encode a 7-bit symbol value (0-127) into its 10-bit DSC codeword (7 info bits + 3 MSB-first check bits, ITU-R M.493-9 S1.1.1)."""
    if not 0 <= symbol <= 127:
        raise ValueError(f"DSC symbol out of range: {symbol}")
    info = tuple((symbol >> i) & 1 for i in range(7))
    b_count = 7 - sum(info)
    check = ((b_count >> 2) & 1, (b_count >> 1) & 1, b_count & 1)
    return info + check


def decode_symbol(bits: tuple[int, ...] | list[int]) -> DecodedSymbol:
    """Decode a 10-bit DSC codeword into (value, valid). Accepts both MSB-first and LSB-first check-bit conventions; the 7 info bits are identical either way."""
    if len(bits) != BITS_PER_SYMBOL:
        return DecodedSymbol(-1, False, tuple(bits))
    value = sum((bit & 1) << i for i, bit in enumerate(bits[:7]))
    expected = encode_symbol(value)
    # ITU-R M.493-9 §1.1.1 transmits check bits most-significant-bit first,
    # but some transmitters send them LSB-first. The 7 information bits are
    # unaffected either way — tolerate both conventions so the validity flag
    # (used by DX/RX time-diversity repair and confidence scoring) stays
    # meaningful for real-world signals.
    expected_rev = expected[:7] + tuple(reversed(expected[7:]))
    valid = tuple(bits) == expected or tuple(bits) == expected_rev
    return DecodedSymbol(value, valid, tuple(bits))


def ecc_for(symbols: list[int]) -> int:
    """Error-check character: XOR of all symbol values, per ITU-R M.493. The ECC is the symbol after the first EOS."""
    ecc = 0
    for symbol in symbols:
        ecc ^= symbol & 0x7F
    return ecc


def pair_digits(symbols: list[int]) -> str:
    """Format 0-99 symbols as a continuous digit string, two digits per symbol (ITU-R M.493 Table 2 pair-coding)."""
    return "".join(f"{symbol:02d}" for symbol in symbols)


def decode_mmsi(symbols: list[int]) -> str:
    """Decode 5 digit-pair symbols into a 9-digit MMSI string.

    MMSI fields are five symbols of 00-99 (10 digits), with X10 always 0
    per ITU-R M.585 (unless M.1080). Service-command symbols (100-127)
    never appear in a valid MMSI field — if any are present the frame is
    misaligned and we return "" so downstream validation rejects it
    instead of storing a bogus 10/11-digit identity.
    """
    if any(symbol >= 100 for symbol in symbols):
        return ""
    digits = pair_digits(symbols)
    if len(digits) == 10 and digits.endswith("0"):
        return digits[:-1]
    return ""


# ITU-R M.585: Valid MID (Maritime Identification Digits) ranges.
# MID is the first 3 digits of a 9-digit MMSI.
_VALID_MID_RANGES = [
    (200, 299),   # Europe
    (300, 399),   # North America, Caribbean
    (400, 499),   # Asia (Middle East)
    (500, 599),   # Asia (South, Southeast)
    (600, 699),   # Africa
    (700, 799),   # South America
    (800, 899),   # Oceania
    (900, 999),   # Special (coast stations, etc.)
]


def is_valid_mmsi(mmsi: str | None) -> bool:
    """Validate a DSC MMSI string per ITU-R M.585.

    Rules:
    - Must be exactly 9 digits
    - First 3 digits (MID) must be in a valid ITU range (200-999)
    - Ship station MMSI: MIDxxxxxx (MID 200-799, last digit varies)
    - Coast station MMSI: 00MIDxxxx (starts with 00, MID in valid range)
    - Group station MMSI: 0MIDxxxxx (starts with 0, MID in valid range)
    - Must not be all filler digits
    """
    if not mmsi or not isinstance(mmsi, str):
        return False
    if len(mmsi) != 9 or not mmsi.isdigit():
        return False

    # Check for all-filler pattern (symbol 127 repeated produces "127127...")
    if all(c == mmsi[0] for c in mmsi):
        return False

    # Determine MID based on MMSI type per ITU-R M.585
    if mmsi.startswith("00"):
        # Coast station: 00MIDxxxx
        mid = int(mmsi[2:5])
    elif mmsi.startswith("0"):
        # Group station: 0MIDxxxxx
        mid = int(mmsi[1:4])
    else:
        # Ship station: MIDxxxxxx
        mid = int(mmsi[:3])

    # Validate MID is in an ITU-assigned range
    for low, high in _VALID_MID_RANGES:
        if low <= mid <= high:
            return True
    return False


def decode_position(symbols: list[int]) -> dict[str, object] | None:
    """Decode DSC position from 5 pair-coded symbols (ITU-R M.493-11, Table 2/4.1).

    Per ITU-R M.493-11 section 8.1.2, distress coordinates are TEN digits
    coded in pairs (Table 2): 5 symbols, each carrying 2 decimal digits
    (00-99). The 10 digits are:
      [quadrant, lat_deg_tens, lat_deg_units, lat_min_tens, lat_min_units,
       lon_deg_hundreds, lon_deg_tens, lon_deg_units, lon_min_tens, lon_min_units]

    Per section 8.1.2.1: quadrant 0 = NE, 1 = NW, 2 = SE, 3 = SW.
    Per section 8.1.2.4: digit 9 repeated 10 times = position unavailable.
    """
    if len(symbols) != 5:
        return None

    digits = list(pair_digits(symbols))  # 10 chars

    # Position unavailable (all 9s)
    if all(d == "9" for d in digits):
        return {"raw": "".join(digits), "valid": False, "unavailable": True}

    quadrant = int(digits[0])
    lat_hemi = "N" if quadrant in (0, 1) else "S"
    lat_deg = int(digits[1]) * 10 + int(digits[2])
    lat_min = int(digits[3]) * 10 + int(digits[4])

    lon_hemi = "E" if quadrant in (0, 2) else "W"
    lon_deg = int(digits[5]) * 100 + int(digits[6]) * 10 + int(digits[7])
    lon_min = int(digits[8]) * 10 + int(digits[9])

    if lat_deg > 90 or lat_min > 59 or lon_deg > 180 or lon_min > 59:
        return {"raw": "".join(digits), "valid": False}

    latitude = lat_deg + lat_min / 60.0
    longitude = lon_deg + lon_min / 60.0
    if lat_hemi == "S":
        latitude = -latitude
    if lon_hemi == "W":
        longitude = -longitude

    return {
        "raw": "".join(digits),
        "valid": True,
        "latitude": round(latitude, 4),
        "longitude": round(longitude, 4),
        "hemispheres": lat_hemi + lon_hemi,
    }


def decode_utc(symbols: list[int]) -> dict[str, object] | None:
    """Decode UTC time from 2 pair-coded DSC symbols (ITU-R M.493-11, Table 2).

    Per ITU-R M.493-11 section 8.1.3, the time indication is FOUR digits
    coded in pairs: 2 symbols, each carrying 2 decimal digits (HH MM).
    Per section 8.1.3.3: digit 8 repeated four times = time unavailable.
    """
    if len(symbols) != 2:
        return None

    digits = list(pair_digits(symbols))  # 4 chars

    # Time unavailable (all 8s)
    if all(d == "8" for d in digits):
        return None

    hour = int(digits[0]) * 10 + int(digits[1])
    minute = int(digits[2]) * 10 + int(digits[3])

    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return {"raw": f"{hour:02d}{minute:02d}", "valid": True, "hour": hour, "minute": minute}

    return {"raw": "".join(digits), "valid": False}

# =====================================================================
# audio — WAV/FFmpeg input, resample, de-emphasis, signal detection
# =====================================================================
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np


TARGET_RATE = 48_000
# DSC tone frequencies (ITU-R M.493-16)
SPACE_HZ = 1300.0  # Y state, binary 1 (lower frequency)
MARK_HZ = 2100.0   # B state, binary 0 (higher frequency)
# Bandpass filter range: wide enough to cover both tones with margin
BANDPASS_LOW_HZ = 800.0
BANDPASS_HIGH_HZ = 2600.0


def de_emphasis_filter(samples: np.ndarray, sample_rate: int, time_constant: float = 50e-6) -> np.ndarray:
    """Apply 6 dB/octave de-emphasis to reverse transmitter pre-emphasis.

    ITU-R M.493-11 section 1.3.2 specifies FM with 6 dB/octave pre-emphasis.
    The de-emphasis filter is a first-order low-pass with time constant 50 us.

    H(f) = 1 / (1 + j * 2pi * f * tau)

    At 1300 Hz: |H| approx 0.96 (minimal attenuation)
    At 2100 Hz: |H| approx 0.91 (slight attenuation)
    This corrects the frequency response imbalance.
    """
    if len(samples) < 2:
        return samples

    # First-order IIR de-emphasis filter
    # y[n] = a * x[n] + (1 - a) * y[n-1]
    # where a = dt / (tau + dt), dt = 1/sample_rate
    dt = 1.0 / sample_rate
    a = dt / (time_constant + dt)

    result = np.zeros_like(samples)
    result[0] = samples[0]
    for i in range(1, len(samples)):
        result[i] = a * samples[i] + (1.0 - a) * result[i - 1]

    return result.astype(np.float32)


def bandpass_filter(samples: np.ndarray, sample_rate: int, low_hz: float = BANDPASS_LOW_HZ, high_hz: float = BANDPASS_HIGH_HZ) -> np.ndarray:
    """Apply a frequency-domain bandpass filter to reject out-of-band noise.

    Uses FFT to zero out frequencies outside [low_hz, high_hz]. Its short
    main-lobe (<1 bit at 1200 baud) distorts alternating FSK less than an
    IIR filter whose ~100-sample impulse response spans 2-3 bits.

    Inputs: samples (float32 array), sample_rate (int), low_hz, high_hz
    Outputs: filtered samples (same length as input)
    """
    if len(samples) < sample_rate // 10:
        # Too short for meaningful FFT filtering - skip
        return samples

    fft = np.fft.rfft(samples)
    freqs = np.fft.rfftfreq(len(samples), d=1.0 / sample_rate)

    # Zero out frequencies outside the passband
    mask = (freqs >= low_hz) & (freqs <= high_hz)
    fft[~mask] = 0.0

    filtered = np.fft.irfft(fft, n=len(samples))
    return filtered.astype(np.float32)


def detect_dsc_signal(samples: np.ndarray, sample_rate: int, threshold: float = 0.15) -> dict[str, object]:
    """Detect whether the audio contains DSC-like AFSK energy.

    Computes the energy ratio at the DSC space (1300 Hz) and mark (2100 Hz)
    frequencies relative to total band-limited energy. If the ratio is above
    the threshold, the signal likely contains DSC tones.

    Uses a narrow Goertzel-like detection around each tone (+/- 100 Hz).

    Inputs: samples (float32 array), sample_rate (int), threshold (float)
    Outputs: dict with has_dsc_signal, space_energy, mark_energy, total_energy, ratio
    """
    if len(samples) < sample_rate // 10:
        return {"has_dsc_signal": False, "reason": "audio_too_short_for_detection"}

    # Apply bandpass first to focus on relevant frequencies
    filtered = bandpass_filter(samples, sample_rate)

    # Use FFT to compute energy in narrow bands around mark and space
    fft = np.fft.rfft(filtered)
    freqs = np.fft.rfftfreq(len(filtered), d=1.0 / sample_rate)
    power = np.abs(fft) ** 2

    # Energy in narrow bands around DSC tones (+/- 100 Hz)
    bandwidth = 100.0
    mark_mask = (freqs >= MARK_HZ - bandwidth) & (freqs <= MARK_HZ + bandwidth)
    space_mask = (freqs >= SPACE_HZ - bandwidth) & (freqs <= SPACE_HZ + bandwidth)
    band_mask = (freqs >= BANDPASS_LOW_HZ) & (freqs <= BANDPASS_HIGH_HZ)

    mark_energy = float(np.sum(power[mark_mask]))
    space_energy = float(np.sum(power[space_mask]))
    total_band_energy = float(np.sum(power[band_mask]))

    if total_band_energy == 0:
        return {
            "has_dsc_signal": False,
            "mark_energy": 0.0,
            "space_energy": 0.0,
            "total_band_energy": 0.0,
            "dsc_ratio": 0.0,
        }

    dsc_ratio = (mark_energy + space_energy) / total_band_energy

    return {
        "has_dsc_signal": dsc_ratio >= threshold,
        "mark_energy": mark_energy,
        "space_energy": space_energy,
        "total_band_energy": total_band_energy,
        "dsc_ratio": round(dsc_ratio, 4),
    }


def read_audio(path: Path) -> tuple[np.ndarray, int]:
    """Read an audio file and return (samples: float ndarray in [-1,1], sample_rate: int). WAV via stdlib; other formats via FFmpeg if available."""
    try:
        return _read_wav(path)
    except (wave.Error, EOFError):
        return _read_with_ffmpeg(path)


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Read a WAV file with the stdlib wave module; map common sample widths to float and normalize."""
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    if sample_width == 1:
        data = np.frombuffer(frames, dtype=np.uint8).astype(np.float32)
        data = (data - 128.0) / 128.0
    elif sample_width == 2:
        data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 3:
        raw = np.frombuffer(frames, dtype=np.uint8).reshape(-1, 3)
        signed = raw[:, 0].astype(np.int32) | (raw[:, 1].astype(np.int32) << 8) | (raw[:, 2].astype(np.int32) << 16)
        signed = np.where(signed & 0x800000, signed - 0x1000000, signed)
        data = signed.astype(np.float32) / 8388608.0
    elif sample_width == 4:
        data = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise wave.Error(f"unsupported WAV sample width: {sample_width}")
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)

    # Resample to TARGET_RATE if needed so the demodulator always sees 48kHz.
    # This ensures consistent samples_per_bit = 48000/1200 = 40 across all inputs.
    if rate != TARGET_RATE:
        from scipy import signal as scipy_signal
        ratio = TARGET_RATE / rate
        # Use resample_poly for high-quality bandlimited resampling
        up, down = ratio.as_integer_ratio()
        # Simplify the ratio to avoid huge integers
        from math import gcd
        g = gcd(up, down)
        up //= g
        down //= g
        data = scipy_signal.resample_poly(data, up, down).astype(np.float32)
        rate = TARGET_RATE

    return preprocess_audio(data, rate), rate


def _read_with_ffmpeg(path: Path) -> tuple[np.ndarray, int]:
    """Read a non-WAV file by shelling out to FFmpeg (16-bit PCM mono to stdout). Returns None if ffmpeg is unavailable."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(path),
                "-ac",
                "1",
                "-ar",
                str(TARGET_RATE),
                str(tmp_path),
            ],
            check=True,
        )
        return _read_wav(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _normalize(samples: np.ndarray) -> np.ndarray:
    """Normalize samples to a [-1, 1] peak amplitude (no-op for silence)."""
    samples = np.asarray(samples, dtype=np.float32)
    samples = samples - float(np.mean(samples))
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak > 0:
        samples = samples / peak
    return samples


def preprocess_audio(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """Normalize and bandpass-filter audio for DSC decoding.

    Note: De-emphasis is NOT applied here. The audio from rtl_airband is
    already FM-demodulated (NFM), which inherently reverses the transmitter's
    6 dB/octave pre-emphasis. Applying de-emphasis again would double-correct
    and slightly distort the DSC tone amplitudes (ITU-R M.493-11 §1.3.2).

    Inputs: samples (float32 array), sample_rate (int)
    Outputs: normalized and filtered samples
    """
    samples = _normalize(samples)
    # De-emphasis removed: already handled by rtl_airband's FM demodulator.
    samples = bandpass_filter(samples, sample_rate)
    return samples

# =====================================================================
# afsk — dot-pattern sync, I/Q quadrature demod, clock recovery
# =====================================================================
from dataclasses import dataclass

import numpy as np

from scipy import signal as scipy_signal


@dataclass(frozen=True)
class BitCandidate:
    bits: list[int]
    confidence: float
    offset: float
    samples_per_bit: float
    freq_offset_hz: float = 0.0
    dot_sync_confidence: float = 0.0


def demodulate_candidates(
    samples: np.ndarray,
    sample_rate: int,
    offsets: int = 16,
    freq_offsets_hz: list[float] | None = None,
    enable_dot_sync: bool = True,
    enable_clock_recovery: bool = True,
) -> list[BitCandidate]:
    """Demodulate DSC AFSK signal with advanced signal processing.

    Args:
        samples: Audio samples
        sample_rate: Sample rate in Hz
        offsets: Number of timing offsets to try per frequency offset
        freq_offsets_hz: List of frequency offsets to try (Hz). If None, uses AFC search.
        enable_dot_sync: Enable dot pattern detection for bit sync
        enable_clock_recovery: Enable clock recovery for timing jitter

    Returns:
        List of BitCandidate sorted by confidence (best first)
    """
    # Default AFC search range: ±100 Hz in 25 Hz steps
    # (narrowed from ±200 Hz since _measure_signal_tones provides accurate frequencies)
    if freq_offsets_hz is None:
        freq_offsets_hz = list(range(-100, 101, 25))

    samples_per_bit = sample_rate / BIT_RATE
    candidates: list[BitCandidate] = []

    # First-pass: measure actual signal tones from the audio
    # This makes the decoder adaptive to any PPM drift, NFM demod quirks,
    # or MP3 compression artifacts that shift tones independently.
    auto_space_hz, auto_mark_hz = _measure_signal_tones(samples, sample_rate)
    if auto_space_hz is not None and auto_mark_hz is not None:
        pass  # Using measured tones below

    for freq_offset in freq_offsets_hz:
        # Build reference frequencies — use measured tones if available
        # so each tone gets its own independent offset correction
        if auto_space_hz is not None and auto_mark_hz is not None:
            space_hz = auto_space_hz + float(freq_offset)
            mark_hz = auto_mark_hz + float(freq_offset)
        else:
            space_hz = SPACE_HZ + float(freq_offset)
            mark_hz = MARK_HZ + float(freq_offset)
        # Try dot pattern sync first
        dot_synced = False
        if enable_dot_sync:
            dot_offset, dot_confidence = _find_dot_pattern(
                samples, sample_rate, samples_per_bit, freq_offset,
                space_hz=space_hz, mark_hz=mark_hz,
            )
            if dot_confidence > 0.25:  # Good dot pattern detected (lowered from 0.3 for weak signals)
                bits, confidence = _demodulate_with_clock_recovery(
                    samples, sample_rate, samples_per_bit, dot_offset, freq_offset,
                    enable_clock_recovery,
                    space_hz=space_hz, mark_hz=mark_hz,
                )
                # Boost confidence for dot-synced candidates
                boosted_confidence = confidence * (1.0 + dot_confidence * 0.5)
                candidates.append(BitCandidate(
                    bits=bits,
                    confidence=boosted_confidence,
                    offset=dot_offset,
                    samples_per_bit=samples_per_bit,
                    freq_offset_hz=freq_offset,
                    dot_sync_confidence=dot_confidence,
                ))
                dot_synced = True

        # Also try brute-force timing offsets (fallback — skip if dot sync succeeded)
        if not dot_synced:
            count = min(offsets, max(1, round(samples_per_bit)))
            for offset in np.linspace(0, samples_per_bit, count, endpoint=False):
                bits, confidence = _demodulate_with_clock_recovery(
                    samples, sample_rate, samples_per_bit, float(offset), freq_offset,
                    enable_clock_recovery,
                    space_hz=space_hz, mark_hz=mark_hz,
                )
                candidates.append(BitCandidate(
                    bits=bits,
                    confidence=confidence,
                    offset=float(offset),
                    samples_per_bit=samples_per_bit,
                    freq_offset_hz=freq_offset,
                    dot_sync_confidence=0.0,
                ))

    return sorted(candidates, key=lambda item: item.confidence, reverse=True)


def _measure_signal_tones(
    samples: np.ndarray,
    sample_rate: int,
) -> tuple[float | None, float | None]:
    """Measure the actual space and mark frequencies from the signal.

    NFM demodulation, PPM drift, and MP3 encoding can shift the expected
    1300/2100 Hz tones independently. This function finds the actual tone
    frequencies by looking at the power spectrum of the signal.

    Uses a sliding-window approach to find the signal-rich portion of the
    audio — looks for the window where BOTH space and mark tone bands have
    strong energy, then measures peak frequencies on that best window.
    This avoids noise peaks in empty or quiet portions of the buffer.

    Returns:
        Tuple of (space_hz, mark_hz) or (None, None) if unreliable.
    """
    # Bandpass filter to isolate DSC energy
    nyq = sample_rate / 2.0
    b, a = scipy_signal.butter(4, [800/nyq, 2800/nyq], btype="band")
    filtered = scipy_signal.filtfilt(b, a, samples)

    # Sliding window to find the signal-rich portion
    # DSC signal needs both tone bands active — find the best 2s window
    window_len = min(int(sample_rate * 2.0), len(filtered))
    stride = max(1, int(sample_rate * 0.25))  # 250ms steps
    best_combined = 0.0
    best_start = 0

    for start in range(0, max(1, len(filtered) - window_len + stride), stride):
        end = min(start + window_len, len(filtered))
        if end - start < sample_rate * 0.5:  # Need at least 500ms
            continue
        wf = filtered[start:end] * np.hanning(end - start)
        wf_fft = np.abs(np.fft.rfft(wf))**2
        wf_freqs = np.fft.rfftfreq(end - start, 1.0/sample_rate)

        energy_1300 = float(np.sum(wf_fft[(wf_freqs >= 1100) & (wf_freqs <= 1500)]))
        energy_2100 = float(np.sum(wf_fft[(wf_freqs >= 1900) & (wf_freqs <= 2300)]))
        combined = energy_1300 * energy_2100  # Both bands must be present

        if combined > best_combined:
            best_combined = combined
            best_start = start

    # Use the best window for tone measurement
    if best_combined > 0:
        end = min(best_start + window_len, len(filtered))
        samples = samples[best_start:end]
        # Re-filter just the best window
        filtered = scipy_signal.filtfilt(b, a, samples)

    # Compute power spectrum
    fft = np.abs(np.fft.rfft(filtered * np.hanning(len(filtered))))**2
    freqs = np.fft.rfftfreq(len(filtered), 1.0/sample_rate)

    # Search for the strongest peak near each expected frequency
    # with a ±400 Hz window to handle large PPM drift
    space_hz = None
    mark_hz = None

    for expected, lo, hi in [(1300, 1100, 1500), (2100, 1900, 2300)]:
        win = (freqs >= lo) & (freqs <= hi)
        if np.sum(win) < 2:
            continue
        pi = np.argmax(fft[win])
        peak_freq = freqs[win][pi]
        peak_energy = fft[win][pi]

        # Check that the peak is above the noise floor (lowered threshold for weak signals)
        mean_noise = np.mean(fft[(freqs >= lo) & (freqs <= hi)])
        if peak_energy > mean_noise * 1.5:
            # Check if the peak is near the expected frequency. If not, search for
            # the strongest peak within ±80 Hz of expected (handles PPM drift).
            if abs(peak_freq - expected) > 80:
                narrow = (freqs >= expected - 80) & (freqs <= expected + 80) & win
                if np.sum(narrow) > 0:
                    pi2 = np.argmax(fft[narrow])
                    peak_freq = freqs[narrow][pi2]
            if expected == 1300:
                space_hz = peak_freq
            else:
                mark_hz = peak_freq

    if space_hz is not None and mark_hz is not None:
        return space_hz, mark_hz
    return None, None


def _find_dot_pattern(
    samples: np.ndarray,
    sample_rate: int,
    samples_per_bit: float,
    freq_offset_hz: float = 0.0,
    space_hz: float | None = None,
    mark_hz: float | None = None,
) -> tuple[float, float]:
    """Find the dot pattern (alternating 1/0) at the start of DSC frame.

    The dot pattern provides bit synchronization. It consists of alternating
    mark and space tones (1000 Hz shift at 1200 baud = alternating every bit).

    Returns:
        Tuple of (best_offset, confidence) where confidence is 0-1
    """
    # First, find where the signal starts by looking for energy burst
    signal_start = _find_signal_start(samples, sample_rate)
    if signal_start is None:
        return 0.0, 0.0

    # Generate reference dot pattern (alternating tones).
    # IMPORTANT: Use a single t array for the full pattern to maintain
    # continuous phase across bit boundaries. Per-bit t_bit arrays cause
    # phase resets that destroy the matched filter correlation.
    pattern_len = int(samples_per_bit * 20)  # 20 bits of dot pattern
    t = np.arange(pattern_len, dtype=np.float32) / sample_rate
    space_freq = (space_hz if space_hz is not None else SPACE_HZ) + freq_offset_hz
    mark_freq = (mark_hz if mark_hz is not None else MARK_HZ) + freq_offset_hz
    ref_dot = np.zeros(pattern_len, dtype=np.float32)
    for i in range(20):
        start = int(i * samples_per_bit)
        end = int((i + 1) * samples_per_bit)
        if end > pattern_len:
            end = pattern_len
        freq = mark_freq if i % 2 == 0 else space_freq
        ref_dot[start:end] = np.sin(2 * np.pi * freq * t[start:end])

    # Search around the signal start. The ITU-R M.493 carrier before the dot
    # pattern is "~100 ms" but varies by transmitter, so search a generous
    # ±200 ms window to make sure the dot pattern is inside it.
    search_center = signal_start
    search_range = int(sample_rate * 0.2)  # 200ms
    search_start = max(0, search_center - search_range)
    search_end = min(len(samples) - pattern_len, search_center + search_range)

    best_offset = float(search_center)
    best_corr = 0.0
    step = max(1, int(samples_per_bit / 4))  # Quarter-bit resolution

    for offset in range(search_start, search_end, step):
        chunk = samples[offset:offset + pattern_len]
        if len(chunk) < pattern_len:
            break

        # Normalize chunks. Guard against (near-)constant chunks — e.g. the
        # mean-shifted silence of a clean capture, whose std is ~0 — where
        # the +1e-10 epsilon would explode chunk_norm and fabricate a
        # perfect (but spurious) correlation anywhere in the silence.
        chunk_std = np.std(chunk)
        if chunk_std < 1e-6:
            continue
        chunk_norm = chunk / chunk_std
        ref_norm = ref_dot / (np.std(ref_dot) + 1e-10)

        # Cross-correlation
        corr = float(np.abs(np.dot(chunk_norm, ref_norm)) / pattern_len)

        if corr > best_corr:
            best_corr = corr
            best_offset = float(offset)

    # Refine offset with finer resolution
    if best_corr > 0.2:
        refine_range = int(samples_per_bit)
        for fine_offset in np.linspace(
            best_offset - refine_range,
            best_offset + refine_range,
            20,
        ):
            if fine_offset < 0 or fine_offset + pattern_len > len(samples):
                continue
            chunk = samples[int(fine_offset):int(fine_offset) + pattern_len]
            chunk_norm = chunk / (np.std(chunk) + 1e-10)
            corr = float(np.abs(np.dot(chunk_norm, ref_norm)) / pattern_len)
            if corr > best_corr:
                best_corr = corr
                best_offset = float(fine_offset)

    return best_offset, min(1.0, best_corr * 2)  # Scale confidence to 0-1


def _find_signal_start(samples: np.ndarray, sample_rate: int) -> int | None:
    """Find where the DSC signal starts by detecting energy burst.

    Returns:
        Sample index where signal starts, or None if no signal found
    """
    # Calculate energy in 10ms windows
    window_size = int(sample_rate * 0.01)
    energies = []

    for i in range(0, len(samples) - window_size, window_size):
        chunk = samples[i:i + window_size]
        energy = float(np.sum(chunk ** 2))
        energies.append((i, energy))

    if not energies:
        return None

    # Use the quietest part of the file as noise reference, not just first 500ms.
    # Short files (<1s) may be all signal — use the lowest-energy windows instead.
    all_energies = sorted([e for _, e in energies])
    quiet_count = max(10, len(all_energies) // 4)  # bottom quartile of windows + 10 min
    noise_floor = np.median(all_energies[:quiet_count])

    # Relative absolute threshold: a fixed floor (e.g. 0.001) trips on the
    # bandpass filter's sinc ringing (~0.003 amplitude) that precedes the
    # signal in clean captures with silent lead-in. Scale the floor with the
    # peak window energy so ringing (<< 0.1% of the signal) is ignored while
    # genuinely weak signals are still found. Real captures are unaffected
    # because their noise floor dominates the absolute term.
    max_energy = all_energies[-1] if all_energies else 0.0
    abs_threshold = max(0.001, max_energy * 1e-3)

    threshold = max(noise_floor * 5, abs_threshold)

    for sample_idx, energy in energies:
        if energy > threshold:
            # Ensure we found a real signal, not noise spike
            # Confirm sustained energy over the next 50ms
            next_idx = sample_idx + int(sample_rate * 0.05)
            if next_idx < len(samples):
                next_chunk = samples[next_idx:next_idx + window_size]
                next_energy = float(np.sum(next_chunk ** 2))
                if next_energy > threshold:
                    return sample_idx
            return sample_idx

    # Fallback: the signal may fill the whole file (no quiet portion), which
    # makes the bottom-quartile 5x threshold unreachable. Use the global
    # median instead; anything above it with sustained energy counts as the
    # signal start. Noise-only files are still rejected downstream by the
    # dot-pattern correlation gate.
    alt_threshold = max(float(np.median(all_energies)) * 0.8, abs_threshold)
    for sample_idx, energy in energies:
        if energy > alt_threshold:
            next_idx = sample_idx + int(sample_rate * 0.05)
            if next_idx < len(samples):
                next_energy = float(np.sum(samples[next_idx:next_idx + window_size] ** 2))
                if next_energy > alt_threshold:
                    return sample_idx
            return sample_idx

    return None


def _demodulate_with_clock_recovery(
    samples: np.ndarray,
    sample_rate: int,
    samples_per_bit: float,
    offset: float,
    freq_offset_hz: float = 0.0,
    enable_clock_recovery: bool = True,
    space_hz: float | None = None,
    mark_hz: float | None = None,
) -> tuple[list[int], float]:
    """Demodulate with optional clock recovery for timing jitter.

    Uses Gardner timing error detector for clock recovery.
    """
    usable = len(samples) - offset
    n_bits = int(usable // samples_per_bit)
    if n_bits <= 0:
        return [], 0.0

    window_len = max(1, round(samples_per_bit))
    window = np.hanning(window_len).astype(np.float32)
    t = np.arange(window_len, dtype=np.float32) / float(sample_rate)

    # Apply frequency offset to tone frequencies
    space_freq = (space_hz if space_hz is not None else SPACE_HZ) + freq_offset_hz
    mark_freq = (mark_hz if mark_hz is not None else MARK_HZ) + freq_offset_hz

    space_i = np.cos(2 * np.pi * space_freq * t)
    space_q = np.sin(2 * np.pi * space_freq * t)
    mark_i = np.cos(2 * np.pi * mark_freq * t)
    mark_q = np.sin(2 * np.pi * mark_freq * t)

    bits: list[int] = []
    margins: list[float] = []

    # Clock recovery state
    timing_offset = 0.0
    timing_gain = 0.1  # Loop gain for timing recovery

    for idx in range(n_bits):
        # Calculate sample position with timing offset
        center = offset + (idx + 0.5 + timing_offset) * samples_per_bit
        start = round(center - window_len / 2)
        end = start + window_len

        if start < 0 or end > len(samples):
            continue

        chunk = samples[start:end] * window

        # Correlate with mark and space tones
        space = float(np.dot(chunk, space_i) ** 2 + np.dot(chunk, space_q) ** 2)
        mark = float(np.dot(chunk, mark_i) ** 2 + np.dot(chunk, mark_q) ** 2)

        bit = 1 if space >= mark else 0
        bits.append(bit)

        # Calculate confidence margin
        total = space + mark
        margin = abs(space - mark) / total if total else 0.0
        margins.append(margin)

        # Clock recovery: Gardner-style timing error detector, gated on data
        # transitions. The older implementation updated on EVERY bit with a
        # constant sign term `1 if current_energy > 0 else -1` — energies are
        # always positive, so the loop never saw a zero error and oscillated,
        # corrupting clean strong signals (synthetic captures decoded as
        # garbage while a pristine dot pattern was found). Standard Gardner
        # only corrects when a symbol transition occurs.
        if enable_clock_recovery and idx > 0 and start > 0 and end < len(samples):
            prev_bit = bits[-1]
            if bit != prev_bit:
                # Get samples at half-bit intervals for Gardner detector
                early_start = round(center - samples_per_bit / 4)
                late_start = round(center + samples_per_bit / 4)

                if early_start >= 0 and late_start + window_len <= len(samples):
                    early_chunk = samples[early_start:early_start + window_len] * window
                    late_chunk = samples[late_start:late_start + window_len] * window

                    early_space = float(np.dot(early_chunk, space_i) ** 2 + np.dot(early_chunk, space_q) ** 2)
                    early_mark = float(np.dot(early_chunk, mark_i) ** 2 + np.dot(early_chunk, mark_q) ** 2)
                    late_space = float(np.dot(late_chunk, space_i) ** 2 + np.dot(late_chunk, space_q) ** 2)
                    late_mark = float(np.dot(late_chunk, mark_i) ** 2 + np.dot(late_chunk, mark_q) ** 2)

                    # Gardner error: (late - early) * direction of transition
                    early_energy = early_space if bit == 0 else early_mark
                    late_energy = late_space if bit == 0 else late_mark
                    current_energy = space if bit == 0 else mark

                    direction = 1 if bit == 1 else -1
                    timing_error = (late_energy - early_energy) * direction
                    timing_offset += timing_gain * timing_error / (current_energy + 1e-10)

                    # Clamp timing offset to prevent drift
                    timing_offset = max(-0.4, min(0.4, timing_offset))

    return bits, float(np.mean(margins)) if margins else 0.0

# =====================================================================
# parser — phasing alignment, DX/RX repair, frame validation
# =====================================================================
from collections import Counter
from dataclasses import dataclass


# ITU-R M.493-13 expected symbol counts per format (after DX/RX recovery).
# Fixed-count formats: must match exactly.
# Minimum-count formats: must be >= this value (variable filler/area description).
_FORMAT_SYMBOL_COUNTS = {
    112: 18,   # Distress alert: exact (fmt2 self5 nature1 pos5 time2 comm1 eos1 ecc1)
    120: 23,   # Individual: exact
    114: 23,   # Group: exact
    116: 11,   # All ships: minimum (variable filler)
    102: 12,   # Geographic area: minimum (variable area description)
    123: 13,   # Automatic service: minimum (variable telephone number)
}
_FORMAT_EXACT_COUNTS = {112, 120, 114}


@dataclass(frozen=True)
class FrameCandidate:
    symbols: list[int]
    invalid_symbols: int
    bit_offset: int
    score: int


def parse_bits(bits: list[int]) -> list[dict[str, object]]:
    """Decode a bit stream into validated call records. Tries all 10 symbol alignments, finds phasing, recovers the DX sequence with RX repair, trims at EOS+ECC, dedupes, and runs validate_call on every candidate."""
    frames: list[FrameCandidate] = []
    for bit_offset in range(10):
        decoded = _decode_symbols(bits[bit_offset:])
        frames.extend(_find_frames(decoded, bit_offset))
    best = _dedupe_frames(frames)
    calls = [_frame_to_json(frame) for frame in best]
    # Filter out calls that fail validation
    return [c for c in calls if validate_call(c)]


def validate_call(call: dict[str, object]) -> bool:
    """Validate a decoded DSC call for authenticity.

    The 7-bit XOR ECC has a 1/128 false positive rate, so random noise
    can occasionally pass. This function applies additional checks to
    reject false decodes.

    Returns True if the call is likely authentic, False if likely noise.
    """
    raw_symbols = call.get("raw_symbols", [])
    confidence = call.get("confidence", {})
    ecc = call.get("ecc", {})
    fmt = call.get("format", {})
    category = call.get("category")
    self_id = call.get("self_id")
    message = call.get("message", {})

    # 1. ECC must be valid
    if not ecc.get("ok", False):
        return False

    # 2. Format specifier must be known
    fmt_symbol = fmt.get("symbol")
    if fmt_symbol not in FORMAT_SPECIFIERS:
        return False

    # 3. Self ID (MMSI) must be valid if present
    if self_id and not is_valid_mmsi(self_id):
        return False

    # 4. Category must be known if present
    if category is not None:
        cat_symbol = category.get("symbol") if isinstance(category, dict) else None
        if cat_symbol is not None and cat_symbol not in CATEGORIES:
            return False

    # 5. Too many filler symbols (127) indicates noise
    filler_count = sum(1 for s in raw_symbols if s == 127)
    total_symbols = len(raw_symbols)
    if total_symbols > 0 and filler_count / total_symbols > 0.5:
        return False

    # 6. Too many invalid symbols indicates poor decode quality
    invalid = confidence.get("invalid_symbols", 0)
    if invalid > 25:
        return False

    # 7. Score must be above minimum (lowered for weak/noisy signals)
    score = confidence.get("score", 0)
    if score < 30:
        return False

    # 8. Symbol count must match ITU-R M.493-13 expectations.
    #    This is a safety net — _trim_to_ecc should already have validated.
    expected = _FORMAT_SYMBOL_COUNTS.get(fmt_symbol)
    if expected is not None:
        if fmt_symbol in _FORMAT_EXACT_COUNTS:
            if len(raw_symbols) != expected:
                return False
        else:
            if len(raw_symbols) < expected:
                return False

    # 9. Every ITU-R M.493 call carries the calling station's MMSI as
    #    self-identification (§7.1) — including distress and geo-area.
    #    Frames with no valid self_id are noise: the ECC false-positive
    #    rate is 1/128 and the symbol-level fallback routinely produces
    #    frames from non-signal audio. Geo-area calls address a geographic
    #    BOX (not an MMSI) so their address is an object, but the calling
    #    station's MMSI is still required.
    self_ok = bool(self_id) and is_valid_mmsi(self_id)
    if not self_ok:
        return False

    # 10. Formats with an MMSI address (individual/group/auto-service) must
    #     also carry a valid called-station identity.
    if fmt_symbol in (120, 114, 123):
        addr = call.get("address")
        addr_ok = isinstance(addr, str) and is_valid_mmsi(addr)
        if not addr_ok:
            return False

    # 11. Geo-area calls: the 5-symbol address is a digit-pair (00-99) zone.
    #     Any service symbol (100-127) in the address region means the frame
    #     is misaligned (a real zone is always pairs of decimal digits).
    if fmt_symbol == 102:
        area = call.get("address")
        if isinstance(area, dict):
            area_syms = area.get("raw_symbols") or []
            if any(s >= 100 for s in area_syms):
                return False
            # Every digit must be 0-9 (pair-decode of 00-99 symbols)
            if any(d > 9 for d in area.get("digits") or []):
                return False
        else:
            return False

    return True


def analyze_bits(bits: list[int]) -> dict[str, object]:
    """Return lightweight decode diagnostics without requiring a valid frame."""
    per_offset: list[dict[str, object]] = []
    for bit_offset in range(10):
        decoded = _decode_symbols(bits[bit_offset:])
        valid = [item.symbol for item in decoded if item.valid]
        valid_counter = Counter(valid)
        phasing_hits = _best_phasing_hits([item.symbol for item in decoded])
        per_offset.append(
            {
                "bit_offset": bit_offset,
                "symbols": len(decoded),
                "valid_symbols": len(valid),
                "valid_ratio": round(len(valid) / len(decoded), 4) if decoded else 0.0,
                "best_phasing_run": phasing_hits,
                "format_like_symbols": sum(1 for item in valid if item in FORMAT_SPECIFIERS),
                "eos_like_symbols": sum(1 for item in valid if item in EOS),
                "top_symbols": [
                    {"symbol": symbol, "count": count}
                    for symbol, count in valid_counter.most_common(10)
                ],
            }
        )
    return {
        "bit_count": len(bits),
        "symbol_offsets": sorted(
            per_offset,
            key=lambda item: (
                int(item["best_phasing_run"]),
                float(item["valid_ratio"]),
                int(item["format_like_symbols"]),
                int(item["eos_like_symbols"]),
            ),
            reverse=True,
        ),
    }


def _decode_symbols(bits: list[int]) -> list[DecodedSymbol]:
    """Group a bit list into 10-bit symbols and decode each (trailing partial symbol dropped)."""
    return [decode_symbol(tuple(bits[i : i + 10])) for i in range(0, len(bits) - 9, 10)]


def _find_frames(decoded: list[DecodedSymbol], bit_offset: int) -> list[FrameCandidate]:
    """Scan a decoded symbol stream for phasing-aligned frames; return scored FrameCandidates."""
    frames: list[FrameCandidate] = []
    values = [item.symbol for item in decoded]
    for start in range(0, max(0, len(values) - 14)):
        align = _phasing_alignment(values, start)
        if align is None:
            continue
        first_slot, dx_parity = align
        # The phasing alignment gives an approximate first_slot.
        # The exact position varies by parity and how many pairs matched.
        # Try a range of offsets to find the actual data start.
        for offset in range(-4, 6):
            fs = first_slot + offset
            if fs < 0 or fs >= len(decoded):
                continue
            info, invalid = _recover_information(decoded, fs, dx_parity)
            frame = _trim_to_ecc(info)
            if frame:
                frame_invalid = _invalid_dx_symbols(decoded, fs, len(frame))
                score = len(frame) * 10 - frame_invalid * 8 + sum(1 for s in frame if s in FORMAT_SPECIFIERS or s in EOS)
                frames.append(FrameCandidate(symbols=frame, invalid_symbols=frame_invalid, bit_offset=bit_offset, score=score))
                break  # First valid frame at this phasing start is best
    return frames


def _phasing_alignment(values: list[int], start: int) -> tuple[int, int] | None:
    """Find phasing alignment per ITU-R M.493-11 section 3.3.

    Phasing is achieved when two DXs and one RX, or two RXs and one DX,
    or three RXs in the appropriate positions are successfully received.
    These may be non-consecutive.
    """
    for parity in (0, 1):
        dx_hits = 0
        rx_hits = 0
        last_pos = start
        for idx, rx_symbol in enumerate(PHASING_RX):
            dx_pos = start + idx * 2 + parity
            rx_pos = dx_pos + 1
            if rx_pos >= len(values):
                break
            dx_match = values[dx_pos] == PHASING_DX
            rx_match = values[rx_pos] == rx_symbol
            if dx_match:
                dx_hits += 1
                last_pos = dx_pos
            if rx_match:
                rx_hits += 1
                last_pos = rx_pos

        # Per ITU-R M.493-11 section 3.3:
        # "two DXs and one RX, or two RXs and one DX, or three RXs"
        if (dx_hits >= 2 and rx_hits >= 1) or (rx_hits >= 2 and dx_hits >= 1) or rx_hits >= 3:
            return last_pos + 1, parity
    return None


def _best_phasing_hits(values: list[int]) -> int:
    """Best contiguous phasing-pair hit count across the stream (diagnostic for analyze_bits)."""
    best = 0
    for start in range(0, max(0, len(values) - 14)):
        for parity in (0, 1):
            hits = 0
            for idx, rx_symbol in enumerate(PHASING_RX):
                dx_pos = start + idx * 2 + parity
                rx_pos = dx_pos + 1
                if rx_pos >= len(values):
                    break
                if values[dx_pos] == PHASING_DX and values[rx_pos] == rx_symbol:
                    hits += 2
                else:
                    break
            best = max(best, hits)
    return best


def _recover_information(decoded: list[DecodedSymbol], first_slot: int, dx_parity: int) -> tuple[list[int], int]:
    """Recover the DX symbol sequence from the interleaved DX/RX stream, repairing invalid DX symbols from their delayed RX copies (ITU-R M.493-9 S1.2.1)."""
    dx_symbols: list[int] = []
    rx_by_dx_index: dict[int, int] = {}
    invalid = 0
    for slot in range(first_slot, len(decoded)):
        item = decoded[slot]
        is_dx = (slot - first_slot) % 2 == 0
        if is_dx:
            dx_symbols.append(item.symbol)
            if not item.valid:
                invalid += 1
        else:
            target = len(dx_symbols) - 3
            if target >= 0 and item.valid:
                rx_by_dx_index[target] = item.symbol
            elif not item.valid:
                invalid += 1
    repaired: list[int] = []
    for idx, symbol in enumerate(dx_symbols):
        item = decoded[first_slot + idx * 2]
        if item.valid:
            repaired.append(symbol)
        elif idx in rx_by_dx_index:
            repaired.append(rx_by_dx_index[idx])
        else:
            repaired.append(symbol)
    return repaired, invalid


def _invalid_dx_symbols(decoded: list[DecodedSymbol], first_slot: int, frame_len: int) -> int:
    """Count invalid DX symbols within a frame span (used for confidence scoring)."""
    invalid = 0
    for idx in range(frame_len):
        slot = first_slot + idx * 2
        if slot >= len(decoded) or not decoded[slot].valid:
            invalid += 1
    return invalid


def _trim_to_ecc(symbols: list[int]) -> list[int] | None:
    """Find the first EOS whose following symbol validates as ECC and return the frame trimmed to it; applies format symbol-count rules and leading-duplicate repair."""
    for idx in range(4, min(len(symbols), 80)):
        if symbols[idx] not in EOS:
            continue
        # EOS is transmitted three times in DX, with ECC after the first EOS.
        ecc_idx = idx + 1
        if ecc_idx >= len(symbols):
            continue
        candidate = symbols[: ecc_idx + 1]
        calc = ecc_for(_ecc_information_symbols(candidate[:-1]))
        if calc == candidate[-1]:
            # ECC matches — validate symbol count against ITU-R M.493-13.
            fmt = candidate[0]
            expected = _FORMAT_SYMBOL_COUNTS.get(fmt)
            if expected is not None:
                if fmt in _FORMAT_EXACT_COUNTS:
                    if len(candidate) == expected:
                        return candidate  # Exact match, no repair needed
                    # Try repair: strip leading duplicate format symbols
                    repaired = _try_repair_symbol_count(candidate)
                    if repaired is not None:
                        return repaired
                    continue
                else:
                    if len(candidate) >= expected:
                        return candidate
                    continue
            return candidate  # Unknown format, accept as-is
        # ECC mismatch — try repairing leading duplicate format symbols
        # before rejecting. Common artifact: extra format symbol at start
        # causes ECC to fail because _ecc_information_symbols only dedupes
        # the first 2, leaving the 3rd in the calculation.
        fmt = candidate[0]
        if fmt in _FORMAT_EXACT_COUNTS and fmt in FORMAT_SPECIFIERS:
            # Check for 3+ leading format symbols
            if len(candidate) >= 3 and candidate[0] == candidate[1] == candidate[2]:
                repaired = _try_repair_symbol_count(candidate)
                if repaired is not None:
                    return repaired
    return None


def _try_repair_symbol_count(symbols: list[int]) -> list[int] | None:
    """Try to repair frames with wrong symbol count by stripping common artifacts.

    The most common artifact is a duplicate format symbol at position 0-1,
    caused by the demodulator picking up noise as an extra symbol in longer
    audio files. This function strips leading duplicate format symbols and
    re-validates the ECC on the repaired frame.

    Returns the repaired symbol list if successful, None otherwise.
    """
    if not symbols:
        return None

    fmt = symbols[0]
    expected = _FORMAT_SYMBOL_COUNTS.get(fmt)
    if expected is None:
        return None  # Variable-length format, no repair needed

    # Common artifact: one or more duplicate format symbols at the start
    # e.g., individual call has 23 symbols instead of 22
    for strip_count in range(1, len(symbols) - expected + 1):
        if not all(s == fmt for s in symbols[:strip_count]):
            break  # Not all leading symbols are duplicates, stop trying

        repaired = symbols[strip_count:]
        if len(repaired) == expected:
            # Validate ECC on repaired frame
            ecc = repaired[-1]
            calc = ecc_for(_ecc_information_symbols(repaired[:-1]))
            if calc == ecc:
                return repaired

    return None  # Could not repair


def _ecc_information_symbols(symbols: list[int]) -> list[int]:
    """Deduplicate the double leading format specifier before computing the ECC (the ECC covers the format once)."""
    if len(symbols) >= 2 and symbols[0] == symbols[1] and symbols[0] in FORMAT_SPECIFIERS:
        return [symbols[0], *symbols[2:]]
    return symbols


def _dedupe_frames(frames: list[FrameCandidate]) -> list[FrameCandidate]:
    """Keep the highest-scoring unique frame payloads (top 10)."""
    by_payload: dict[tuple[int, ...], FrameCandidate] = {}
    for frame in sorted(frames, key=lambda item: item.score, reverse=True):
        key = tuple(frame.symbols)
        if key not in by_payload:
            by_payload[key] = frame
    return sorted(by_payload.values(), key=lambda item: item.score, reverse=True)[:10]


def _is_distress_relay(symbols: list[int], idx: int, fmt: int) -> bool:
    """Check if a call is a distress relay based on format, category, and TC1.

    Distress relay: category=112, TC1=112.
    Can be format 120, 102, or 116.
    """
    if fmt not in (120, 102, 116):
        return False

    # Find category position
    if fmt == 120:
        # Individual: address (5 symbols) then category
        if len(symbols) <= idx + 5:
            return False
        cat_pos = idx + 5
    elif fmt == 102:
        # Geographic area: address (5 symbols) then category
        if len(symbols) <= idx + 5:
            return False
        cat_pos = idx + 5
    else:
        # all_ships: category is at idx (no address)
        cat_pos = idx

    if cat_pos >= len(symbols) or symbols[cat_pos] != 112:
        return False

    # Find TC1 position (after category + self-ID)
    tc1_pos = cat_pos + 1 + 5  # category + 5 self-ID symbols
    if tc1_pos >= len(symbols) or symbols[tc1_pos] != 112:
        return False

    return True


def _frame_to_json(frame: FrameCandidate) -> dict[str, object]:
    """Convert a FrameCandidate into the full call record dict with format-specific message decoding."""
    symbols = frame.symbols
    fmt = symbols[0] if symbols else -1
    payload: dict[str, object] = {
        "raw_symbols": symbols,
        "format": {"symbol": fmt, "name": FORMAT_SPECIFIERS.get(fmt, "unknown")},
        "category": None,
        "self_id": None,
        "address": None,
        "eos": {"symbol": symbols[-2], "name": EOS.get(symbols[-2], "unknown")} if len(symbols) >= 2 else None,
        "ecc": {
            "received": symbols[-1] if symbols else None,
            "calculated": ecc_for(_ecc_information_symbols(symbols[:-1])) if symbols else None,
            "ok": bool(symbols and ecc_for(_ecc_information_symbols(symbols[:-1])) == symbols[-1]),
        },
        "confidence": {
            "score": frame.score,
            "invalid_symbols": frame.invalid_symbols,
            "bit_offset": frame.bit_offset,
        },
        "message": {"kind": "unknown"},
    }

    # Determine symbol offset: format is repeated, so skip the duplicate
    idx = 2 if len(symbols) > 1 and symbols[0] == symbols[1] and symbols[0] in FORMAT_SPECIFIERS else 1

    # Route to format-specific decoder.
    if fmt == 112:
        _decode_distress_call(symbols, idx, payload)
    elif fmt == 102:
        _decode_geographic_area_call(symbols, idx, payload)
    elif fmt == 114:
        _decode_group_call(symbols, idx, payload)
    elif fmt == 123:
        _decode_automatic_service_call(symbols, idx, payload)
    elif fmt == 120:
        if _is_distress_relay(symbols, idx, fmt):
            _decode_distress_relay(symbols, idx, payload)
        else:
            _decode_individual_call(symbols, idx, payload)
    elif fmt == 116:
        # Check if this is a distress acknowledgement (category 112, TC1 110)
        # or a distress relay (category 112, TC1 112)
        if idx < len(symbols) and symbols[idx] == 112:
            if idx + 6 < len(symbols) and symbols[idx + 6] == 110:
                _decode_distress_acknowledgement(symbols, idx, payload)
            elif idx + 6 < len(symbols) and symbols[idx + 6] == 112:
                _decode_distress_relay(symbols, idx, payload)
            else:
                _decode_all_ships_call(symbols, idx, payload)
        else:
            _decode_all_ships_call(symbols, idx, payload)
    else:
        _decode_individual_call(symbols, idx, payload)

    return payload


def _decode_distress_call(symbols: list[int], idx: int, payload: dict[str, object]) -> None:
    """Decode distress call per ITU-R M.493-11 (format specifier 112).

    Per ITU-R M.493-11 section 6.2, distress format has NO category field.
    Structure (with idx=2, skipping the duplicate format specifier):
      [idx:idx+5]     Self MMSI (5 symbols)
      [idx+5]         Nature of distress (0-31)
      [idx+6:idx+16]  Position (10 symbols: lat 5 + lon 5)
      [idx+16:idx+20] UTC time (4 symbols)
      [idx+20]        Subsequent communication type (100-127)
      [idx+21]        EOS
      [idx+22]        ECC

    Total: idx + 23 symbols (typically 25 symbols total with idx=2)
    """
    fmt = symbols[0] if symbols else -1
    message_kind = "distress_call" if fmt == 112 else "all_ships_distress"

    # No category in distress format (ITU-R M.493-11 section 6.2)
    payload["category"] = None

    if len(symbols) < idx + 16:
        # Not enough symbols for full distress format - fall back to partial decode
        _decode_distress_partial(symbols, idx, payload, message_kind)
        return

    # Self MMSI starts at idx (no category before it)
    payload["self_id"] = decode_mmsi(symbols[idx : idx + 5])

    # Nature of distress at idx + 5 (ITU-R M.493-13 Table 3: symbols 100-112)
    nature_sym = symbols[idx + 5]
    if nature_sym in DISTRESS_NATURE:
        nature_name = DISTRESS_NATURE[nature_sym]
    elif nature_sym in TELECOMMAND_1:
        # Some distress alerts use telecommand codes instead of nature codes
        nature_name = f"telecommand: {TELECOMMAND_1[nature_sym]}"
    else:
        nature_name = f"unknown ({nature_sym})"

    # Position (5 symbols = 10 pair-coded digits) at idx + 6 to idx + 11
    pos_symbols = symbols[idx + 6 : idx + 11]
    position = decode_position(pos_symbols)

    # UTC time (2 symbols = 4 pair-coded digits) at idx + 11 to idx + 13
    time_symbols = symbols[idx + 11 : idx + 13]
    utc = decode_utc(time_symbols)

    # Subsequent communication type at idx + 13
    comm_sym = symbols[idx + 13]
    comm_name = TELECOMMAND_1.get(comm_sym, f"unknown ({comm_sym})")

    payload["message"] = {
        "kind": message_kind,
        "nature": {"symbol": nature_sym, "name": nature_name},
        "position": position,
        "utc": utc,
        "subsequent_communication": {"symbol": comm_sym, "name": comm_name},
    }


def _decode_distress_partial(symbols: list[int], idx: int, payload: dict[str, object], message_kind: str = "distress_call") -> None:
    """Partial distress decode when symbol count is insufficient."""
    payload["category"] = None
    if len(symbols) >= idx + 5:
        payload["self_id"] = decode_mmsi(symbols[idx : idx + 5])
    if len(symbols) > idx + 5:
        nature_sym = symbols[idx + 5]
        if nature_sym in DISTRESS_NATURE:
            nature_name = DISTRESS_NATURE[nature_sym]
        elif nature_sym in TELECOMMAND_1:
            nature_name = f"telecommand: {TELECOMMAND_1[nature_sym]}"
        else:
            nature_name = f"unknown ({nature_sym})"
        payload["message"] = {
            "kind": message_kind,
            "nature": {"symbol": nature_sym, "name": nature_name},
            "position": decode_position(symbols[idx + 6 : idx + 11]) if len(symbols) >= idx + 11 else None,
            "utc": decode_utc(symbols[idx + 11 : idx + 13]) if len(symbols) >= idx + 13 else None,
        }
    else:
        payload["message"] = {"kind": message_kind}


def _decode_distress_acknowledgement(symbols: list[int], idx: int, payload: dict[str, object]) -> None:
    """Decode distress acknowledgement per ITU-R M.493-11 Table 4.2.

    Format: 116 (all_ships), Category: 112 (distress)
    Structure:
      [idx]       Category (112 = distress)
      [idx+1:idx+6] Self MMSI (coast station, 5 symbols)
      [idx+6]     TC1 (110 = distress acknowledgement)
      [idx+7:idx+12] Distress MMSI (ship in distress, 5 symbols)
      [idx+12]    Nature of distress
      [idx+13:idx+23] Position (10 symbols)
      [idx+23:idx+27] UTC time (4 symbols)
      [idx+27]    Subsequent communication type
      [idx+28]    EOS
      [idx+29]    ECC
    """
    payload["message"] = {"kind": "distress_acknowledgement"}

    if len(symbols) < idx + 6:
        return

    # Category
    if symbols[idx] in CATEGORIES:
        payload["category"] = {"symbol": symbols[idx], "name": CATEGORIES[symbols[idx]]}

    # Self MMSI (coast station)
    payload["self_id"] = decode_mmsi(symbols[idx + 1 : idx + 6])

    if len(symbols) < idx + 7:
        return

    # TC1
    tc1_sym = symbols[idx + 6]
    payload["message"]["telecommand_1"] = {"symbol": tc1_sym, "name": TELECOMMAND_1.get(tc1_sym, f"unknown ({tc1_sym})")}

    if len(symbols) < idx + 12:
        return

    # Distress MMSI (ship in distress)
    payload["distress_mmsi"] = decode_mmsi(symbols[idx + 7 : idx + 12])

    if len(symbols) < idx + 13:
        return

    # Nature of distress
    nature_sym = symbols[idx + 12]
    if nature_sym in DISTRESS_NATURE:
        nature_name = DISTRESS_NATURE[nature_sym]
    elif nature_sym in TELECOMMAND_1:
        nature_name = f"telecommand: {TELECOMMAND_1[nature_sym]}"
    else:
        nature_name = f"unknown ({nature_sym})"
    payload["message"]["nature"] = {"symbol": nature_sym, "name": nature_name}

    # Position (5 symbols = 10 pair-coded digits)
    if len(symbols) >= idx + 18:
        payload["message"]["position"] = decode_position(symbols[idx + 13 : idx + 18])

    # UTC (2 symbols = 4 pair-coded digits)
    if len(symbols) >= idx + 20:
        payload["message"]["utc"] = decode_utc(symbols[idx + 18 : idx + 20])

    # Subsequent communication
    if len(symbols) >= idx + 21:
        comm_sym = symbols[idx + 20]
        payload["message"]["subsequent_communication"] = {"symbol": comm_sym, "name": TELECOMMAND_1.get(comm_sym, f"unknown ({comm_sym})")}


def _decode_distress_relay(symbols: list[int], idx: int, payload: dict[str, object]) -> None:
    """Decode distress relay per ITU-R M.493-11 Table 4.3.

    Formats: 120 (individual), 102 (geographic area), 116 (all ships)
    Category: 112 (distress)
    TC1: 112 (distress relay)
    Structure:
      [idx]       Category (112)
      [idx+1:idx+6] Self MMSI (relaying station, 5 symbols)
      [idx+6]     TC1 (112 = distress relay)
      [idx+7:idx+12] Distress MMSI (ship in distress, 5 symbols)
      [idx+12]    Nature of distress
      [idx+13:idx+23] Position (10 symbols)
      [idx+23:idx+27] UTC time (4 symbols)
      [idx+27]    Subsequent communication type
      [idx+28]    EOS
      [idx+29]    ECC
    """
    payload["message"] = {"kind": "distress_relay"}

    if len(symbols) < idx + 6:
        return

    # Category
    if symbols[idx] in CATEGORIES:
        payload["category"] = {"symbol": symbols[idx], "name": CATEGORIES[symbols[idx]]}

    # Self MMSI (relaying station)
    payload["self_id"] = decode_mmsi(symbols[idx + 1 : idx + 6])

    if len(symbols) < idx + 7:
        return

    # TC1
    tc1_sym = symbols[idx + 6]
    payload["message"]["telecommand_1"] = {"symbol": tc1_sym, "name": TELECOMMAND_1.get(tc1_sym, f"unknown ({tc1_sym})")}

    if len(symbols) < idx + 12:
        return

    # Distress MMSI
    payload["distress_mmsi"] = decode_mmsi(symbols[idx + 7 : idx + 12])

    if len(symbols) < idx + 13:
        return

    # Nature of distress
    nature_sym = symbols[idx + 12]
    if nature_sym in DISTRESS_NATURE:
        nature_name = DISTRESS_NATURE[nature_sym]
    elif nature_sym in TELECOMMAND_1:
        nature_name = f"telecommand: {TELECOMMAND_1[nature_sym]}"
    else:
        nature_name = f"unknown ({nature_sym})"
    payload["message"]["nature"] = {"symbol": nature_sym, "name": nature_name}

    # Position (5 symbols = 10 pair-coded digits)
    if len(symbols) >= idx + 18:
        payload["message"]["position"] = decode_position(symbols[idx + 13 : idx + 18])

    # UTC (2 symbols = 4 pair-coded digits)
    if len(symbols) >= idx + 20:
        payload["message"]["utc"] = decode_utc(symbols[idx + 18 : idx + 20])

    # Subsequent communication
    if len(symbols) >= idx + 21:
        comm_sym = symbols[idx + 20]
        payload["message"]["subsequent_communication"] = {"symbol": comm_sym, "name": TELECOMMAND_1.get(comm_sym, f"unknown ({comm_sym})")}


def _decode_geographic_area_call(symbols: list[int], idx: int, payload: dict[str, object]) -> None:
    """Decode geographic area call per ITU-R M.493-11 section 5.3.

    Format: 102
    Address: 10-digit geographic coordinate (5 symbols)
      Digit 1: quadrant (0=NE, 1=NW, 2=SE, 3=SW)
      Digits 2-3: latitude reference (tens, units of degrees)
      Digits 4-6: longitude reference (hundreds, tens, units of degrees)
      Digits 7-8: vertical side delta-phi (tens, units of degrees)
      Digits 9-10: horizontal side delta-lambda (tens, units of degrees)
    """
    payload["message"] = {"kind": "geographic_area_call"}

    if len(symbols) < idx + 5:
        return

    # Decode geographic area address (5 symbols -> 10 digits)
    area_symbols = symbols[idx : idx + 5]
    digits = []
    for s in area_symbols:
        digits.append(s // 10)
        digits.append(s % 10)

    quadrant = digits[0]
    area = {
        "kind": "geographic_area",
        "raw_symbols": area_symbols,
        "digits": digits,
        "quadrant": {0: "NE", 1: "NW", 2: "SE", 3: "SW"}.get(quadrant, "unknown"),
        "reference_lat_deg": digits[1] * 10 + digits[2],
        "reference_lon_deg": digits[3] * 100 + digits[4] * 10 + digits[5],
        "delta_lat_deg": digits[6] * 10 + digits[7],
        "delta_lon_deg": digits[8] * 10 + digits[9],
    }
    payload["address"] = area

    # Rest of the call: category, self-ID, telecommands, etc.
    if len(symbols) >= idx + 6:
        if symbols[idx + 5] in CATEGORIES:
            payload["category"] = {"symbol": symbols[idx + 5], "name": CATEGORIES[symbols[idx + 5]]}
            cat_end = idx + 6
        else:
            cat_end = idx + 5
    else:
        cat_end = idx + 5

    if len(symbols) >= cat_end + 5:
        payload["self_id"] = decode_mmsi(symbols[cat_end : cat_end + 5])
        mmsi_end = cat_end + 5
    else:
        mmsi_end = cat_end

    if len(symbols) >= mmsi_end + 2:
        remaining = symbols[mmsi_end + 2 : -2]
        payload["message"]["telecommand_1"] = {"symbol": symbols[mmsi_end], "name": TELECOMMAND_1.get(symbols[mmsi_end], f"unknown ({symbols[mmsi_end]})")}
        payload["message"]["telecommand_2"] = {"symbol": symbols[mmsi_end + 1], "name": TELECOMMAND_2.get(symbols[mmsi_end + 1], f"unknown ({symbols[mmsi_end + 1]})")}
        payload["message"]["remaining_symbols"] = remaining
        payload["message"]["communication"] = _decode_communication_channel(remaining)


def _decode_group_call(symbols: list[int], idx: int, payload: dict[str, object]) -> None:
    """Decode group call per ITU-R M.493-11 Table 4.8.

    Format: 114
    Address: group MMSI (5 symbols)
    """
    payload["message"] = {"kind": "group_call"}

    if len(symbols) < idx + 5:
        return

    payload["address"] = decode_mmsi(symbols[idx : idx + 5])
    addr_end = idx + 5

    if addr_end < len(symbols) and symbols[addr_end] in CATEGORIES:
        payload["category"] = {"symbol": symbols[addr_end], "name": CATEGORIES[symbols[addr_end]]}
        cat_end = addr_end + 1
    else:
        cat_end = addr_end

    if len(symbols) >= cat_end + 5:
        payload["self_id"] = decode_mmsi(symbols[cat_end : cat_end + 5])
        mmsi_end = cat_end + 5
    else:
        mmsi_end = cat_end

    if len(symbols) >= mmsi_end + 2:
        remaining = symbols[mmsi_end + 2 : -2]
        payload["message"]["telecommand_1"] = {"symbol": symbols[mmsi_end], "name": TELECOMMAND_1.get(symbols[mmsi_end], f"unknown ({symbols[mmsi_end]})")}
        payload["message"]["telecommand_2"] = {"symbol": symbols[mmsi_end + 1], "name": TELECOMMAND_2.get(symbols[mmsi_end + 1], f"unknown ({symbols[mmsi_end + 1]})")}
        payload["message"]["remaining_symbols"] = remaining
        payload["message"]["communication"] = _decode_communication_channel(remaining)


def _decode_automatic_service_call(symbols: list[int], idx: int, payload: dict[str, object]) -> None:
    """Decode automatic/semi-automatic service call per ITU-R M.493-11 Table 4.10.1/4.10.2.

    Format: 123
    Includes telephone number field (up to 9 symbols after frequency).
    """
    payload["message"] = {"kind": "automatic_service_call"}

    if len(symbols) < idx + 5:
        return

    payload["address"] = decode_mmsi(symbols[idx : idx + 5])
    addr_end = idx + 5

    if addr_end < len(symbols) and symbols[addr_end] in CATEGORIES:
        payload["category"] = {"symbol": symbols[addr_end], "name": CATEGORIES[symbols[addr_end]]}
        cat_end = addr_end + 1
    else:
        cat_end = addr_end

    if len(symbols) >= cat_end + 5:
        payload["self_id"] = decode_mmsi(symbols[cat_end : cat_end + 5])
        mmsi_end = cat_end + 5
    else:
        mmsi_end = cat_end

    if len(symbols) >= mmsi_end + 2:
        tc1 = symbols[mmsi_end]
        tc2 = symbols[mmsi_end + 1]
        payload["message"]["telecommand_1"] = {"symbol": tc1, "name": TELECOMMAND_1.get(tc1, f"unknown ({tc1})")}
        payload["message"]["telecommand_2"] = {"symbol": tc2, "name": TELECOMMAND_2.get(tc2, f"unknown ({tc2})")}

        remaining = symbols[mmsi_end + 2 : -2]
        payload["message"]["remaining_symbols"] = remaining

        # Per ITU-R M.493-11 §8.3.2, VHF semi-automatic/automatic service uses
        # only ONE channel/frequency element (3 symbols). MF/HF uses 6.
        # The channel element comes first, then the phone number (105/106 + digits).
        channel_element = remaining[:3] if len(remaining) >= 3 else remaining
        payload["message"]["communication"] = _decode_communication_channel(channel_element)

        # Look for telephone number indicator (symbol 105 = odd digits, 106 = even)
        phone_number = None
        for i, sym in enumerate(remaining):
            if sym in (105, 106):
                # Extract phone number digits from following symbols
                phone_digits = []
                for j in range(i + 1, min(i + 10, len(remaining))):
                    if remaining[j] < 100:
                        phone_digits.append(remaining[j] // 10)
                        phone_digits.append(remaining[j] % 10)
                    else:
                        break
                phone_number = "".join(str(d) for d in phone_digits)
                break

        if phone_number:
            payload["message"]["telephone_number"] = phone_number


def _decode_all_ships_call(symbols: list[int], idx: int, payload: dict[str, object]) -> None:
    """Decode all_ships call (format 116) per ITU-R M.493-11.

    All_ships call structure (no address MMSI):
      [idx]       Category (100=routine, 108=safety, 110=urgency, 112=distress)
      [idx+1:idx+6] Self MMSI (5 symbols)
      [idx+6]     TC1 / Nature (depends on category)
      [idx+7]     TC2
      [idx+8:]    Remaining fields (filler/channel or distress data)
      [-2]        EOS
      [-1]        ECC
    """
    if len(symbols) < idx + 6:
        payload["message"] = {"kind": "all_ships_call"}
        return

    # Category
    if symbols[idx] in CATEGORIES:
        payload["category"] = {"symbol": symbols[idx], "name": CATEGORIES[symbols[idx]]}
        cat_idx = idx + 1
    else:
        cat_idx = idx

    # Self MMSI
    if len(symbols) >= cat_idx + 5:
        payload["self_id"] = decode_mmsi(symbols[cat_idx : cat_idx + 5])
        mmsi_end = cat_idx + 5
    else:
        mmsi_end = cat_idx

    # Remaining fields: TC1, TC2, filler/channel, EOS, ECC
    if len(symbols) >= mmsi_end + 2:
        remaining = symbols[mmsi_end + 2 : -2]
        payload["message"] = {
            "kind": "all_ships_call",
            "telecommand_1": {"symbol": symbols[mmsi_end], "name": TELECOMMAND_1.get(symbols[mmsi_end], f"unknown ({symbols[mmsi_end]})")},
            "telecommand_2": {"symbol": symbols[mmsi_end + 1], "name": TELECOMMAND_2.get(symbols[mmsi_end + 1], f"unknown ({symbols[mmsi_end + 1]})")},
            "remaining_symbols": remaining,
            "communication": _decode_communication_channel(remaining),
        }
    else:
        payload["message"] = {"kind": "all_ships_call"}


def _decode_individual_call(symbols: list[int], idx: int, payload: dict[str, object]) -> None:
    """Decode individual/general call per ITU-R M.493-11 (23 symbols total).

    ITU-R M.493-11 individual call structure:
      [idx:idx+5]   Address MMSI (5 symbols)
      [idx+5]       Category (100=routine, 108=safety, 110=urgency)
      [idx+6:idx+11] Self MMSI (5 symbols)
      [idx+11]      Telecommand 1
      [idx+12]      Telecommand 2
      [idx+13:idx+19] Filler/subsequent communication (6 symbols)
      [idx+19]      EOS
      [idx+20]      ECC

    Total: idx + 21 symbols (typically 23 symbols total with idx=2)
    """
    fmt = symbols[0] if symbols else -1

    # Address MMSI (present for individual calls, not for all_ships/distress)
    if fmt not in (112, 116) and len(symbols) >= idx + 5:
        payload["address"] = decode_mmsi(symbols[idx : idx + 5])
        addr_end = idx + 5
    else:
        addr_end = idx

    # Category (comes AFTER address MMSI in individual calls)
    if addr_end < len(symbols) and symbols[addr_end] in CATEGORIES:
        payload["category"] = {"symbol": symbols[addr_end], "name": CATEGORIES[symbols[addr_end]]}
        cat_end = addr_end + 1
    else:
        cat_end = addr_end

    # Self MMSI
    if len(symbols) >= cat_end + 5:
        payload["self_id"] = decode_mmsi(symbols[cat_end : cat_end + 5])
        mmsi_end = cat_end + 5
    else:
        mmsi_end = cat_end

    # Telecommands and remaining symbols
    if len(symbols) >= mmsi_end + 2:
        remaining = symbols[mmsi_end + 2 : -2]
        message_kind = "individual_call" if fmt == 120 else "general_call"

        # Identify test calls (TC1 = 107, per ITU-R M.493-11 Table 3)
        if symbols[mmsi_end] == 107:
            message_kind = "test_call"

        payload["message"] = {
            "kind": message_kind,
            "is_test": symbols[mmsi_end] == 118,
            "telecommand_1": {"symbol": symbols[mmsi_end], "name": TELECOMMAND_1.get(symbols[mmsi_end], f"unknown ({symbols[mmsi_end]})")},
            "telecommand_2": {"symbol": symbols[mmsi_end + 1], "name": TELECOMMAND_2.get(symbols[mmsi_end + 1], f"unknown ({symbols[mmsi_end + 1]})")},
            "remaining_symbols": remaining,
            "communication": _decode_communication_channel(remaining),
        }
    else:
        message_kind = "individual_call" if fmt == 120 else "general_call"
        payload["message"] = {"kind": message_kind}


def _decode_communication_channel(symbols: list[int]) -> dict[str, object] | None:
    """Decode the subsequent-communication channel/frequency elements from the trailing symbols."""
    if not symbols:
        return None
    elements = [_decode_channel_or_frequency_element(symbols[index : index + 3]) for index in range(0, min(len(symbols), 6), 3)]
    elements = [element for element in elements if element is not None]
    if not elements:
        return None
    primary = elements[0]
    return {
        "kind": "subsequent_communication",
        "primary": primary,
        "secondary": elements[1] if len(elements) > 1 else None,
        "elements": elements,
    }


def _decode_channel_or_frequency_element(symbols: list[int]) -> dict[str, object] | None:
    """Decode a 3-symbol channel or frequency element.

    Per ITU-R M.493-11 Table 5, the digit layout from pair_digits() for 3 symbols is:
      [U, T, H, M, TM, HM] at indices 0-5.

    HM digit (index 5) determines the type:
      9 = VHF channel
      3 = MF/HF channel number
      other = frequency
    """
    if len(symbols) < 3:
        return None
    if symbols == [126, 126, 126]:
        return {"kind": "no_information", "raw_symbols": symbols}
    digits = pair_digits(symbols)
    if len(digits) != 6:
        return {"kind": "unknown", "raw_symbols": symbols, "digits": digits}

    # VHF-only receiver: the 6 field digits read [HM, TM, M, H, T, U]
    # (most significant first, per ITU-R M.493 Table 5). The HM digit is
    # digits[0]. HM=9 means a VHF working channel given by M H T U
    # (digits[2..5]); the M digit (digits[2]) carries the simplex mode.
    # (Previous code tested digits[5], which only matched channels ending
    # in 9, and misparsed e.g. 900013 as an MF/HF channel.)
    if digits[0] == "9":
        channel = int(digits[2] + digits[3] + digits[4] + digits[5])  # M, H, T, U
        mode_digit = digits[2]  # M digit
        mode = {
            "0": "two-frequency or standard channel",
            "1": "ship station transmit frequency used as simplex",
            "2": "coast station transmit frequency used as simplex",
        }.get(mode_digit, "unknown")
        return {
            "kind": "vhf_channel",
            "channel": str(channel),
            "mode": mode,
            "raw_symbols": symbols,
            "digits": digits,
        }

    # Not a VHF channel — should not occur on this VHF-only receiver.
    return {"kind": "frequency_or_channel", "raw_symbols": symbols, "digits": digits}
# =====================================================================
# CLI — decode a WAV file / stdin to JSON
# =====================================================================

MIN_VHF_DSC_DURATION_SECONDS = 1.5


def decode_file(path: Path) -> dict[str, object]:
    if not path.exists():
        raise FileNotFoundError(f"input audio file does not exist: {path}")
    samples, sample_rate = read_audio(path)
    duration_seconds = len(samples) / sample_rate if sample_rate else 0
    if duration_seconds < MIN_VHF_DSC_DURATION_SECONDS:
        return {
            "ok": False,
            "rejected": True,
            "reason": "audio_too_short",
            "message": (
                f"audio is {duration_seconds:.3f}s; VHF DSC decode requires at least "
                f"{MIN_VHF_DSC_DURATION_SECONDS:.1f}s of audio"
            ),
            "input": str(path),
            "sample_rate": sample_rate,
            "duration_seconds": duration_seconds,
            "minimum_duration_seconds": MIN_VHF_DSC_DURATION_SECONDS,
            "calls": [],
        }

    # Pre-screen: check if audio contains DSC-like AFSK energy
    signal = detect_dsc_signal(samples, sample_rate)
    if not signal.get("has_dsc_signal"):
        return {
            "ok": False,
            "rejected": True,
            "reason": "no_dsc_signal_detected",
            "message": (
                f"audio does not contain detectable DSC AFSK energy "
                f"(dsc_ratio={signal.get('dsc_ratio', 0):.4f}; "
                f"mark_energy={signal.get('mark_energy', 0):.2f}, "
                f"space_energy={signal.get('space_energy', 0):.2f})"
            ),
            "input": str(path),
            "sample_rate": sample_rate,
            "duration_seconds": duration_seconds,
            "signal_detection": signal,
            "calls": [],
        }

    candidates = demodulate_candidates(
        samples,
        sample_rate,
        enable_dot_sync=True,
        enable_clock_recovery=True,
    )
    calls: list[dict[str, object]] = []
    seen: set[tuple[int, ...]] = set()
    for candidate in candidates[:8]:
        for call in parse_bits(candidate.bits):
            key = tuple(call.get("raw_symbols", []))
            if key in seen:
                continue
            seen.add(key)
            call["demodulator"] = {
                "sample_rate": sample_rate,
                "symbol_offset_samples": candidate.offset,
                "samples_per_bit": candidate.samples_per_bit,
                "tone_confidence": candidate.confidence,
                "freq_offset_hz": candidate.freq_offset_hz,
                "dot_sync_confidence": candidate.dot_sync_confidence,
            }
            calls.append(call)
    calls.sort(key=lambda item: item.get("confidence", {}).get("score", 0), reverse=True)
    return {
        "ok": bool(calls),
        "input": str(path),
        "sample_rate": sample_rate,
        "duration_seconds": duration_seconds,
        "calls": calls,
    }


def main() -> None:
    import argparse
    import tempfile

    parser = argparse.ArgumentParser(
        description="Decode VHF Marine DSC audio (WAV) to JSON (ITU-R M.493)",
    )
    parser.add_argument("audio", nargs="?", type=Path,
                        help="input WAV file, or '-' to read audio bytes from stdin")
    parser.add_argument("--pretty", action="store_true", help="pretty-print JSON")
    parser.add_argument("--debug", action="store_true",
                        help="include demodulator diagnostics in JSON")
    args = parser.parse_args()

    if args.audio is None:
        parser.error("audio is required")

    try:
        if str(args.audio) == "-":
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(sys.stdin.buffer.read())
                tmp_path = Path(tmp.name)
            try:
                result = decode_file(tmp_path)
                result["input"] = "stdin"
            finally:
                tmp_path.unlink(missing_ok=True)
        else:
            result = decode_file(args.audio)
        if args.debug:
            # attach per-candidate diagnostics (already inside calls under
            # "demodulator"); nothing extra to add at top level
            pass
        print(json.dumps(result, indent=2 if args.pretty else None,
                         sort_keys=args.pretty))
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as exc:
        print(f"failed to decode {args.audio}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
